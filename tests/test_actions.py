"""T11 coverage: the send-time safety layer.

Almost every test here asserts a refusal. That is the shape of the module:
its job is to not do things. The one test that asserts an approve succeeds is
outnumbered roughly ten to one, deliberately.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.actions import (  # noqa: E402
    Action,
    ActionGate,
    ActionRequest,
    PendingDecision,
    Refusal,
    fingerprint,
    parse_action,
)
from shepherd.herdr import HerdrError  # noqa: E402

T0 = datetime(2026, 9, 5, 11, 7, 55, tzinfo=timezone.utc)

BASH_PROMPT = """
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don’t ask again for: git *
   3. No

 Esc to cancel · Tab to amend
"""

OTHER_PROMPT = """
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don’t ask again for: rm *
   3. No

 Esc to cancel · Tab to amend
"""

NO_PROMPT = "\n─────\n❯\n─────\n  ⏸ manual mode on\n"


def run(coro):
    return asyncio.run(coro)


class FakeSource:
    """Enough HerdrSource to exercise the gate. Records every write."""

    def __init__(self, pane_text=BASH_PROMPT, fail_keys=False):
        self.pane_text = pane_text
        self.fail_keys = fail_keys
        self.sent: list[tuple[str, list[str]]] = []
        self.focused: list[str] = []
        self.reads = 0

    async def list_agents(self):  # not used here
        raise NotImplementedError

    async def read_pane(self, pane_id, lines=40):
        self.reads += 1
        return self.pane_text

    async def send_keys(self, pane_id, keys):
        if self.fail_keys:
            raise HerdrError("invalid key")
        self.sent.append((pane_id, list(keys)))

    async def focus(self, pane_id):
        self.focused.append(pane_id)


def gate(tmp_path, source=None, shown_text=BASH_PROMPT, truncated=False,
         decision="abc123", pane="w9:p1"):
    g = ActionGate(
        source=source or FakeSource(),
        now=lambda: T0,
        audit=tmp_path / "actions.jsonl",
    )
    g.observe_frame(
        frozenset({pane, "w2:p1"}),
        {decision: PendingDecision(pane, fingerprint(shown_text), truncated)},
    )
    return g


def audit(tmp_path):
    p = tmp_path / "actions.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln]


# ------------------------------------------------------------ wire parsing


def test_parse_action_accepts_the_closed_set():
    for k, expected in (("approve", Action.APPROVE), ("deny", Action.DENY),
                        ("focus", Action.FOCUS)):
        req = parse_action({"t": "act", "i": "w9:p1", "k": k, "r": "abc123"})
        assert req is not None and req.action is expected


def test_parse_action_rejects_everything_else_silently():
    bad = [
        {"t": "act", "i": "w9:p1", "k": "run"},           # not in the set
        {"t": "act", "i": "w9:p1", "k": "pane.run"},
        {"t": "act", "i": "w9:p1"},                        # no action
        {"t": "act", "i": "not-a-pane", "k": "approve"},
        {"t": "act", "i": "w9:p1; rm -rf /", "k": "deny"},
        {"t": "snap", "i": "w9:p1", "k": "approve"},       # wrong frame type
        {"t": "act", "i": "w9:p1", "k": "approve", "r": "../../etc"},
        {"t": "act", "i": "w9:p1", "k": 3},
        "not a dict", None, 42,
    ]
    for raw in bad:
        assert parse_action(raw) is None, raw


# ------------------------------------------------------------ fingerprint


def test_fingerprint_ignores_churn_above_the_question():
    # Spinners, token counters and redrawn borders appear above the prompt and
    # must not read as a changed question. Churn *inside* the option list is a
    # different matter and deliberately does change the fingerprint: an
    # unexplained extra option is exactly what should abort an approve.
    noisy = ("● Write(x)\n   · Thinking... (3s · 12 tokens)\n"
             "──────────────────────\n" + BASH_PROMPT)
    assert fingerprint(noisy) == fingerprint(BASH_PROMPT)


def test_fingerprint_changes_when_an_option_appears():
    extra = BASH_PROMPT.replace("   3. No", "   3. Yes, and never ask\n   4. No")
    assert fingerprint(extra) != fingerprint(BASH_PROMPT)


def test_fingerprint_changes_when_the_question_does():
    assert fingerprint(BASH_PROMPT) != fingerprint(OTHER_PROMPT)


def test_fingerprint_of_no_prompt_is_empty():
    assert fingerprint(NO_PROMPT) == ""
    assert fingerprint(None) == ""


# ---------------------------------------------------------------- allowlist


def test_action_on_an_unrendered_pane_is_refused(tmp_path):
    g = gate(tmp_path)
    res = run(g.dispatch(ActionRequest("w7:p1", Action.DENY)))
    assert not res.ok and Refusal.UNKNOWN_PANE.value in res.reason


# ------------------------------------------------------------------- deny


def test_deny_always_works_and_needs_no_verification(tmp_path):
    src = FakeSource(pane_text=OTHER_PROMPT)     # prompt already changed
    g = gate(tmp_path, source=src)
    res = run(g.dispatch(ActionRequest("w9:p1", Action.DENY, "abc123")))
    assert res.ok
    assert src.sent == [("w9:p1", ["esc"])]
    # Refusing to deny would strand a blocked agent; cancelling the wrong
    # prompt is a nuisance, not a hazard.
    assert src.reads == 0


def test_deny_burns_the_decision_id_too(tmp_path):
    g = gate(tmp_path)
    run(g.dispatch(ActionRequest("w9:p1", Action.DENY, "abc123")))
    res = run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))
    assert not res.ok and Refusal.REPLAYED.value in res.reason


# ---------------------------------------------------------------- approve


def test_approve_reverifies_then_sends(tmp_path):
    src = FakeSource()
    g = gate(tmp_path, source=src)
    res = run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))
    assert res.ok
    assert src.reads == 1, "must re-read the pane before acting"
    assert src.sent == [("w9:p1", ["enter"])]


def test_approve_aborts_when_the_prompt_changed(tmp_path):
    # The whole reason this layer exists: approve git status, land on rm -rf.
    src = FakeSource(pane_text=OTHER_PROMPT)
    g = gate(tmp_path, source=src, shown_text=BASH_PROMPT)
    res = run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))
    assert not res.ok
    assert Refusal.PROMPT_CHANGED.value in res.reason
    assert res.restale, "device must be told to refresh"
    assert src.sent == [], "nothing may be sent after a mismatch"


def test_approve_aborts_when_the_prompt_is_gone(tmp_path):
    src = FakeSource(pane_text=NO_PROMPT)
    g = gate(tmp_path, source=src)
    res = run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))
    assert not res.ok and Refusal.PROMPT_GONE.value in res.reason
    assert res.restale and src.sent == []


def test_approve_refused_when_the_prompt_was_truncated(tmp_path):
    # The device may always say no; it may only say yes to something it showed
    # in full. A 40-column display hides the tail of any real command.
    src = FakeSource()
    g = gate(tmp_path, source=src, truncated=True)
    res = run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))
    assert not res.ok and Refusal.TRUNCATED.value in res.reason
    assert src.sent == []


def test_approve_is_one_shot(tmp_path):
    src = FakeSource()
    g = gate(tmp_path, source=src)
    assert run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123"))).ok
    second = run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))
    assert not second.ok and Refusal.REPLAYED.value in second.reason
    assert len(src.sent) == 1


def test_approve_needs_a_decision_id_the_host_minted(tmp_path):
    g = gate(tmp_path)
    for req in (ActionRequest("w9:p1", Action.APPROVE),
                ActionRequest("w9:p1", Action.APPROVE, "deadbeef")):
        res = run(g.dispatch(req))
        assert not res.ok and Refusal.UNKNOWN_DECISION.value in res.reason


def test_decision_id_cannot_be_replayed_against_another_pane(tmp_path):
    g = gate(tmp_path)
    res = run(g.dispatch(ActionRequest("w2:p1", Action.APPROVE, "abc123")))
    assert not res.ok and Refusal.UNKNOWN_DECISION.value in res.reason


def test_approve_refused_when_no_safe_option_exists(tmp_path):
    trust = ("\n Quick safety check:\n ❯ No, exit\n"
             "   Yes, I trust this folder\n\n Enter to confirm · Esc to cancel\n")
    src = FakeSource(pane_text=trust)
    g = gate(tmp_path, source=src, shown_text=trust)
    res = run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))
    assert not res.ok and Refusal.UNSAFE_OPTIONS.value in res.reason
    assert src.sent == []


def test_herdr_failure_is_reported_not_swallowed(tmp_path):
    src = FakeSource(fail_keys=True)
    g = gate(tmp_path, source=src)
    res = run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))
    assert not res.ok and Refusal.HERDR_FAILED.value in res.reason


# ------------------------------------------------------------------ focus


def test_focus_needs_only_the_allowlist(tmp_path):
    src = FakeSource()
    g = gate(tmp_path, source=src)
    assert run(g.dispatch(ActionRequest("w2:p1", Action.FOCUS))).ok
    assert src.focused == ["w2:p1"]


# ------------------------------------------------------------------ audit


def test_every_attempt_is_recorded_including_refusals(tmp_path):
    src = FakeSource()
    g = gate(tmp_path, source=src)
    run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))
    run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))   # replay
    run(g.dispatch(ActionRequest("w7:p1", Action.DENY)))                # bad pane

    rows = audit(tmp_path)
    assert len(rows) == 3
    assert [r["ok"] for r in rows] == [True, False, False]
    assert rows[0]["keys"] == ["enter"]
    assert rows[0]["ts"] == "2026-09-05T11:07:55Z"
    # The log carries the question as shown, so it answers "what was approved"
    # without ambiguity.
    assert "do you want to proceed" in rows[0]["shown"].lower()


def test_a_broken_audit_log_never_blocks_a_deny(tmp_path):
    src = FakeSource()
    g = ActionGate(source=src, now=lambda: T0,
                   audit=tmp_path / "nope" / "\x00bad" / "actions.jsonl")
    g.observe_frame(frozenset({"w9:p1"}), {})
    res = run(g.dispatch(ActionRequest("w9:p1", Action.DENY)))
    assert res.ok and src.sent == [("w9:p1", ["esc"])]


# --------------------------------------------------------- frame observation


def test_a_new_frame_replaces_the_allowlist(tmp_path):
    g = gate(tmp_path)
    g.observe_frame(frozenset({"w2:p1"}), {})
    res = run(g.dispatch(ActionRequest("w9:p1", Action.DENY)))
    assert not res.ok and Refusal.UNKNOWN_PANE.value in res.reason


def test_burned_ids_survive_a_new_frame(tmp_path):
    # An id must not become answerable again just because a frame was resent.
    src = FakeSource()
    g = gate(tmp_path, source=src)
    assert run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123"))).ok
    g.observe_frame(
        frozenset({"w9:p1"}),
        {"abc123": PendingDecision("w9:p1", fingerprint(BASH_PROMPT), False)},
    )
    res = run(g.dispatch(ActionRequest("w9:p1", Action.APPROVE, "abc123")))
    assert not res.ok and Refusal.REPLAYED.value in res.reason
