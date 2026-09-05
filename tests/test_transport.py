"""T9 coverage: chunking, reassembly, and backoff.

No radio involved. The hazards this layer actually has are arithmetic and
byte-boundary problems, and those are exactly the things a test can pin and a
hardware session cannot reliably reproduce.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.frame import FrameBuilder  # noqa: E402
from shepherd.models import Agent, AgentStatus, HerdSnapshot  # noqa: E402
from shepherd.transport import (  # noqa: E402
    ATT_OVERHEAD,
    MIN_MTU,
    NUS_RX,
    NUS_SERVICE,
    NUS_TX,
    BACKOFF_CAP,
    FakeTransport,
    LineAssembler,
    TransportError,
    backoff_delays,
    chunk_size,
    split_frame,
)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- constants


def test_nus_uuids_match_the_firmware():
    # Read off firmware/src/ble_bridge.cpp. If these drift, the device is
    # advertising a service the relay will never find.
    assert NUS_SERVICE == "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
    assert NUS_RX == "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
    assert NUS_TX == "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


# ----------------------------------------------------------------- chunking


def test_chunk_size_subtracts_att_overhead():
    assert chunk_size(247) == 247 - ATT_OVERHEAD
    assert chunk_size(185) == 182


def test_chunk_size_floors_when_mtu_is_unknown_or_absurd():
    # bleak exposes mtu_size read-only and only after a GATT session exists,
    # so None is a normal value, not an error. A write larger than the real
    # MTU is silently truncated, which is why the fallback is the floor and
    # not something optimistic.
    for bad in (None, 0, -1, 7, "247", 22):
        assert chunk_size(bad) == MIN_MTU - ATT_OVERHEAD, bad


def test_split_frame_covers_the_payload_exactly():
    payload = bytes(range(256))
    parts = split_frame(payload, 20)
    assert b"".join(parts) == payload
    assert all(len(p) <= 20 for p in parts)
    assert len(parts) == 13


def test_split_frame_handles_exact_multiples_and_empty():
    assert split_frame(b"x" * 40, 20) == [b"x" * 20, b"x" * 20]
    assert split_frame(b"", 20) == [b""]
    with pytest.raises(ValueError):
        split_frame(b"x", 0)


def test_worst_case_mtu_still_sends_a_real_frame_whole():
    # At a 23-byte MTU a full frame is many writes; the point is that it is
    # still complete when reassembled, not that it is fast.
    b = FrameBuilder()
    agents = tuple(
        Agent(f"w{i}:p1", f"w{i}", AgentStatus.WORKING, 1, r"C:\x\repo-name", "t", "claude", False)
        for i in range(6)
    )
    payload = b.encode(b.build(HerdSnapshot(agents=agents)))
    parts = split_frame(payload, chunk_size(MIN_MTU))
    assert len(parts) > 10
    assert b"".join(parts) == payload


# --------------------------------------------------------------- reassembly


def test_assembler_joins_chunks_into_frames():
    a = LineAssembler()
    assert a.feed(b'{"t":"ac') == []
    assert a.feed(b't"}\n') == ['{"t":"act"}']


def test_assembler_handles_several_frames_in_one_chunk():
    a = LineAssembler()
    assert a.feed(b"one\ntwo\nthree\n") == ["one", "two", "three"]


def test_assembler_survives_utf8_split_across_a_boundary():
    # The real hazard. At a 20-byte payload a long prompt is split every
    # twenty bytes, so a multi-byte sequence landing on a boundary is a
    # certainty. Decoding per-chunk would corrupt it; this accumulates bytes
    # and only decodes at the newline.
    text = "Allow Bash(git push)? … naïve —"
    blob = (json.dumps({"q": text}, ensure_ascii=False) + "\n").encode("utf-8")
    a = LineAssembler()
    out: list[str] = []
    for i in range(0, len(blob), 7):     # 7 deliberately lands mid-sequence
        out.extend(a.feed(blob[i : i + 7]))
    assert len(out) == 1
    assert json.loads(out[0])["q"] == text


def test_assembler_ignores_blank_lines_and_keeps_partials():
    a = LineAssembler()
    assert a.feed(b"\n\n") == []
    assert a.feed(b"partial") == []
    assert a.pending == len(b"partial")
    assert a.feed(b"\n") == ["partial"]
    assert a.pending == 0


def test_assembler_drops_an_unterminated_flood_rather_than_growing():
    a = LineAssembler(max_pending=64)
    assert a.feed(b"x" * 200) == []
    assert a.pending == 0
    # Still usable afterwards.
    assert a.feed(b"recovered\n") == ["recovered"]


# ------------------------------------------------------------------ backoff


def test_backoff_doubles_then_holds_at_the_cap():
    got = list(itertools.islice(backoff_delays(0.5, 3.0), 8))
    assert got[:3] == [0.5, 1.0, 2.0]
    assert all(d == 3.0 for d in got[3:])


def test_backoff_cap_keeps_the_ten_second_criterion_reachable():
    # An unbounded exponential fails "back within 10s" on the second
    # consecutive drop, which is precisely when you are walking around.
    delays = list(itertools.islice(backoff_delays(), 6))
    assert max(delays) <= BACKOFF_CAP
    assert sum(delays[:3]) < 10.0


# ------------------------------------------------------------ fake transport


def test_fake_transport_records_and_chunks():
    t = FakeTransport(mtu=23)
    payload = b"x" * 100 + b"\n"
    run(t.send(payload))
    assert t.sent == [payload]
    assert b"".join(t.chunks) == payload
    assert all(len(c) <= chunk_size(23) for c in t.chunks)


def test_fake_transport_refuses_after_close():
    t = FakeTransport()
    run(t.close())
    assert not t.connected
    with pytest.raises(TransportError):
        run(t.send(b"x\n"))


def test_fake_transport_replays_incoming_lines():
    t = FakeTransport(incoming=['{"t":"act"}', '{"t":"hello"}'])

    async def drain():
        return [line async for line in t.lines()]

    assert run(drain()) == ['{"t":"act"}', '{"t":"hello"}']


def test_a_built_frame_round_trips_through_chunking_and_reassembly():
    # The whole T8 -> T9 path, minus the radio: build, encode, chunk at the
    # worst-case MTU, reassemble, parse.
    b = FrameBuilder()
    agents = (
        Agent("w9:p1", "w9", AgentStatus.BLOCKED, 441, r"C:\x\elastic-migration", "t", "claude", False),
    )
    payload = b.encode(
        b.build(HerdSnapshot(agents=agents),
                prompts={"w9:p1": "Allow Bash(git push --force origin main)? … ok"})
    )
    a = LineAssembler()
    lines: list[str] = []
    for part in split_frame(payload, chunk_size(MIN_MTU)):
        lines.extend(a.feed(part))
    assert len(lines) == 1
    frame = json.loads(lines[0])
    assert frame["t"] == "snap"
    assert frame["a"][0]["i"] == "w9:p1"
    assert "…" in frame["a"][0]["q"]


# --------------------------------------------------------------- live MTU


class _FakeClient:
    """Stands in for bleak's client. mtu_size changes after connect, as the
    real one does."""

    def __init__(self, mtu):
        self.mtu_size = mtu
        self.is_connected = True


def test_mtu_is_read_live_not_cached_at_connect():
    # Observed on hardware: bleak reported mtu_size == 23 immediately after
    # connect while the device logged mtu=517 a second later. Caching the
    # connect-time value chunked every frame to 20 bytes when 514 were
    # available - a silent 25x throughput loss on a battery device.
    from shepherd.transport import BleTransport

    t = BleTransport(address="AA:BB:CC:DD:EE:FF")
    t._client = _FakeClient(23)
    assert t.mtu == 23
    assert chunk_size(t.mtu) == 20

    t._client.mtu_size = 517          # negotiation completes
    assert t.mtu == 517, "MTU must be re-read, not memoised"
    assert chunk_size(t.mtu) == 514


def test_mtu_is_none_when_disconnected():
    from shepherd.transport import BleTransport

    t = BleTransport(address="AA:BB:CC:DD:EE:FF")
    assert t.mtu is None
    assert chunk_size(t.mtu) == MIN_MTU - ATT_OVERHEAD
