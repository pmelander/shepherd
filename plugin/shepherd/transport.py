"""Getting frames to the device and keypresses back.

The Nordic UART UUIDs and characteristic properties here are read off the
firmware in `firmware/src/ble_bridge.cpp`, not guessed:

    service  6e400001-b5a3-f393-e0a9-e50e24dcca9e
    RX       6e400002-...  host -> device, WRITE | WRITE_NR, encrypted
    TX       6e400003-...  device -> host, NOTIFY, encrypted

Both characteristics are encrypted-only, so nothing flows until the link is
bonded. The firmware pairs Just Works with NO_INPUT_OUTPUT because bleak's
WinRT backend hardcodes CONFIRM_ONLY; see the SHEPHERD_BLE_PASSKEY block
in ble_bridge.cpp for the whole story.

Two firmware facts shape the code below:

* `bleWrite()` chunks outbound notifications to `mtu - 3` capped at 180, so
  device -> host frames arrive in pieces and must be reassembled.
* `rxPush()` **silently drops** inbound bytes when its 2048-byte ring is
  full. There is no flow control in the protocol, so writes use
  write-with-response rather than the faster write-without-response. Losing
  the middle of a frame to a full ring would be invisible on both ends.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Iterator, Protocol, Sequence

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # host writes here
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device notifies here

DEVICE_NAME_PREFIX = "Shepherd-"

# WinRT does not let a central request an ATT MTU — bleak exposes mtu_size
# read-only, and only once a GATT session exists. Windows has been seen to
# negotiate as little as the 23-byte default, so the floor is not theoretical.
MIN_MTU = 23
ATT_OVERHEAD = 3

# Reconnect backoff. Capped rather than exponential-forever because the
# success criterion is "back within 10s"; an unbounded backoff fails that on
# the second consecutive drop, which is exactly when you are walking around.
BACKOFF_START = 0.5
BACKOFF_CAP = 3.0

SCAN_TIMEOUT = 12.0
CONNECT_TIMEOUT = 20.0


class TransportError(RuntimeError):
    """The link could not be established, or has gone away."""


# ---------------------------------------------------------------- pure bits


def backoff_delays(
    start: float = BACKOFF_START, cap: float = BACKOFF_CAP
) -> Iterator[float]:
    """Doubling backoff that stops doubling at the cap.

    Deliberately infinite: the relay keeps trying for as long as it runs. It
    is the caller's job to mark the herd unknown while disconnected, not this
    generator's job to give up.
    """
    delay = start
    while True:
        yield delay
        delay = min(delay * 2, cap)


def chunk_size(mtu: int | None) -> int:
    """How many payload bytes fit in one write.

    `mtu` may be None before a GATT session exists. Falling back to the
    23-byte default is correct rather than pessimistic: a write larger than
    the real MTU is silently truncated by the stack.
    """
    effective = mtu if isinstance(mtu, int) and mtu >= MIN_MTU else MIN_MTU
    return effective - ATT_OVERHEAD


def split_frame(payload: bytes, size: int) -> list[bytes]:
    if size < 1:
        raise ValueError("chunk size must be positive")
    return [payload[i : i + size] for i in range(0, len(payload), size)] or [b""]


class LineAssembler:
    """Reassembles newline-delimited frames from arbitrary chunk boundaries.

    Accumulates **bytes** and only decodes once a newline is seen. Decoding
    each chunk as it arrives would corrupt any multi-byte UTF-8 sequence that
    straddles a boundary, and at a 20-byte payload a long prompt is split
    every twenty bytes — so this is a certainty, not an edge case.
    """

    def __init__(self, max_pending: int = 8192) -> None:
        self._buf = bytearray()
        self._max = max_pending

    def feed(self, data: bytes) -> list[str]:
        self._buf.extend(data)
        if len(self._buf) > self._max:
            # A frame that never terminates would otherwise grow without
            # bound. Dropping is the only safe move, and dropping loudly is
            # better than dropping the oldest bytes and silently corrupting
            # whatever eventually does arrive.
            self._buf.clear()
            return []

        out: list[str] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            text = raw.decode("utf-8", "replace").strip()
            if text:
                out.append(text)
        return out

    @property
    def pending(self) -> int:
        return len(self._buf)


# ------------------------------------------------------------------- seam


class Transport(Protocol):
    """A byte pipe to the device. Framing is the caller's business."""

    async def send(self, payload: bytes) -> None: ...

    def lines(self) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...

    @property
    def connected(self) -> bool: ...


# --------------------------------------------------------------- the real one


class BleTransport:
    """Transport over BLE GATT, via bleak."""

    def __init__(
        self,
        address: str | None = None,
        name_prefix: str = DEVICE_NAME_PREFIX,
        scan_timeout: float = SCAN_TIMEOUT,
    ) -> None:
        self._address = address
        self._name_prefix = name_prefix
        self._scan_timeout = scan_timeout
        self._client = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._assembler = LineAssembler()
        self._mtu: int | None = None

    # -- discovery ------------------------------------------------------

    async def discover(self) -> str:
        """Find the device.

        Matching on the NUS service UUID as well as the name means a device
        whose name did not make it into the advertisement is still found.
        Reconnect on Windows needs a fresh scan rather than a bare address
        connect, so this runs on every reconnect, not just the first.
        """
        from bleak import BleakScanner

        found: dict[str, object] = {}

        def cb(dev, adv):
            name = (adv.local_name or dev.name or "")
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            if name.startswith(self._name_prefix) or NUS_SERVICE in uuids:
                found.setdefault(dev.address, dev)

        scanner = BleakScanner(detection_callback=cb)
        await scanner.start()
        try:
            deadline = asyncio.get_running_loop().time() + self._scan_timeout
            while not found and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.25)
        finally:
            await scanner.stop()

        if not found:
            raise TransportError(
                f"no device advertising {self._name_prefix}* or NUS "
                f"within {self._scan_timeout:g}s"
            )
        return next(iter(found))

    # -- lifecycle ------------------------------------------------------

    async def connect(self) -> None:
        from bleak import BleakClient

        address = self._address or await self.discover()

        client = BleakClient(address, timeout=CONNECT_TIMEOUT)
        try:
            await client.connect()
        except Exception as e:  # noqa: BLE001 - bleak raises a wide family
            raise TransportError(f"connect to {address} failed: {e}") from e

        # Both characteristics are encrypted-only, so the link must be bonded
        # before anything flows. Windows often pairs implicitly on first
        # encrypted access; asking explicitly makes the failure legible when
        # it does not. Already-paired raises on some backends, which is fine.
        try:
            await client.pair()
        except Exception:  # noqa: BLE001
            pass

        try:
            await client.start_notify(NUS_TX, self._on_notify)
        except Exception as e:  # noqa: BLE001
            await client.disconnect()
            raise TransportError(f"cannot subscribe to NUS TX: {e}") from e

        self._client = client
        self._address = address
        self._mtu = getattr(client, "mtu_size", None)

    def _on_notify(self, _sender, data: bytearray) -> None:
        for line in self._assembler.feed(bytes(data)):
            self._queue.put_nowait(line)

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    @property
    def mtu(self) -> int | None:
        return self._mtu

    # -- traffic --------------------------------------------------------

    async def send(self, payload: bytes) -> None:
        """Write one already-framed payload, chunked to the negotiated MTU.

        Write-with-response, deliberately. The firmware's receive ring drops
        silently when full and the protocol has no flow control, so the
        round trip per chunk is what stops a long frame from arriving with a
        hole in the middle.
        """
        if not self.connected or self._client is None:
            raise TransportError("not connected")
        for part in split_frame(payload, chunk_size(self._mtu)):
            try:
                await self._client.write_gatt_char(NUS_RX, part, response=True)
            except Exception as e:  # noqa: BLE001
                raise TransportError(f"write failed: {e}") from e

    async def lines(self) -> AsyncIterator[str]:
        """Yield complete frames from the device until the link drops."""
        while True:
            if not self.connected and self._queue.empty():
                return
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - shutdown must not mask a real error
            pass


# ----------------------------------------------------------------- testing


class FakeTransport:
    """In-memory Transport. Records what was sent, replays canned lines."""

    def __init__(self, incoming: Sequence[str] = (), mtu: int = 247) -> None:
        self.sent: list[bytes] = []
        self.chunks: list[bytes] = []
        self.closed = False
        self._mtu = mtu
        self._incoming = list(incoming)

    @property
    def connected(self) -> bool:
        return not self.closed

    @property
    def mtu(self) -> int | None:
        return self._mtu

    async def send(self, payload: bytes) -> None:
        if self.closed:
            raise TransportError("not connected")
        self.sent.append(payload)
        self.chunks.extend(split_frame(payload, chunk_size(self._mtu)))

    async def lines(self) -> AsyncIterator[str]:
        for line in self._incoming:
            yield line

    async def close(self) -> None:
        self.closed = True
