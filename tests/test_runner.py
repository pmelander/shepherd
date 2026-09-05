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


# ------------------------------------------------------------- push loop


class _Gate:
    def __init__(self):
        self.frames = []

    def observe_frame(self, pane_ids, pending):
        self.frames.append((set(pane_ids), dict(pending)))


def test_push_loop_sends_on_change_then_holds():
    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    t = FakeTransport()
    r, clock, _ = make(src, transport=t, tick=1.0, keepalive=10.0, poll_interval=30.0)
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
