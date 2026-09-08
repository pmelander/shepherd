"""Runner coverage: the composition, not the components.

Everything here uses fakes and a fake clock, so the loop's timing rules —
push on change, keepalive floor, poll interval, prompt caching — are pinned
without waiting real seconds or attaching a radio.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.models import Agent, AgentStatus, HerdSnapshot  # noqa: E402
from shepherd.runner import Runner  # noqa: E402
from shepherd.transport import FakeTransport  # noqa: E402

BLOCKED_PANE = """
 Do you want to proceed?
 ❯ 1. Yes
   2. No

 Esc to cancel
"""


def agent(pane="w2:p1", status=AgentStatus.IDLE, seq=1):
    return Agent(pane, pane.split(":")[0], status, seq,
                 rf"C:\.workspaces\repo-{pane[1]}", "t", "claude", False)


class FakeSource:
    def __init__(self, snapshots=None, pane_text=BLOCKED_PANE):
        self.snapshots = list(snapshots or [])
        self.pane_text = pane_text
        self.list_calls = 0
        self.read_calls: list[str] = []
        self.sent: list[tuple[str, list[str]]] = []

    async def list_agents(self):
        self.list_calls += 1
        if self.snapshots:
            return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        return HerdSnapshot(agents=(agent(),))

    async def read_pane(self, pane_id, lines=40):
        self.read_calls.append(pane_id)
        return self.pane_text

    async def send_keys(self, pane_id, keys):
        self.sent.append((pane_id, list(keys)))

    async def focus(self, pane_id):
        pass


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(source, transport=None, **kw):
    clock = Clock()
    slept: list[float] = []

    async def sleep(d):
        slept.append(d)
        clock.t += d
        # Must actually yield. A coroutine that returns without awaiting
        # never hands control back, so the loop under test spins forever and
        # the test hangs rather than fails - which is how this was found.
        await asyncio.sleep(0)

    r = Runner(source=source, transport_factory=lambda: transport or FakeTransport(),
               clock=clock, sleep=sleep, use_events=False, **kw)
    return r, clock, slept


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ herd state


def test_refresh_marks_dirty_when_a_seq_moves():
    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    r, _, _ = make(src)
    run(r.refresh())
    r._dirty = False
    src.snapshots = [HerdSnapshot(agents=(agent(seq=2),))]
    run(r.refresh())
    assert r._dirty


def test_refresh_is_not_dirty_when_nothing_moved():
    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    r, _, _ = make(src)
    run(r.refresh())
    r._dirty = False
    run(r.refresh())
    assert not r._dirty


def test_an_agent_vanishing_is_a_change():
    # Otherwise the device keeps offering to answer a pane that is gone.
    src = FakeSource([HerdSnapshot(agents=(agent("w2:p1"), agent("w9:p1")))])
    r, _, _ = make(src)
    run(r.refresh())
    r._dirty = False
    src.snapshots = [HerdSnapshot(agents=(agent("w2:p1"),))]
    run(r.refresh())
    assert r._dirty
    assert "w9:p1" not in r._seqs


def test_losing_herdr_is_dirty_and_so_is_recovering():
    src = FakeSource([HerdSnapshot(agents=(agent(),))])
    r, _, _ = make(src)
    run(r.refresh())
    r._dirty = False

    src.snapshots = [HerdSnapshot(ok=False, reason="cannot run herdr")]
    run(r.refresh())
    assert r._dirty, "the device must hear about a blind relay immediately"

    r._dirty = False
    src.snapshots = [HerdSnapshot(agents=(agent(),))]
    run(r.refresh())
    assert r._dirty, "recovering is worth a frame too"


# --------------------------------------------------------- prompt caching


def test_prompt_is_fetched_once_per_state_change():
    src = FakeSource([HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED, 5),))])
    r, _, _ = make(src)
    run(r.refresh())
    run(r.refresh())
    run(r.refresh())
    assert src.read_calls == ["w9:p1"], "re-reading every tick spawns a process a second"


def test_prompt_is_refetched_when_the_seq_advances():
    src = FakeSource([HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED, 5),))])
    r, _, _ = make(src)
    run(r.refresh())
    src.snapshots = [HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED, 6),))]
    run(r.refresh())
    assert src.read_calls == ["w9:p1", "w9:p1"]


def test_prompt_is_dropped_when_the_agent_unblocks():
    src = FakeSource([HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED, 5),))])
    r, _, _ = make(src)
    run(r.refresh())
    assert "w9:p1" in r._prompts
    src.snapshots = [HerdSnapshot(agents=(agent("w9:p1", AgentStatus.IDLE, 6),))]
    run(r.refresh())
    assert "w9:p1" not in r._prompts


# ------------------------------------------------------------- frames


def test_build_produces_pending_decisions_for_the_gate():
    src = FakeSource([HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED, 5),))])
    r, _, _ = make(src)
    run(r.refresh())
    frame, pending = r.build()
    row = frame["a"][0]
    assert row["s"] == "blocked" and row["r"]
    assert list(pending) == [row["r"]]
    assert pending[row["r"]].pane_id == "w9:p1"
    assert pending[row["r"]].fingerprint, "gate needs something to compare against"


def test_build_has_no_pending_when_nothing_is_blocked():
    src = FakeSource([HerdSnapshot(agents=(agent(),))])
    r, _, _ = make(src)
    run(r.refresh())
    _, pending = r.build()
    assert pending == {}


# A real Claude Code pane, not a fixture: splash banner above, live token
# counter below, the question somewhere in the middle. Everything that broke
# "Y does nothing" is in the parts that are NOT the question.
def _real_pane(tokens: int) -> str:
    return f"""\
 ▐▛███▛█ Claude Code v2.1.261
 ▝▜██████▀ Opus 5 (1M context)

 cwd: C:\\scratch\\signtest

> Create a file called signed.txt containing the word ok.

 Write(signed.txt)
 ⎿  Writing 1 line

 Do you want to create signed.txt?
 ❯ 1. Yes
   2. Yes, and don't ask again this session
   3. No, and tell Claude what to do differently (esc)

 esc to interrupt · {tokens} tokens · 12.4s
"""


def test_the_frame_carries_the_parsed_question_not_the_pane_buffer():
    # This is the bug the device found: build() sent the whole detection
    # buffer as `q`, so the banner filled the screen, the length flagged the
    # prompt as truncated, and every blocked agent came out unanswerable —
    # the approve key did nothing, silently, on a genuinely blocked agent.
    src = FakeSource([HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED, 5),))],
                     pane_text=_real_pane(41_233))
    r, _, _ = make(src)
    run(r.refresh())
    frame, _ = r.build()
    row = frame["a"][0]

    assert row["q"] == "Do you want to create signed.txt?"
    assert "Claude Code" not in row["q"], "the splash banner is not the question"
    assert "tokens" not in row["q"], "the status line is not the question"
    assert not row.get("x"), "a short question must not arrive marked truncated"
    assert row["r"], "answerable rows need a decision id"


def test_the_decision_id_does_not_churn_while_the_token_counter_ticks():
    # The id is hashed from the question text, and the gate only honours ids
    # it has sent. Hashing the raw buffer would have minted a new id every
    # poll: the one on screen would go stale between reading it and pressing
    # a key, and every approve would be refused as unknown.
    src = FakeSource([HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED, 5),))],
                     pane_text=_real_pane(41_233))
    r, _, _ = make(src)
    run(r.refresh())
    first = r.build()[0]["a"][0]["r"]

    src.pane_text = _real_pane(58_907)
    r._prompts.clear()          # force a re-read, as a seq change would
    run(r._fetch_prompts())
    assert r.build()[0]["a"][0]["r"] == first


# ------------------------------------------------------------- push loop


class _Gate:
    def __init__(self):
        self.frames = []

    def observe_frame(self, pane_ids, pending, ts=None):
        self.frames.append((set(pane_ids), dict(pending), ts))


def test_push_loop_sends_on_change_then_holds():
    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    t = FakeTransport()
    r, clock, _ = make(src, transport=t, tick=1.0, keepalive=10.0,
                       poll_interval=30.0, require_signatures=False)
    gate = _Gate()

    async def scenario():
        task = asyncio.create_task(r._push_loop(t, gate))
        await asyncio.sleep(0.05)
        r.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())
    # At least the first frame goes out, and the gate is told about it.
    assert len(t.sent) >= 1
    assert gate.frames and gate.frames[0][0] == {"w2:p1"}
    # The gate is told which frame it was, so a signed action can be bound to
    # a frame the relay actually sent.
    assert gate.frames[0][2], "observe_frame must carry the frame timestamp"


def test_keepalive_is_below_the_device_staleness_threshold():
    from shepherd.runner import KEEPALIVE
    # The device declares NO SIGNAL after 30s. A keepalive at or above that
    # would make a healthy relay look dead.
    assert KEEPALIVE <= 15.0


def test_poll_interval_keeps_subprocess_count_sane():
    from shepherd.runner import POLL_INTERVAL
    # At 1Hz this is ~86k process spawns per working day on a laptop already
    # hosting five agents.
    per_working_day = (8 * 60 * 60) / POLL_INTERVAL
    assert per_working_day < 2000


# ----------------------------------------------------------- action loop


def test_action_loop_dispatches_and_ignores_junk():
    good = json.dumps({"t": "act", "i": "w9:p1", "k": "deny", "r": "abc123"})
    t = FakeTransport(incoming=["not json", "{}", '{"t":"snap"}', good])
    src = FakeSource([HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED, 5),))])
    r, _, _ = make(src, transport=t)

    from shepherd.actions import ActionGate
    gate = ActionGate(source=src, audit=Path("nul") if sys.platform == "win32" else Path("/dev/null"))
    gate.observe_frame(frozenset({"w9:p1"}), {})

    run(r._action_loop(t, gate))
    assert src.sent == [("w9:p1", ["esc"])], "only the well-formed deny should act"
    assert r._dirty, "acting on the world makes the device's picture stale"


def test_action_loop_marks_dirty_on_a_refusal_that_needs_a_refresh():
    # A refusal carrying restale means what the device is showing is wrong.
    stale_req = json.dumps({"t": "act", "i": "w9:p1", "k": "approve", "r": "abc123"})
    t = FakeTransport(incoming=[stale_req])
    src = FakeSource([HerdSnapshot(agents=(agent("w9:p1", AgentStatus.BLOCKED, 5),))],
                     pane_text="nothing here\n")
    r, _, _ = make(src, transport=t)

    from shepherd.actions import ActionGate, PendingDecision, fingerprint
    gate = ActionGate(source=src, audit=Path("nul") if sys.platform == "win32" else Path("/dev/null"))
    gate.observe_frame(
        frozenset({"w9:p1"}),
        {"abc123": PendingDecision("w9:p1", fingerprint(BLOCKED_PANE), False)},
    )
    run(r._action_loop(t, gate))
    assert src.sent == [], "a changed prompt must not be answered"
    assert r._dirty


# --------------------------------------------------------------- watchdog


def test_liveness_is_none_without_the_env_var(monkeypatch):
    from shepherd.runner import herdr_liveness
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    assert herdr_liveness() is None


def test_liveness_reads_the_stamp(monkeypatch, tmp_path):
    from shepherd.runner import herdr_liveness
    sock = tmp_path / "herdr.sock"
    sock.write_text("1256:1788328813559367200", encoding="utf-8")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock))
    assert herdr_liveness() == "1256:1788328813559367200"


def test_liveness_is_none_when_the_file_is_gone(monkeypatch, tmp_path):
    from shepherd.runner import herdr_liveness
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "absent.sock"))
    assert herdr_liveness() is None


def test_watchdog_stops_the_relay_when_herdr_goes_away(monkeypatch, tmp_path):
    # Herdr does NOT kill plugin startup processes when a session stops -
    # found by stopping a test session and seeing the relay survive. Without
    # this, each Herdr restart leaves another orphan fighting for the device.
    from shepherd.runner import watch_herdr

    sock = tmp_path / "herdr.sock"
    sock.write_text("1256:111", encoding="utf-8")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock))

    src = FakeSource()
    r, _, _ = make(src)
    ticks = {"n": 0}

    async def sleep(_d):
        ticks["n"] += 1
        if ticks["n"] == 2:
            sock.unlink()          # session stopped
        await asyncio.sleep(0)

    run(watch_herdr(r, interval=0.01, sleep=sleep))
    assert r._stop, "relay must exit when its Herdr session ends"


def test_watchdog_stops_the_relay_when_herdr_restarts(monkeypatch, tmp_path):
    # A different pid:starttime means a new Herdr, whose own startup hook will
    # launch a fresh relay. This one should get out of its way.
    from shepherd.runner import watch_herdr

    sock = tmp_path / "herdr.sock"
    sock.write_text("1256:111", encoding="utf-8")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock))

    r, _, _ = make(FakeSource())
    ticks = {"n": 0}

    async def sleep(_d):
        ticks["n"] += 1
        if ticks["n"] == 2:
            sock.write_text("9999:222", encoding="utf-8")
        await asyncio.sleep(0)

    run(watch_herdr(r, interval=0.01, sleep=sleep))
    assert r._stop


def test_watchdog_is_a_noop_without_supervision(monkeypatch):
    from shepherd.runner import watch_herdr
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    r, _, _ = make(FakeSource())
    run(watch_herdr(r, interval=0.01))
    assert not r._stop, "running outside Herdr must not self-terminate"


# ------------------------------------------- outliving your own Herdr


def test_an_unreadable_stamp_does_not_disarm_the_watchdog(monkeypatch, tmp_path):
    """The two-day orphan.

    The relay is started BY Herdr, so racing Herdr's own write of the socket
    stamp is normal. The old code read the stamp once, got None, concluded
    "nobody is supervising me" and never looked again — so when that Herdr
    session ended the relay stayed up, held the single BLE link, and reported
    every agent as unknown. The device showed all-red for two days.
    """
    from shepherd.runner import watch_herdr

    sock = tmp_path / "herdr.sock"          # deliberately does NOT exist yet
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock))
    r, _, _ = make(FakeSource())
    ticks = {"n": 0}

    async def sleep(_d):
        ticks["n"] += 1
        if ticks["n"] == 2:
            sock.write_text("111:222", encoding="utf-8")   # Herdr finishes
        if ticks["n"] == 5:
            sock.write_text("999:888", encoding="utf-8")   # and later restarts
        await asyncio.sleep(0)

    run(watch_herdr(r, interval=0.01, sleep=sleep))
    assert r._stop, "must have noticed the session change it waited for"


def test_a_missing_env_var_is_still_unsupervised(monkeypatch):
    # The other half of the distinction: started by hand, nobody to watch.
    from shepherd.runner import watch_herdr

    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    r, _, _ = make(FakeSource())
    run(watch_herdr(r, interval=0.01))
    assert not r._stop


def test_a_herdr_outage_is_logged_once_and_then_gives_up():
    # A relay whose Herdr has gone sends honest, useless frames while holding
    # the one BLE link, so a healthy replacement cannot take over. It exits.
    from shepherd.runner import HERDR_GONE_AFTER

    src = FakeSource([HerdSnapshot(agents=(), ok=False, reason="cannot run herdr")])
    r, clock, _ = make(src)

    run(r.refresh())
    assert r._herd_failing_since is not None
    assert not r._stop, "one failed poll is not an outage"

    clock.t += HERDR_GONE_AFTER - 1
    run(r.refresh())
    assert not r._stop, "still inside the grace window"

    clock.t += 2
    run(r.refresh())
    assert r._stop, "should have given up and freed the device"


def test_recovering_clears_the_outage():
    from shepherd.runner import HERDR_GONE_AFTER

    ok = HerdSnapshot(agents=(agent(),))
    bad = HerdSnapshot(agents=(), ok=False, reason="cannot run herdr")
    src = FakeSource([bad])
    r, clock, _ = make(src)

    run(r.refresh())
    assert r._herd_failing_since is not None

    src.snapshots = [ok]
    run(r.refresh())
    assert r._herd_failing_since is None
    # And the clock having moved past the limit must not then trip it.
    clock.t += HERDR_GONE_AFTER * 2
    run(r.refresh())
    assert not r._stop
