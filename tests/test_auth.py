"""Signature coverage: the secret, the canonical message, and the gate.

The canonical message tests matter more than they look. The device builds the
same string in plain C, so anything locale-dependent, anything clever about
separators, or any disagreement about how an absent decision id is encoded
would diverge silently and reject every real action.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

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
from shepherd.auth import (  # noqa: E402
    MAC_HEX_LEN,
    SECRET_BYTES,
    build_flag,
    canonical_message,
    load_or_create_secret,
    sign,
    verify,
)

SECRET = bytes(range(32))
TS = "2026-09-05T12:30:00Z"

BASH_PROMPT = """
 Do you want to proceed?
 ❯ 1. Yes
   2. No

 Esc to cancel
"""


def run(coro):
    return asyncio.run(coro)


class FakeSource:
    def __init__(self, pane_text=BASH_PROMPT):
        self.pane_text = pane_text
        self.sent: list[tuple[str, list[str]]] = []
        self.focused: list[str] = []
        self.reads = 0

    async def read_pane(self, pane_id, lines=40):
        self.reads += 1
        return self.pane_text

    async def send_keys(self, pane_id, keys):
        self.sent.append((pane_id, list(keys)))

    async def focus(self, pane_id):
        self.focused.append(pane_id)


# ------------------------------------------------------------- the secret


def test_secret_is_generated_once_and_reused(tmp_path):
    p = tmp_path / "shepherd.secret"
    first = load_or_create_secret(p)
    assert len(first) == SECRET_BYTES
    assert load_or_create_secret(p) == first, "must not rotate on every start"


def test_secret_is_stored_as_hex_for_pasting_into_a_build_flag(tmp_path):
    p = tmp_path / "shepherd.secret"
    raw = load_or_create_secret(p)
    text = p.read_text(encoding="utf-8").strip()
    assert bytes.fromhex(text) == raw
    assert text == text.lower()
    # Pinned exactly, not by prefix. The loose assertion here let a flag of
    # the form -DSHEPHERD_SECRET='"..."' pass for weeks: correct on a shell
    # command line, wrong in a PlatformIO ini, where plain quotes are eaten
    # and the define reaches the compiler as a bare numeric token. This is
    # the form verified byte-for-byte against a secret.ini that builds and
    # produces signatures the relay accepts.
    assert build_flag(raw) == '-DSHEPHERD_SECRET=\\"' + raw.hex() + '\\"'
    assert build_flag(raw).count("\\") == 2


def test_a_corrupt_secret_file_fails_loudly(tmp_path):
    p = tmp_path / "shepherd.secret"
    p.write_text("not hex at all", encoding="utf-8")
    with pytest.raises(ValueError, match="not hex"):
        load_or_create_secret(p)

    p.write_text("aabb", encoding="utf-8")   # right alphabet, wrong length
    with pytest.raises(ValueError, match="expected"):
        load_or_create_secret(p)


# -------------------------------------------------- the canonical message


def test_canonical_message_shape():
    # Written as a literal, not an f-string, and deliberately identical to the
    # one asserted in firmware/test/test_shepherd_frame/test_main.cpp. The two
    # implementations drifting is the realistic failure and it would be silent
    # — every real action refused as a bad signature — so the agreement is
    # made greppable across both suites rather than left to inspection.
    assert canonical_message("2026-09-05T12:30:00Z", "w9:p1", "approve",
                             "abc123") == \
        b"2026-09-05T12:30:00Z|w9:p1|approve|abc123|"
    # The fifth field is the chosen option index, added in v3. It is signed
    # rather than sent beside the MAC, so a captured approve for "Yes" cannot
    # have its index bumped to the "Yes, and don't ask again" underneath it.
    assert canonical_message("2026-09-05T12:30:00Z", "w9:p1", "approve",
                             "abc123", 2) == \
        b"2026-09-05T12:30:00Z|w9:p1|approve|abc123|2"


def test_absent_decision_id_is_an_empty_field_not_a_missing_one():
    # focus carries no decision id. Dropping the field instead of emptying it
    # would let focus|<nothing> collide with a different action's message.
    assert canonical_message(TS, "w9:p1", "focus", None) == \
        f"{TS}|w9:p1|focus||".encode("utf-8")
    # Same for the choice: absent and "chose option 0" must be different, or
    # an approve that picked the first option could collide with one that
    # picked nothing at all.
    assert canonical_message(TS, "w9:p1", "approve", "a", None) != \
        canonical_message(TS, "w9:p1", "approve", "a", 0)
    assert canonical_message(TS, "w9:p1", "focus", None) == \
        canonical_message(TS, "w9:p1", "focus", "")


def test_every_field_is_bound():
    base = sign(SECRET, TS, "w9:p1", "approve", "abc123")
    # Choosing option 1 and choosing option 2 must not share a signature.
    assert base != sign(SECRET, TS, "w9:p1", "approve", "abc123", 0)
    one = sign(SECRET, TS, "w9:p1", "approve", "abc123", 1)
    two = sign(SECRET, TS, "w9:p1", "approve", "abc123", 2)
    assert one != two
    assert base != sign(SECRET, "2026-09-05T12:30:01Z", "w9:p1", "approve", "abc123")
    assert base != sign(SECRET, TS, "w2:p1", "approve", "abc123")
    assert base != sign(SECRET, TS, "w9:p1", "deny", "abc123")
    assert base != sign(SECRET, TS, "w9:p1", "approve", "abc124")
    assert base != sign(bytes(32), TS, "w9:p1", "approve", "abc123")


def test_signature_is_short_enough_for_a_20_byte_payload_link():
    mac = sign(SECRET, TS, "w9:p1", "approve", "abc123")
    assert len(mac) == MAC_HEX_LEN == 16


def test_verify_accepts_its_own_signature_and_rejects_everything_else():
    mac = sign(SECRET, TS, "w9:p1", "approve", "abc123")
    assert verify(SECRET, mac, TS, "w9:p1", "approve", "abc123")
    assert verify(SECRET, mac.upper(), TS, "w9:p1", "approve", "abc123")

    assert not verify(SECRET, mac, TS, "w9:p1", "deny", "abc123")
    assert not verify(bytes(32), mac, TS, "w9:p1", "approve", "abc123")
    for bad in (None, "", "short", "z" * 16, 12345, mac[:-1] + "0"):
        assert not verify(SECRET, bad, TS, "w9:p1", "approve", "abc123"), bad


# ------------------------------------------------------------- the gate


def gate(tmp_path, source=None, secret=SECRET, ts=TS, decision="abc123"):
    g = ActionGate(source=source or FakeSource(), audit=tmp_path / "a.jsonl",
                   secret=secret)
    g.observe_frame(
        frozenset({"w9:p1", "w2:p1"}),
        {decision: PendingDecision("w9:p1", fingerprint(BASH_PROMPT), False)},
        ts=ts,
    )
    return g


def signed(action="approve", pane="w9:p1", decision="abc123", ts=TS,
           secret=SECRET):
    return ActionRequest(pane_id=pane, action=Action(action),
                         decision_id=decision, ts=ts,
                         mac=sign(secret, ts, pane, action, decision))


def test_a_correctly_signed_action_goes_through(tmp_path):
    src = FakeSource()
    g = gate(tmp_path, src)
    res = run(g.dispatch(signed()))
    assert res.ok
    assert src.sent == [("w9:p1", ["enter"])]


def test_an_unsigned_action_is_refused_when_a_secret_is_set(tmp_path):
    src = FakeSource()
    g = gate(tmp_path, src)
    res = run(g.dispatch(ActionRequest("w9:p1", Action.DENY, "abc123")))
    assert not res.ok and Refusal.UNSIGNED.value in res.reason
    assert src.sent == []


def test_a_forged_signature_is_refused(tmp_path):
    src = FakeSource()
    g = gate(tmp_path, src)
    forged = signed(secret=bytes(32))
    res = run(g.dispatch(forged))
    assert not res.ok and Refusal.BAD_MAC.value in res.reason
    assert src.sent == []


def test_a_signature_for_a_different_action_does_not_transfer(tmp_path):
    # Capturing a legitimate deny must not yield an approve.
    src = FakeSource()
    g = gate(tmp_path, src)
    deny = signed("deny")
    swapped = ActionRequest("w9:p1", Action.APPROVE, "abc123", ts=deny.ts,
                            mac=deny.mac)
    res = run(g.dispatch(swapped))
    assert not res.ok and Refusal.BAD_MAC.value in res.reason


def test_a_frame_we_never_sent_is_refused(tmp_path):
    # Binding to a timestamp we actually issued is the only replay protection
    # focus has, since it carries no decision id.
    src = FakeSource()
    g = gate(tmp_path, src)
    old = signed("focus", pane="w2:p1", decision=None, ts="2020-01-01T00:00:00Z")
    res = run(g.dispatch(old))
    assert not res.ok and Refusal.STALE_TS.value in res.reason
    assert src.focused == []


def test_a_thumb_slower_than_a_keepalive_still_works(tmp_path):
    # Several frames go out while the human reads the question. The one they
    # were looking at must still be answerable.
    src = FakeSource()
    g = gate(tmp_path, src, ts="t0")
    for t in ("t1", "t2", "t3"):
        g.observe_frame(frozenset({"w9:p1"}),
                        {"abc123": PendingDecision("w9:p1", fingerprint(BASH_PROMPT), False)},
                        ts=t)
    res = run(g.dispatch(signed(ts="t0")))
    assert res.ok, "answering the frame you were reading must not race the keepalive"


def test_very_old_frames_eventually_stop_being_answerable(tmp_path):
    src = FakeSource()
    g = gate(tmp_path, src, ts="t0")
    for t in [f"t{i}" for i in range(1, 12)]:
        g.observe_frame(frozenset({"w9:p1"}), {}, ts=t)
    res = run(g.dispatch(signed(ts="t0")))
    assert not res.ok and Refusal.STALE_TS.value in res.reason


def test_verification_happens_before_the_pane_is_read(tmp_path):
    # An unauthenticated peer should not be able to make the relay do work,
    # including the work of shelling out to read a pane.
    src = FakeSource()
    g = gate(tmp_path, src)
    run(g.dispatch(signed(secret=bytes(32))))
    assert src.reads == 0


def test_unsigned_mode_still_works_for_bringup(tmp_path):
    # secret=None is for tests and for bringing a device up before its
    # firmware carries the secret.
    src = FakeSource()
    g = gate(tmp_path, src, secret=None)
    assert run(g.dispatch(ActionRequest("w9:p1", Action.DENY, "abc123"))).ok


def test_parse_action_carries_the_signature_fields():
    req = parse_action({"t": "act", "i": "w9:p1", "k": "approve",
                        "r": "abc123", "ts": TS, "mac": "0011223344556677"})
    assert req is not None
    assert req.ts == TS and req.mac == "0011223344556677"

    for bad in ({"t": "act", "i": "w9:p1", "k": "deny", "ts": 5},
                {"t": "act", "i": "w9:p1", "k": "deny", "mac": []}):
        assert parse_action(bad) is None


# ------------------------------------------------- choosing an option


BASH4 = """
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for: git *
   3. No

 Esc to cancel
"""
OPTS = ("Yes", "Yes, and don't ask again for: git *", "No")


def gate4(tmp_path, src, ts=TS, decision="abc123"):
    g = ActionGate(source=src, audit=tmp_path / "a.jsonl", secret=SECRET)
    g.observe_frame(
        frozenset({"w9:p1"}),
        {decision: PendingDecision("w9:p1", fingerprint(BASH4), False, OPTS)},
        ts=ts,
    )
    return g


def chose(index, pane="w9:p1", decision="abc123", ts=TS, secret=SECRET):
    return ActionRequest(pane_id=pane, action=Action.APPROVE,
                         decision_id=decision, ts=ts, choice=index,
                         mac=sign(secret, ts, pane, "approve", decision, index))


def test_a_chosen_option_is_selected_by_walking_the_cursor(tmp_path):
    src = FakeSource(BASH4)
    g = gate4(tmp_path, src)
    assert run(g.dispatch(chose(1))).ok
    assert src.sent == [("w9:p1", ["down", "enter"])]


def test_the_choice_is_covered_by_the_signature(tmp_path):
    # Sign for option 0, present option 1. Without the choice inside the
    # signed message this would go through and grant a permanent permission.
    src = FakeSource(BASH4)
    g = gate4(tmp_path, src)
    forged = ActionRequest(
        pane_id="w9:p1", action=Action.APPROVE, decision_id="abc123", ts=TS,
        choice=1, mac=sign(SECRET, TS, "w9:p1", "approve", "abc123", 0))
    res = run(g.dispatch(forged))
    assert not res.ok and Refusal.BAD_MAC.value in res.reason
    assert src.sent == []


def test_an_option_the_device_was_never_shown_is_refused(tmp_path):
    src = FakeSource(BASH4)
    g = gate4(tmp_path, src)
    res = run(g.dispatch(chose(5)))
    assert not res.ok and Refusal.UNKNOWN_CHOICE.value in res.reason
    assert src.sent == []


def test_a_prompt_that_changed_underneath_refuses_the_choice(tmp_path):
    # The fingerprint guard, reached through the choice path: the human
    # picked option 1 of a list that is no longer the list on the pane.
    src = FakeSource(BASH4)
    g = gate4(tmp_path, src)
    src.pane_text = BASH_PROMPT
    res = run(g.dispatch(chose(1)))
    assert not res.ok and Refusal.PROMPT_CHANGED.value in res.reason
    assert src.sent == []


def test_no_choice_still_takes_the_safe_path(tmp_path):
    # A device that sends no index gets the relay's own judgement, which
    # picks the plain Yes and refuses to guess between qualified ones.
    src = FakeSource(BASH4)
    g = gate4(tmp_path, src)
    assert run(g.dispatch(signed())).ok
    assert src.sent == [("w9:p1", ["enter"])]


def test_parse_action_reads_the_choice_and_rejects_nonsense():
    req = parse_action({"t": "act", "i": "w9:p1", "k": "approve",
                        "r": "abc123", "c": 2})
    assert req is not None and req.choice == 2

    for bad in (True, False, "1", 1.5, -1, 999, None if False else []):
        assert parse_action({"t": "act", "i": "w9:p1", "k": "approve",
                             "r": "abc123", "c": bad}) is None, bad
    # Absent is fine and means "no choice".
    assert parse_action({"t": "act", "i": "w9:p1", "k": "approve",
                         "r": "abc123"}).choice is None
