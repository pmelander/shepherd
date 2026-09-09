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
from shepherd.transport import FakeTransport, TransportError  # noqa: E402

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


async def _spin(*coros, ticks=0.05):
    """Run some loops briefly, then cancel them.

    Re-raises anything that is not a cancellation, and that is not a detail:
    the first version of this helper used gather(return_exceptions=True) and
    swallowed a NameError, so a test asserting that a loop kept running passed
    while that loop was actually dead on its first line. A helper that hides
    exceptions turns every test built on it into a test of nothing.
    """
    tasks = [asyncio.create_task(c) for c in coros]
    await asyncio.sleep(ticks)
    for t in tasks:
        t.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException) and not isinstance(
            res, asyncio.CancelledError
        ):
            raise res


def test_push_loop_sends_what_the_herd_loop_built():
    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    t = FakeTransport()
    r, clock, _ = make(src, transport=t, tick=1.0, keepalive=10.0,
                       poll_interval=30.0, require_signatures=False)
    gate = _Gate()

    run(_spin(r._herd_loop(), r._push_loop(t, gate)))

    # At least the first frame goes out, and the gate is told about it.
    assert len(t.sent) >= 1
    assert gate.frames and gate.frames[0][0] == {"w2:p1"}
    # The gate is told which frame it was, so a signed action can be bound to
    # a frame the relay actually sent.
    assert gate.frames[0][2], "observe_frame must carry the frame timestamp"


def test_the_herd_is_watched_with_no_transport_at_all():
    # THE point of separating the loops. This used to be impossible: polling
    # lived inside the connect/serve loop, so no device meant no frames, and
    # any second consumer of the stream saw nothing whenever the Cardputer
    # was away.
    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    r, clock, _ = make(src, tick=1.0, keepalive=10.0, poll_interval=30.0,
                       require_signatures=False)

    run(_spin(r._herd_loop()))

    assert src.list_calls >= 1, "the herd was never polled"
    assert r._latest is not None, "no frame was built without a device"
    assert r._latest.gen >= 1
    assert r._latest.payload.endswith(b"\n")
    assert [row["i"] for row in r._latest.frame["a"]] == ["w2:p1"]


def test_a_transport_that_never_connects_does_not_stop_the_herd_loop():
    # The regression test for the actual bug: a relay whose device is off
    # must still watch the herd. Every connect attempt fails, forever.
    class DeadTransport:
        def __init__(self):
            self.sent = []
            self.attempts = 0

        async def connect(self):
            self.attempts += 1
            raise TransportError("no device advertising")

        async def close(self):
            pass

    dead = DeadTransport()
    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    r, clock, _ = make(src, transport=dead, tick=1.0,
                       keepalive=10.0, poll_interval=30.0,
                       require_signatures=False)

    run(_spin(r._herd_loop(), r._transport_loop()))

    # Without this the test would pass even if the transport loop never ran,
    # which is precisely how the first version of it passed while dying on a
    # NameError.
    assert dead.attempts >= 1, "the transport loop never tried to connect"
    assert src.list_calls >= 1, "a dead radio silenced the herd poll"
    assert r._latest is not None, "a dead radio stopped frames being built"


def test_frames_are_published_with_no_device_connected(tmp_path):
    # The two halves of the split, together: the herd loop builds without a
    # transport, and the tee publishes what it builds. This is the behaviour
    # Bruno actually depends on.
    from shepherd.publish import STREAM_NAME, FramePublisher

    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    publisher = FramePublisher(path=tmp_path / STREAM_NAME)
    r, clock, _ = make(src, tick=1.0, keepalive=10.0, poll_interval=30.0,
                       require_signatures=False, publisher=publisher)

    run(_spin(r._herd_loop()))

    written = publisher.path.read_text(encoding="utf-8").splitlines()
    assert written, "nothing was published"
    first = json.loads(written[0])
    assert first["t"] == "snap"
    assert [row["i"] for row in first["a"]] == ["w2:p1"]


def test_what_is_published_is_byte_identical_to_what_the_device_is_sent(tmp_path):
    from shepherd.publish import STREAM_NAME, FramePublisher

    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    t = FakeTransport()
    publisher = FramePublisher(path=tmp_path / STREAM_NAME)
    r, clock, _ = make(src, transport=t, tick=1.0, keepalive=10.0,
                       poll_interval=30.0, require_signatures=False,
                       publisher=publisher)
    gate = _Gate()

    run(_spin(r._herd_loop(), r._push_loop(t, gate)))

    sent = b"".join(t.sent)
    published = publisher.path.read_bytes()
    # The device may be a frame behind, since the push loop sends on its own
    # tick, so the published stream must START with what was sent.
    assert sent, "nothing was sent"
    assert published.startswith(sent), (
        "the stream diverged from what the device received"
    )


def test_a_publisher_that_explodes_does_not_stop_the_relay(tmp_path, caplog):
    # The tee is additive. If it fails, the device must carry on exactly as
    # before - that is the whole reason it is a tee and not a fan-out.
    class Exploding:
        def __init__(self):
            self.calls = 0

        def publish(self, frame, payload):
            self.calls += 1
            raise RuntimeError("disk on fire")

    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    t = FakeTransport()
    boom = Exploding()
    r, clock, _ = make(src, transport=t, tick=1.0, keepalive=10.0,
                       poll_interval=30.0, require_signatures=False,
                       publisher=boom)
    gate = _Gate()

    with caplog.at_level("WARNING", logger="shepherd"):
        run(_spin(r._herd_loop(), r._push_loop(t, gate)))

    assert boom.calls >= 1, "the publisher was never called"
    assert len(t.sent) >= 1, "a broken tee stopped the device being served"
    warnings = [r for r in caplog.records
                if r.levelname == "WARNING" and "publisher raised" in r.getMessage()]
    assert len(warnings) == 1, (
        f"one line per outage, not one per frame (got {len(warnings)} for "
        f"{boom.calls} calls)"
    )


def test_the_publisher_never_sees_a_rekey_frame(tmp_path):
    # Structural, not a matter of the allowlist doing its job: the rekey frame
    # is sent from _rotate() on the transport path and the tee only ever sees
    # what the herd loop builds. Both guards exist; this pins the outer one.
    from shepherd.publish import STREAM_NAME, FramePublisher

    seen: list[str] = []

    class Watching(FramePublisher):
        def publish(self, frame, payload):
            seen.append(frame.get("t"))
            return super().publish(frame, payload)

    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    r, clock, _ = make(src, tick=1.0, keepalive=10.0, poll_interval=30.0,
                       require_signatures=False,
                       publisher=Watching(path=tmp_path / STREAM_NAME))

    run(_spin(r._herd_loop()))

    assert seen, "the publisher was never called"
    assert set(seen) == {"snap"}, f"the tee saw more than snapshots: {set(seen)}"


def test_the_push_loop_does_not_poll_the_herd_itself():
    # The inverse of the separation, and the assertion that would fail if
    # anyone moved polling back into the connect/serve path. Run the push loop
    # with no herd loop beside it: it must find nothing to send and must not
    # go asking Herdr on its own.
    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    t = FakeTransport()
    r, clock, _ = make(src, transport=t, tick=1.0, keepalive=10.0,
                       poll_interval=30.0, require_signatures=False)
    gate = _Gate()

    run(_spin(r._push_loop(t, gate)))

    assert src.list_calls == 0, "the push loop polled the herd"
    assert t.sent == [], "the push loop invented a frame to send"
    assert gate.frames == []


def test_a_late_connection_gets_the_current_frame_on_its_first_tick():
    # A device that connects after the relay has been running must not wait
    # for the next change or keepalive to have something on screen. Same path
    # a reconnect takes.
    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    t = FakeTransport()
    r, clock, _ = make(src, transport=t, tick=1.0, keepalive=10.0,
                       poll_interval=30.0, require_signatures=False)
    gate = _Gate()

    async def scenario():
        # Herd loop runs alone first: a frame exists, nothing has been sent.
        await _spin(r._herd_loop())
        assert r._latest is not None
        assert t.sent == []
        # Now a device turns up.
        await _spin(r._push_loop(t, gate))

    run(scenario())
    assert len(t.sent) >= 1, "a late connection was not sent the current frame"


def test_the_push_loop_does_not_resend_an_unchanged_generation():
    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    t = FakeTransport()
    r, clock, _ = make(src, transport=t, tick=1.0, keepalive=10.0,
                       poll_interval=30.0, require_signatures=False)
    gate = _Gate()

    async def scenario():
        await _spin(r._herd_loop())          # exactly one build
        gen = r._latest.gen
        await _spin(r._push_loop(t, gate))   # many ticks, one generation
        return gen

    gen = run(scenario())
    assert gen == r._latest.gen, "the herd loop rebuilt while parked"
    assert len(t.sent) == 1, f"resent an unchanged frame: {len(t.sent)} sends"


def test_the_keepalive_rebuild_cycles_the_replay_window():
    # The keepalive is not only about looking alive. The gate keeps the last
    # six frame timestamps and refuses any action whose ts is not among them,
    # which is the only replay protection `focus` has. Holding one frame and
    # resending it would leave a captured frame replayable until six
    # unrelated changes pushed it out.
    from datetime import datetime, timedelta, timezone

    from shepherd.frame import FrameBuilder

    src = FakeSource([HerdSnapshot(agents=(agent(seq=1),))])
    r, clock, _ = make(src, tick=1.0, keepalive=10.0, poll_interval=30.0,
                       require_signatures=False)
    # The builder stamps frames from wall-clock time at one-second resolution,
    # so two builds in the same real millisecond share a ts and the assertion
    # below would be vacuous. Tie the builder's clock to the fake one instead,
    # which is what makes this test about the rebuild rather than about how
    # fast the machine is.
    epoch = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    r.builder = FrameBuilder(now=lambda: epoch + timedelta(seconds=clock.t))

    async def scenario():
        task = asyncio.create_task(r._herd_loop())
        await asyncio.sleep(0)
        first = None
        # The fake clock only advances when the loop sleeps, so this walks
        # past one keepalive boundary.
        for _ in range(30):
            await asyncio.sleep(0)
            if first is None and r._latest is not None:
                first = r._latest
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return first

    first = run(scenario())
    assert first is not None
    assert r._latest.gen > first.gen, "the keepalive never rebuilt"
    assert r._latest.frame["ts"] != first.frame["ts"], (
        "a keepalive rebuild reused the timestamp, so the gate's replay "
        "window would stop cycling"
    )


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
