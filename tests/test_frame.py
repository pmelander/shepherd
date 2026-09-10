"""T8 coverage: the frame builder.

This layer carries most of the decisions the design review argued about, so
these tests double as the record of them. Where a test encodes a decision, the
comment says which one and why — a future reader changing the behaviour should
have to argue with the reasoning, not just with a red test.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.frame import (  # noqa: E402
    ALIAS_LEN,
    MAX_AGENTS,
    MAX_OPTIONS,
    OPTION_MAX,
    PROMPT_MAX,
    PROTOCOL_VERSION,
    SAID_MAX,
    FrameBuilder,
    decision_id,
    derive_alias,
    disambiguate,
    RECAP_MAX,
    truncate_prompt,
    truncate_recap,
)
from shepherd.models import Agent, AgentStatus, HerdSnapshot  # noqa: E402

T0 = datetime(2026, 9, 5, 10, 21, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)


def agent(pane_id="w2:p1", status=AgentStatus.IDLE, seq=1, cwd=None, **kw):
    return Agent(
        pane_id=pane_id,
        workspace_id=pane_id.split(":")[0],
        status=status,
        state_change_seq=seq,
        cwd=cwd if cwd is not None else r"C:\.workspaces\new-pricemanager-client",
        terminal_title=kw.get("title", "some work"),
        kind="claude",
        focused=kw.get("focused", False),
    )


# ------------------------------------------------------------- aliases


def test_alias_keeps_distinguishing_letters_not_just_a_prefix():
    # The review's original plan was head truncation. These two real repo
    # names are why that was wrong: they share a long prefix.
    a = derive_alias(r"C:\repos\Yield_PriceManager_API")
    b = derive_alias(r"C:\repos\Yield_InitialPricing_Service")
    assert a != b
    assert len(a) <= ALIAS_LEN and len(b) <= ALIAS_LEN


def test_alias_handles_single_word_and_separators():
    assert derive_alias(r"C:\x\herdr") == "herdr"
    assert len(derive_alias(r"C:\x\new-pricemanager-client")) <= ALIAS_LEN
    assert derive_alias("/home/u/tempo_curve_adjustment") != ""


def test_alias_of_empty_or_weird_cwd_is_safe():
    for cwd in ("", "/", "\\", "///", "!!!"):
        out = derive_alias(cwd)
        assert isinstance(out, str) and out


def test_disambiguate_is_stable_and_unique():
    aliases = {"w2:p1": "sameword", "w9:p1": "sameword", "w5:p1": "other"}
    out = disambiguate(aliases)
    assert len(set(out.values())) == 3
    assert out["w5:p1"] == "other"
    # Same input must always give the same labels; a label that shuffles
    # between frames is worse than a duplicate.
    assert disambiguate(aliases) == out
    assert all(len(v) <= ALIAS_LEN for v in out.values())


# ------------------------------------------------------------- prompts


def test_truncate_flags_when_it_cut():
    short, cut = truncate_prompt("Allow Bash(git status)?")
    assert not cut and short == "Allow Bash(git status)?"

    long, cut = truncate_prompt("x" * 500)
    assert cut and len(long) <= PROMPT_MAX


def test_truncate_collapses_whitespace():
    out, _ = truncate_prompt("  Allow   Bash(git\n  push)?  ")
    assert out == "Allow Bash(git push)?"


def test_decision_id_changes_when_the_prompt_changes():
    # This is what makes send-time re-verification cheap: if the agent moves
    # on to a different question, the id no longer matches and a decision
    # minted against the old prompt is detectably stale.
    a = decision_id("w9:p1", 441, "Allow Bash(git status)?")
    b = decision_id("w9:p1", 441, "Allow Bash(rm -rf /)?")
    c = decision_id("w9:p1", 442, "Allow Bash(git status)?")
    assert a != b and a != c
    assert a == decision_id("w9:p1", 441, "Allow Bash(git status)?")


# --------------------------------------------------------------- frame


def test_frame_envelope():
    f = FrameBuilder(now=Clock()).build(HerdSnapshot(agents=(agent(),)))
    assert f["t"] == "snap"
    assert f["v"] == PROTOCOL_VERSION
    assert f["ts"] == "2026-09-05T10:21:30Z"
    assert isinstance(f["a"], list)
    # Nothing extra in the common case: no overflow, no failure reason.
    assert "more" not in f and "why" not in f


def test_row_has_no_glyph_field():
    # The device derives its glyph from `s`. Two sources of truth for the
    # same thing is how a strip ends up disagreeing with its own detail pane.
    row = FrameBuilder(now=Clock()).build(HerdSnapshot(agents=(agent(),)))["a"][0]
    assert "g" not in row
    assert row["s"] == "idle"


def test_blocked_rows_carry_question_and_decision_id():
    b = FrameBuilder(now=Clock())
    snap = HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED, seq=441),))
    f = b.build(snap, prompts={"w9:p1": "Allow Bash(git push --force origin main)?"})
    row = f["a"][0]
    assert row["q"].startswith("Allow Bash(git push")
    assert len(row["r"]) == 12
    assert "x" not in row  # short enough to show in full


def test_truncated_prompt_is_flagged_on_the_row():
    # The action layer refuses to approve a truncated prompt, so this flag is
    # load-bearing, not cosmetic.
    b = FrameBuilder(now=Clock())
    snap = HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED),))
    f = b.build(snap, prompts={"w9:p1": "Allow Bash(" + "x" * 400 + ")?"})
    assert f["a"][0]["x"] is True


def test_blocked_without_prompt_text_still_renders_blocked():
    f = FrameBuilder(now=Clock()).build(
        HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED),))
    )
    row = f["a"][0]
    assert row["s"] == "blocked"
    assert "q" not in row and "r" not in row


def test_non_blocked_rows_never_carry_a_prompt():
    f = FrameBuilder(now=Clock()).build(
        HerdSnapshot(agents=(agent("w2:p1", AgentStatus.IDLE),)),
        prompts={"w2:p1": "stale question"},
    )
    assert "q" not in f["a"][0]


# ------------------------------------------------------------ ordering


def test_blocked_first_then_done_then_the_rest():
    b = FrameBuilder(now=Clock())
    snap = HerdSnapshot(agents=(
        agent("w2:p1", AgentStatus.IDLE),
        agent("w5:p1", AgentStatus.WORKING),
        agent("w7:p1", AgentStatus.DONE),
        agent("w9:p1", AgentStatus.BLOCKED),
    ))
    got = [r["s"] for r in b.build(snap)["a"]]
    assert got == ["blocked", "done", "working", "idle"]


def test_longest_waiting_first_within_a_status():
    clock = Clock()
    b = FrameBuilder(now=clock)
    # Both panes are known to the builder before anything blocks, so the
    # transitions below are witnessed rather than first sightings.
    b.build(HerdSnapshot(agents=(
        agent("w2:p1", AgentStatus.IDLE, seq=1),
        agent("w9:p1", AgentStatus.IDLE, seq=1),
    )))
    # w9 blocks first...
    b.build(HerdSnapshot(agents=(
        agent("w2:p1", AgentStatus.IDLE, seq=1),
        agent("w9:p1", AgentStatus.BLOCKED, seq=2),
    )))
    clock.advance(300)
    # ...then w2 blocks, five minutes later.
    snap = HerdSnapshot(agents=(
        agent("w2:p1", AgentStatus.BLOCKED, seq=2),
        agent("w9:p1", AgentStatus.BLOCKED, seq=2),
    ))
    assert [r["i"] for r in b.build(snap)["a"]] == ["w9:p1", "w2:p1"]


# ------------------------------------------------------------ timestamps


def test_first_sighting_omits_the_duration_rather_than_guessing():
    # Herdr reports no timestamp with state_change_seq. On startup an agent
    # blocked twenty minutes ago looks identical to one blocked this second,
    # so claiming "waiting 0m" would be a wrong duration on the one screen
    # that must not lie. No duration is honest; a fabricated one is not.
    b = FrameBuilder(now=Clock())
    row = b.build(HerdSnapshot(agents=(agent(seq=5),)))["a"][0]
    assert "e" not in row


def test_witnessed_transition_carries_an_absolute_timestamp():
    # `e` is a timestamp, not an elapsed count, so the device never ticks its
    # own clock and cannot drift.
    clock = Clock()
    b = FrameBuilder(now=clock)
    b.build(HerdSnapshot(agents=(agent(seq=5),)))   # first sighting
    clock.advance(120)
    row = b.build(HerdSnapshot(agents=(agent(seq=6),)))["a"][0]
    assert row["e"] == "2026-09-05T10:23:30Z"


def test_duration_holds_steady_while_the_state_does():
    clock = Clock()
    b = FrameBuilder(now=clock)
    b.build(HerdSnapshot(agents=(agent(seq=5),)))
    clock.advance(60)
    b.build(HerdSnapshot(agents=(agent(seq=6),)))   # witnessed
    clock.advance(120)
    row = b.build(HerdSnapshot(agents=(agent(seq=6),)))["a"][0]
    assert row["e"] == "2026-09-05T10:22:30Z"       # unchanged: same seq


# -------------------------------------------------------- failure path


def test_failed_snapshot_marks_last_known_agents_unknown():
    # Issue 4A. Not the last good frame, and not an empty herd — either would
    # be a lie, just about different things.
    b = FrameBuilder(now=Clock())
    b.build(HerdSnapshot(agents=(
        agent("w2:p1", AgentStatus.WORKING),
        agent("w9:p1", AgentStatus.BLOCKED),
    )))
    f = b.build(HerdSnapshot(ok=False, reason="cannot run herdr: [WinError 2]"))
    assert [r["s"] for r in f["a"]] == ["unknown", "unknown"]
    assert {r["i"] for r in f["a"]} == {"w2:p1", "w9:p1"}
    assert "cannot run herdr" in f["why"]


def test_failed_snapshot_before_any_success_yields_an_empty_herd():
    f = FrameBuilder(now=Clock()).build(HerdSnapshot(ok=False, reason="boom"))
    assert f["a"] == []
    assert f["why"] == "boom"


def test_recovery_after_failure_restores_real_statuses():
    b = FrameBuilder(now=Clock())
    b.build(HerdSnapshot(agents=(agent("w2:p1", AgentStatus.WORKING),)))
    b.build(HerdSnapshot(ok=False, reason="gone"))
    f = b.build(HerdSnapshot(agents=(agent("w2:p1", AgentStatus.IDLE),)))
    assert f["a"][0]["s"] == "idle"
    assert "why" not in f


# --------------------------------------------------------- overflow/size


def test_overflow_is_reported_not_silently_dropped():
    # Reinstated against the review's decision to drop it: that call rested on
    # "you have exactly five agents, so it is always zero", and a sixth
    # workspace appeared the same afternoon.
    agents = tuple(agent(f"w{i}:p1", AgentStatus.IDLE) for i in range(MAX_AGENTS + 3))
    f = FrameBuilder(now=Clock()).build(HerdSnapshot(agents=agents))
    assert len(f["a"]) == MAX_AGENTS
    assert f["more"] == 3


def test_no_overflow_key_when_everything_fits():
    agents = tuple(agent(f"w{i}:p1") for i in range(3))
    assert "more" not in FrameBuilder(now=Clock()).build(HerdSnapshot(agents=agents))


def test_encoded_frame_fits_the_device_receive_ring():
    # The device's RX ring is 2048 bytes and it has no PSRAM. A full herd with
    # two prompts in flight is the realistic worst case.
    agents = []
    for i in range(MAX_AGENTS):
        st = AgentStatus.BLOCKED if i < 2 else AgentStatus.WORKING
        agents.append(agent(f"w{i}:p1", st, cwd=r"C:\.workspaces\a-fairly-long-repo-name"))
    prompts = {f"w{i}:p1": "Allow Bash(" + "y" * 300 + ")?" for i in range(2)}
    b = FrameBuilder(now=Clock())
    blob = b.encode(b.build(HerdSnapshot(agents=tuple(agents)), prompts))
    assert len(blob) < 2048, len(blob)
    assert blob.endswith(b"\n") and blob.count(b"\n") == 1


def test_encoding_is_valid_json_and_keeps_unicode_readable():
    b = FrameBuilder(now=Clock())
    f = b.build(
        HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED),)),
        prompts={"w9:p1": "Allow Bash(git push)? " + "z" * 200},
    )
    decoded = json.loads(b.encode(f).decode("utf-8"))
    assert decoded["a"][0]["q"].endswith("…")  # not \u2026 double-escaped


# ------------------------------------------------------------- the recap


def test_the_recap_is_the_agents_own_summary_not_ours():
    # terminal_title_stripped is what Claude Code writes about itself, so the
    # detail screen quotes the agent rather than guessing from a cwd.
    b = FrameBuilder(now=Clock())
    frame = b.build(HerdSnapshot(agents=(
        agent("w2:p1", title="PriceComponentManager_Client migration to React"),
    )))
    assert frame["a"][0]["d"] == "PriceComponentManager_Client migration to React"


def test_an_agent_with_nothing_to_say_carries_no_recap_field():
    # Absent beats empty: the device tests for the field, and an empty string
    # would make it draw a heading over nothing.
    b = FrameBuilder(now=Clock())
    frame = b.build(HerdSnapshot(agents=(agent("w2:p1", title=""),)))
    assert "d" not in frame["a"][0]


def test_a_long_recap_is_cut_silently():
    # Unlike a prompt, nothing is approved against a recap, so a cut one
    # needs no flag and must not make the agent unanswerable.
    long = " ".join(["migration"] * 40)
    assert len(truncate_recap(long)) == RECAP_MAX
    # ASCII, not U+2026: the device font has no glyph for the ellipsis.
    assert truncate_recap(long).endswith("...")
    assert truncate_recap("  spaced   out  ") == "spaced out"
    assert truncate_recap(None) == ""


def test_the_worst_case_frame_fits_the_device_line_buffer():
    # The bound that actually matters: the device reassembles one JSON line at
    # a time, and a line that does not fit is discarded - a stale screen, not
    # an error.
    #
    # This test used to omit the option block and assert < 4096, and both were
    # wrong. Without options a full herd is about 3KB, so it passed happily
    # while the REAL worst case - every agent blocked, each carrying
    # MAX_OPTIONS labels at OPTION_MAX - is 6317 bytes and silently overflowed
    # the 4096-byte buffer the comment named. The buffer is SHEPHERD_LINE_MAX
    # (8192) in firmware/src/line_buf.h, which carries the same number and a
    # static_assert against it.
    b = FrameBuilder(now=Clock())
    agents = tuple(
        agent(f"w{i:02d}:pane{i:02d}", status=AgentStatus.BLOCKED, seq=i,
              cwd=rf"C:\.workspaces\some-fairly-long-workspace-name-{i}",
              title="x" * RECAP_MAX)
        for i in range(MAX_AGENTS)
    )
    prompts = {a.pane_id: "y" * 400 for a in agents}
    options = {
        a.pane_id: tuple(("O" * OPTION_MAX, "w") for _ in range(MAX_OPTIONS))
        for a in agents
    }
    b.build(HerdSnapshot(agents=agents), prompts, options)   # seed _seen
    payload = b.encode(b.build(HerdSnapshot(agents=agents), prompts, options))

    # Keep this assertion in step with SHEPHERD_LINE_MAX. If the frame grows a
    # field, re-measure and raise both together - do not relax this alone.
    assert len(payload) < 8192, len(payload)
    # And a lower bound, so nobody can make this pass by accidentally
    # shrinking the worst case out from under the firmware's assert.
    assert len(payload) > 6000, (
        f"the worst case shrank to {len(payload)}; re-measure and update "
        f"SHEPHERD_WORST_FRAME in firmware/src/line_buf.h"
    )


# ------------------------------------------------------------ said frames


def test_a_said_frame_is_its_own_type_not_a_detail():
    # The point of decision 4 in the plan review. `deet` means "the body you
    # asked for" and the device applies one unconditionally, resetting scroll
    # and restarting the type-out - so an unsolicited one would yank the
    # screen. A distinct `t` makes Shepherd ignore it instead.
    said = FrameBuilder(now=Clock()).said_frame("w1:p1", "All done.")
    deet = FrameBuilder.detail_frame("w1:p1", "All done.")
    assert said["t"] == "said"
    assert deet["t"] == "deet"
    assert said["t"] != deet["t"]
    assert said["v"] == PROTOCOL_VERSION
    assert said["i"] == "w1:p1"
    assert said["b"] == "All done."


def test_a_said_frame_with_nothing_to_say_is_still_a_valid_frame():
    said = FrameBuilder(now=Clock()).said_frame("w1:p1", "")
    assert said["b"] == ""
    assert said["i"] == "w1:p1"


def test_a_said_frame_keeps_the_end_of_a_long_answer():
    tail = "and here is what it concluded."
    said = FrameBuilder(now=Clock()).said_frame("w1:p1", ("filler " * 200) + tail)
    assert len(said["b"]) <= SAID_MAX
    assert said["b"].endswith(tail)
    assert said["b"].startswith("...")


def test_a_said_frame_is_much_smaller_than_a_detail_frame():
    # A bubble cannot scroll, so anything past what fits is weight on the wire
    # for text nobody will read.
    long_text = "z" * 2000
    assert len(FrameBuilder(now=Clock()).said_frame("w1:p1", long_text)["b"]) <= SAID_MAX
    assert len(FrameBuilder.detail_frame("w1:p1", long_text)["b"]) > SAID_MAX
