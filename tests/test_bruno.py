"""Bruno the follower: the opt-in, the supervision, and what reaches the wire.

No board is needed for any of this. The stream is a file, the serial port is a
fake that records what it was handed, and the clock is fake too - which is the
whole reason the design step said to write this against a hand-written frames
file before the hardware arrives.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

import bruno as B  # noqa: E402
from shepherd.frame import FrameBuilder  # noqa: E402
from shepherd.singleton import AlreadyRunning, acquire  # noqa: E402

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def at(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def snap(n=1, ts=None):
    return {"t": "snap", "v": 3, "ts": ts or at(0), "a": [], "n": n}


def said(pane="w1:p1", body="done", ts=None):
    return {"t": "said", "v": 3, "ts": ts or at(0), "i": pane, "b": body}


def lines(*frames):
    return [json.dumps(f) for f in frames]


def kinds(payloads):
    return [json.loads(p.decode("utf-8"))["t"] for p in payloads]


class FakeSerial:
    def __init__(self, fail_after=None):
        self.port = "COM-fake"
        self.written: list[bytes] = []
        self.closed = False
        self.fail_after = fail_after

    def write(self, payload):
        if self.fail_after is not None and len(self.written) >= self.fail_after:
            raise OSError("the board went away")
        self.written.append(payload)

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def fake_sleep_for(clock):
    async def sleep(d):
        clock.t += d
        await asyncio.sleep(0)
    return sleep


# ============================================================= the opt-in


def test_no_port_file_means_bruno_is_not_wanted_here(monkeypatch, tmp_path):
    # The whole opt-in, and the reason a Shepherd user who never buys a Core
    # must not notice Bruno exists: one log line, exit 0, nothing held.
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    assert B.read_port() is None


def test_an_empty_port_file_means_auto_detect(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / B.PORT_FILE).write_text("\n\n", encoding="utf-8")
    # "" not None: present-but-blank is a request, absent is a refusal.
    assert B.read_port() == ""


def test_a_port_file_is_read_past_comments_and_whitespace(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / B.PORT_FILE).write_text(
        "# the desk companion\n\n  COM7  # bottom usb port\n", encoding="utf-8")
    assert B.read_port() == "COM7"


def test_the_opt_in_lives_in_config_and_the_lock_in_state(monkeypatch, tmp_path):
    # Config describes how to run and a human creates it; state describes this
    # run and must never end up in a repo or a backup.
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    assert B.port_file().parent == tmp_path / "cfg"
    assert B.lock_path().parent == tmp_path / "state"


def test_brunos_lock_is_not_the_relays(monkeypatch, tmp_path):
    # They contend for different hardware. If they shared a lock file, whoever
    # started second would refuse to run and the other device would go dark.
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    from shepherd import singleton

    assert B.lock_path() != singleton.lock_path()

    relay = acquire(path=singleton.lock_path())
    try:
        mine = acquire(path=B.lock_path())      # must NOT be refused
        mine.close()
    finally:
        relay.close()


def test_a_second_bruno_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    first = acquire(path=B.lock_path())
    try:
        with pytest.raises(AlreadyRunning):
            acquire(path=B.lock_path())
    finally:
        first.close()


# ============================================================ supervision


def test_the_watchdog_stops_bruno_when_the_session_changes(monkeypatch, tmp_path):
    stamp = tmp_path / "sock"
    stamp.write_text("111:222", encoding="utf-8")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(stamp))
    bruno = B.Bruno(port="COM1", stream=tmp_path / "frames.ndjson")

    async def scenario():
        async def sleep(d):
            stamp.write_text("999:888", encoding="utf-8")   # session replaced
            await asyncio.sleep(0)
        await B.watch_session(bruno, sleep=sleep)

    run(scenario())
    assert bruno._stop, "an orphan survived its Herdr session"


def test_the_watchdog_is_a_noop_when_unsupervised(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    bruno = B.Bruno(port="COM1", stream=tmp_path / "frames.ndjson")

    async def scenario():
        async def sleep(d):
            raise AssertionError("should not have waited on anything")
        await B.watch_session(bruno, sleep=sleep)

    run(scenario())
    assert not bruno._stop, "a hand-started Bruno stopped itself"


def test_an_unreadable_stamp_is_waited_for_not_treated_as_unsupervised(
        monkeypatch, tmp_path):
    # The bug this project already paid for once. Bruno is started BY Herdr,
    # so racing its startup is the normal order; treating a momentarily
    # missing stamp as "nobody is supervising me" is what left a relay running
    # for two days after its session ended.
    stamp = tmp_path / "sock"
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(stamp))
    bruno = B.Bruno(port="COM1", stream=tmp_path / "frames.ndjson")
    waits = {"n": 0}

    async def scenario():
        async def sleep(d):
            waits["n"] += 1
            if waits["n"] == 3:
                stamp.write_text("111:222", encoding="utf-8")   # Herdr arrives
            elif waits["n"] > 3:
                bruno.stop()
            await asyncio.sleep(0)
        await B.watch_session(bruno, sleep=sleep)

    run(scenario())
    assert waits["n"] >= 3, "it gave up before Herdr had written its stamp"


def test_an_unreachable_board_eventually_gives_up(monkeypatch, tmp_path):
    # The backstop, and it deliberately depends on no env plumbing being
    # right: a stuck follower holding nothing useful must make way.
    monkeypatch.setattr(B, "open_port", lambda *a, **k: (_ for _ in ()).throw(
        OSError("no such port")))
    monkeypatch.setattr(B, "candidates", lambda: [])
    bruno = B.Bruno(port="COM-nope", stream=tmp_path / "frames.ndjson")
    clock = Clock()

    rc = run(bruno.run(sleep=fake_sleep_for(clock), clock=clock))
    assert rc == 1, "it waited forever for a board that is not there"
    assert clock.t >= B.PORT_GONE_AFTER


# ============================================================== the follower


def write_lines(path: Path, *frames, mode="a"):
    with path.open(mode, encoding="utf-8") as fh:
        for f in frames:
            fh.write(json.dumps(f) + "\n")


def test_the_follower_starts_at_the_end(tmp_path):
    # A subscriber joining mid-session must not replay the whole file.
    p = tmp_path / "frames.ndjson"
    write_lines(p, snap(1), snap(2), said())
    f = B.Follower(p)
    assert f.poll() == [], "history was replayed on startup"
    write_lines(p, snap(3))
    assert len(f.poll()) == 1


def test_a_missing_stream_is_not_an_error(tmp_path):
    # Normal, twice over: the relay may not have started, and rotation leaves
    # the live file absent for an instant.
    f = B.Follower(tmp_path / "nope.ndjson")
    assert f.poll() == []
    assert f.poll() == []


def test_new_lines_are_returned_oldest_first(tmp_path):
    p = tmp_path / "frames.ndjson"
    p.write_text("", encoding="utf-8")
    f = B.Follower(p)
    f.poll()
    write_lines(p, snap(1), snap(2), snap(3))
    got = [json.loads(l)["n"] for l in f.poll()]
    assert got == [1, 2, 3]


def test_a_partial_trailing_line_is_held_not_forwarded(tmp_path):
    # The tee appends and this polls, with no coordination, so reading half a
    # frame is normal. Forwarding it would turn one good frame into two
    # malformed ones the device silently drops.
    p = tmp_path / "frames.ndjson"
    p.write_text("", encoding="utf-8")
    f = B.Follower(p)
    f.poll()

    full = json.dumps(snap(1))
    with p.open("a", encoding="utf-8") as fh:
        fh.write(full + "\n" + full[:20])       # one whole line, one fragment

    got = f.poll()
    assert len(got) == 1, f"a fragment was forwarded: {got}"
    assert f.partial == full[:20]

    # The rest arrives; now it is a whole line.
    with p.open("a", encoding="utf-8") as fh:
        fh.write(full[20:] + "\n")
    got = f.poll()
    assert len(got) == 1
    assert json.loads(got[0])["n"] == 1


def test_a_line_split_across_three_polls_is_reassembled(tmp_path):
    p = tmp_path / "frames.ndjson"
    p.write_text("", encoding="utf-8")
    f = B.Follower(p)
    f.poll()
    full = json.dumps(said(body="a sentence that arrived in pieces"))
    for piece in (full[:10], full[10:25], full[25:] + "\n"):
        with p.open("a", encoding="utf-8") as fh:
            fh.write(piece)
        got = f.poll()
    assert len(got) == 1
    assert json.loads(got[0])["b"] == "a sentence that arrived in pieces"


def test_a_rotation_is_noticed_and_read_from_the_start(tmp_path):
    # How the relay bounds the stream: rename to .1, open a fresh file. A
    # follower that missed the swap would keep reading from a stale offset.
    p = tmp_path / "frames.ndjson"
    write_lines(p, snap(1))
    f = B.Follower(p)
    f.poll()

    p.rename(tmp_path / "frames.ndjson.1")
    write_lines(p, snap(2), snap(3), mode="w")

    got = [json.loads(l)["n"] for l in f.poll()]
    assert got == [2, 3], f"the rotation was missed: {got}"


def test_a_truncated_stream_is_read_from_the_start(tmp_path):
    p = tmp_path / "frames.ndjson"
    write_lines(p, snap(1), snap(2))
    f = B.Follower(p)
    f.poll()
    write_lines(p, snap(9), mode="w")           # truncate in place
    got = [json.loads(l)["n"] for l in f.poll()]
    assert got == [9]


def test_nothing_new_means_nothing_returned(tmp_path):
    p = tmp_path / "frames.ndjson"
    write_lines(p, snap(1))
    f = B.Follower(p)
    f.poll()
    assert f.poll() == []
    assert f.poll() == []


# =========================================================== what goes out


def test_a_snapshot_and_an_announcement_both_go_out():
    out, dropped = select_now(snap(1), said())
    assert kinds(out) == ["snap", "said"]
    assert sum(dropped.values()) == 0


def select_now(*frames):
    return B.select(lines(*frames), NOW)


def test_superseded_snapshots_are_coalesced():
    # At 115200 baud a worst-case frame is about 550ms on the wire, so a busy
    # herd can outrun the cable. An older snapshot is worthless once a newer
    # one exists.
    out, dropped = select_now(snap(1), snap(2), snap(3))
    assert kinds(out) == ["snap"]
    assert json.loads(out[0])["n"] == 3, "kept the wrong snapshot"
    assert dropped["superseded"] == 2


def test_announcements_are_never_coalesced():
    # Getting this backwards would silently stop Bruno announcing the thing it
    # exists to announce.
    out, _ = select_now(said("w1:p1"), said("w2:p1"), said("w3:p1"))
    assert kinds(out) == ["said", "said", "said"]
    assert [json.loads(p)["i"] for p in out] == ["w1:p1", "w2:p1", "w3:p1"]


def test_order_is_preserved_among_survivors():
    out, _ = select_now(snap(1), said("w1:p1"), snap(2), said("w2:p1"))
    assert kinds(out) == ["said", "snap", "said"]
    assert json.loads(out[1])["n"] == 2


def test_a_rekey_frame_is_never_forwarded():
    # The relay's tee already refuses to publish one - it carries the shared
    # secret as plain hex - and this is the other half of the guard, on the
    # reading side. This process also reads a file a human could have edited.
    out, dropped = select_now({"t": "key", "v": 2, "k": "de" * 32,
                               "ts": at(0), "mac": "x"})
    assert out == []
    assert dropped["refused"] == 1


def test_an_unknown_frame_type_is_not_forwarded():
    out, dropped = select_now({"t": "telemetry", "v": 3, "ts": at(0)},
                              {"t": "", "v": 3, "ts": at(0)},
                              {"v": 3, "ts": at(0)})
    assert out == []
    assert dropped["refused"] == 3


def test_junk_lines_are_counted_not_fatal():
    out, dropped = B.select(["not json", "", "   ", "[1,2,3]",
                             json.dumps(snap(1))], NOW)
    assert kinds(out) == ["snap"]
    # Two unparsable: the junk string, and the JSON array that parses fine but
    # is not a frame. The blank lines are skipped rather than counted, because
    # a trailing newline is not a fault.
    assert dropped["unparsable"] == 2


# ================================================== staleness (T12)


def test_a_stale_snapshot_is_dropped_not_forwarded():
    # The device's staleness rule measures time since it last RECEIVED a
    # frame. Flushing buffered history after a reconnect would make Bruno look
    # perfectly current in front of a herd nobody is watching.
    out, dropped = select_now(snap(1, ts=at(B.MAX_FRAME_AGE + 5)))
    assert out == []
    assert dropped["stale"] == 1


def test_a_stale_announcement_is_dropped():
    # An hour-old "agent finished" is not an announcement, it is a recording.
    out, dropped = select_now(said(ts=at(3600)))
    assert out == []
    assert dropped["stale"] == 1


def test_a_fresh_frame_at_the_boundary_still_goes_out():
    out, dropped = select_now(snap(1, ts=at(B.MAX_FRAME_AGE - 1)))
    assert kinds(out) == ["snap"]
    assert dropped["stale"] == 0


def test_replaying_an_outage_forwards_only_the_current_state():
    # The realistic shape of a reconnect: a pile of history, then the present.
    old = [snap(i, ts=at(3600 - i)) for i in range(20)]
    recent_said = said("w1:p1", "just finished", ts=at(1))
    out, dropped = B.select(lines(*old, recent_said, snap(99)), NOW)
    assert kinds(out) == ["said", "snap"]
    assert json.loads(out[1])["n"] == 99
    assert dropped["stale"] == 20


def test_a_frame_with_no_timestamp_is_forwarded_not_assumed_stale():
    # Unknown is not stale. Discarding it would mean one format change
    # silently blanks the screen instead of showing something slightly old.
    out, dropped = select_now({"t": "snap", "v": 3, "a": []})
    assert kinds(out) == ["snap"]
    assert dropped["stale"] == 0


def test_a_malformed_timestamp_is_forwarded_not_assumed_stale():
    for bad in ("yesterday", "2026-09-10", "", None, 12345):
        out, dropped = select_now({"t": "snap", "v": 3, "ts": bad, "a": []})
        assert kinds(out) == ["snap"], bad
        assert dropped["stale"] == 0


def test_frame_age_reads_the_builders_own_format():
    # Guards the parser against the builder changing its timestamp format:
    # this uses a REAL frame rather than a hand-written one.
    real = FrameBuilder(now=lambda: NOW).said_frame("w1:p1", "done")
    assert B.frame_age(real, NOW) == pytest.approx(0.0, abs=1.0)
    assert B.frame_age(real, NOW + timedelta(seconds=90)) == pytest.approx(90.0)


# ================================================== end to end


def test_the_relays_real_stream_is_readable_by_the_real_follower(tmp_path):
    # The two halves have never met in any other test here: everything above
    # uses hand-written frames. This writes with the relay's own publisher and
    # reads with Bruno's own follower, so a change to either side's format
    # fails here rather than on a desk.
    from shepherd.publish import STREAM_NAME, FramePublisher

    pub = FramePublisher(path=tmp_path / STREAM_NAME)
    b = FrameBuilder(now=lambda: NOW)

    pub.path.write_text("", encoding="utf-8")
    follower = B.Follower(pub.path)
    follower.poll()                       # start at the end

    from shepherd.models import Agent, AgentStatus, HerdSnapshot

    a = Agent("w2:p1", "w2", AgentStatus.DONE, 7, r"C:\work\repo",
              "Don\u2019t stop \u2014 caf\u00e9", "claude", False)
    frame = b.build(HerdSnapshot(agents=(a,), ok=True))
    pub.publish(frame, b.encode(frame))

    announcement = b.said_frame("w2:p1", "PR #75587 open \u2014 your review next")
    pub.publish(announcement, b.encode(announcement))

    # And one the publisher must refuse, written through the same call.
    from shepherd.auth import rekey_frame

    key = rekey_frame(b"\x11" * 32, "2026-09-10T12:00:00Z", b"\xab" * 32)
    pub.publish(key, b.encode(key))

    got = follower.poll()
    out, dropped = B.select(got, NOW)

    assert kinds(out) == ["snap", "said"], kinds(out)
    assert sum(dropped.values()) == 0, dropped
    # The secret never even reached the follower, because the publisher
    # refused it: two guards, and this is the outer one working.
    assert all("ab" * 32 not in line for line in got)
    # UTF-8 survives writer, file, reader and re-encode.
    assert json.loads(out[1])["b"].endswith("your review next")
    assert "\u2014" in json.loads(out[1])["b"]


# ================================================== the write path


def test_frames_reach_the_port(monkeypatch, tmp_path):
    p = tmp_path / "frames.ndjson"
    p.write_text("", encoding="utf-8")
    fake = FakeSerial()
    monkeypatch.setattr(B, "open_port", lambda *a, **k: fake)

    bruno = B.Bruno(port="COM-fake", stream=p, poll_interval=0.01)
    clock = Clock()

    async def scenario():
        task = asyncio.create_task(bruno.run(sleep=fake_sleep_for(clock),
                                             clock=clock))
        await asyncio.sleep(0)
        write_lines(p, snap(1), said("w1:p1", "all done"))
        for _ in range(20):
            await asyncio.sleep(0)
        bruno.stop()
        await task

    run(scenario())
    assert kinds(fake.written) == ["snap", "said"], fake.written
    assert fake.closed, "the port was not released on the way out"


def test_losing_the_board_mid_write_reconnects(monkeypatch, tmp_path):
    p = tmp_path / "frames.ndjson"
    p.write_text("", encoding="utf-8")
    opened: list[FakeSerial] = []

    def opener(*a, **k):
        s = FakeSerial(fail_after=1 if not opened else None)
        opened.append(s)
        return s

    monkeypatch.setattr(B, "open_port", opener)
    bruno = B.Bruno(port="COM-fake", stream=p, poll_interval=0.01)
    clock = Clock()

    async def scenario():
        task = asyncio.create_task(bruno.run(sleep=fake_sleep_for(clock),
                                             clock=clock))
        await asyncio.sleep(0)
        write_lines(p, snap(1), snap(2), said("w1:p1", "x"))
        for _ in range(40):
            await asyncio.sleep(0)
        bruno.stop()
        await task

    run(scenario())
    assert len(opened) >= 2, "it did not reconnect after losing the board"
    assert opened[0].closed, "the dead handle was leaked"
