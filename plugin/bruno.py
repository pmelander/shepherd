#!/usr/bin/env python3
"""Bruno: follow the relay's frame stream and write it to a serial port.

Bruno is a desk companion on a wire. It never talks to Herdr and the relay
never learns it exists: the relay tees every frame it builds into
`frames.ndjson`, and this follows that file and forwards lines to the board.

    [[startup]] in herdr-plugin.toml
      -> bruno.py
           reads  $HERDR_PLUGIN_CONFIG_DIR/bruno.port   (the whole opt-in)
           follows $HERDR_PLUGIN_STATE_DIR/frames.ndjson
           writes  COM5, or whatever that file says

Six things here are not obvious, and most of them were established by running
something rather than by reasoning about it.

**No port file, no Bruno.** First act is to look for `bruno.port`. Absent, it
logs one line and exits 0. An install with no Bruno hardware therefore costs
exactly one process start per Herdr session and nothing after it - no retry
loop, no log noise, no held port. Creating that one file is the whole opt-in,
and deleting it is the whole off switch.

**It supervises itself, because Herdr will not.** Verified in this project on
2026-09-05: `herdr session stop` does NOT kill the processes a startup hook
spawned - they orphan and keep running. A Windows COM port is exclusive-open,
so orphan number two cannot open the port and sits retrying forever while the
screen goes stale. So this takes the same three precautions the relay does: an
OS file lock, a watchdog on HERDR_SOCKET_PATH, and an independent backstop
that gives up if the port stays unreachable.

**The file is opened, read, and CLOSED on every poll.** Not an efficiency
choice - the opposite. Windows will not rename a file another process holds
open unless that process asked for delete-sharing, and Python's `open()`
cannot ask. Verified: with a follower holding the file, the relay's rotation
raises `PermissionError: [WinError 32]`. Holding the handle would break the
writer's rotation, which is how the stream stays bounded.

**Only complete lines are forwarded.** The tee appends and this polls, with no
coordination between them, so reading half a frame is normal rather than
exceptional. Forwarding a partial line - or advancing past it - turns one good
frame into two malformed ones that the device silently drops, and a silently
dropped frame looks exactly like a relay that went quiet.

**Snapshots are coalesced, announcements never are.** At 115200 baud a
worst-case 6.3KB frame is about 550ms on the wire, so a busy herd can produce
frames faster than the cable carries them. A snapshot is idempotent state and
a superseded one is worthless; an announcement is an event and dropping it
loses the bleat. Getting that backwards would silently stop Bruno announcing
the thing it exists to announce.

**Stale frames are dropped rather than forwarded.** The device's staleness
rule measures time since it last RECEIVED a frame, so flushing an hour of
buffered history after a reconnect would make Bruno look perfectly current in
front of a herd nobody is watching. Frames carry an absolute UTC timestamp;
anything older than the device's own threshold is history, not news.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from shepherd.publish import STREAM_NAME, default_path  # noqa: E402
from shepherd.runner import herdr_supervised, herdr_liveness  # noqa: E402
from shepherd.serialport import candidates, open_port  # noqa: E402
from shepherd.singleton import AlreadyRunning, acquire  # noqa: E402

log = logging.getLogger("bruno")

# The opt-in. One file, holding a port name.
PORT_FILE = "bruno.port"

# Bruno's own lock, beside the relay's. A different file on purpose: the two
# daemons contend for different hardware and one must not refuse the other.
LOCK_FILENAME = "bruno.lock"

# How often to look for new lines. Cheap: a stat and, when something changed,
# one open-read-close.
POLL_INTERVAL = 0.5

# How long the board may be unreachable before this gives up and exits, so a
# Herdr restart gets a clean process rather than inheriting a stuck one. The
# relay has the same backstop for the same reason.
PORT_GONE_AFTER = 300.0

# Reconnect backoff after the board disappears, in seconds. Unplugging it is
# normal, so this stays gentle and bounded.
RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0)

# Frames older than this are history rather than news. Matches
# SHEPHERD_STALE_MS on the device: past it, the device declares NO SIGNAL on
# its own, and forwarding older frames would talk it out of that.
MAX_FRAME_AGE = 30.0

# What may be forwarded at all. The relay's tee already refuses to publish
# anything else - a rekey frame carries the shared secret in plain hex - and
# this is the second half of that guard, on the reading side. Neither is a
# substitute for the other: this process also reads a file a human could have
# edited.
FORWARDABLE = frozenset({"snap", "said"})

# Frame types where only the newest matters.
COALESCE = frozenset({"snap"})


def port_file() -> Path:
    """Where the opt-in lives: plugin CONFIG, not state.

    Config, because it describes how to run rather than what happened, and
    because a user creates it by hand. `herdr plugin config-dir shepherd`
    prints the directory.
    """
    base = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".shepherd") / PORT_FILE


def lock_path() -> Path:
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return (Path(base) if base else Path.home() / ".shepherd") / LOCK_FILENAME


def read_port() -> str | None:
    """The configured port, or None when Bruno is not wanted here.

    A blank file means "find it yourself", which is friendlier than making
    somebody look up a COM number to get started.
    """
    p = port_file()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        line = line.split("#")[0].strip()
        if line:
            return line
    return ""     # present but empty: auto-detect


def frame_age(frame: dict, now: datetime) -> float | None:
    """Seconds since the frame was built, or None when it cannot be told.

    Unknown is NOT treated as stale. A frame without a usable timestamp is
    forwarded, because the alternative - discarding it - would mean a single
    format change silently blanks the screen rather than showing something
    slightly old.
    """
    raw = frame.get("ts")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        when = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return (now - when).total_seconds()


def select(lines: list[str], now: datetime) -> tuple[list[bytes], dict[str, int]]:
    """Decide what actually goes down the wire, from one batch of new lines.

    Returns the payloads to write and a count of what was dropped and why, so
    a caller can log it rather than discarding silently.

    The order of the surviving lines is preserved. Only superseded snapshots
    are removed, and a batch containing more than one snapshot is itself the
    signal that we are behind - when keeping up, there is nothing to coalesce.
    """
    dropped = {"unparsable": 0, "refused": 0, "stale": 0, "superseded": 0}

    parsed: list[tuple[dict, str]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            dropped["unparsable"] += 1
            continue
        if not isinstance(frame, dict):
            dropped["unparsable"] += 1
            continue
        kind = frame.get("t")
        if kind not in FORWARDABLE:
            dropped["refused"] += 1
            continue
        age = frame_age(frame, now)
        if age is not None and age > MAX_FRAME_AGE:
            dropped["stale"] += 1
            continue
        parsed.append((frame, line))

    # Index of the last coalescable frame of each kind; everything earlier of
    # that kind is superseded.
    last: dict[str, int] = {}
    for i, (frame, _) in enumerate(parsed):
        kind = frame["t"]
        if kind in COALESCE:
            last[kind] = i

    out: list[bytes] = []
    for i, (frame, line) in enumerate(parsed):
        kind = frame["t"]
        if kind in COALESCE and last.get(kind) != i:
            dropped["superseded"] += 1
            continue
        out.append(line.encode("utf-8") + b"\n")
    return out, dropped


class Follower:
    """Tracks a position in the stream, across rotations and restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.ino: int | None = None
        # Bytes read that did not end in a newline. Held until the rest
        # arrives, never forwarded.
        self.partial = ""
        self.started = False

    def _reopen_from(self, position: int, why: str) -> None:
        log.info("stream %s; reading from %s", why,
                 "the start" if position == 0 else f"offset {position}")
        self.offset = position
        self.partial = ""

    def poll(self) -> list[str]:
        """Complete new lines since the last poll, oldest first."""
        try:
            st = self.path.stat()
        except OSError:
            # No stream yet, or it was just renamed away and the fresh file
            # does not exist for this instant. Both are normal - the relay
            # rotates immediately after the write that crosses its cap - so
            # this waits rather than complaining.
            return []

        if not self.started:
            # Start at the END. A subscriber joining mid-session must not
            # replay the whole file; the staleness filter would drop most of
            # it anyway, and the rest would be a burst of old news.
            self.offset = st.st_size
            self.ino = st.st_ino
            self.started = True
            log.info("following %s from offset %d", self.path, self.offset)
            return []

        if self.ino is not None and st.st_ino != self.ino:
            # Rotated: the relay renamed the file and opened a fresh one.
            # st_ino IS populated and nonzero on NTFS and does change here.
            self.ino = st.st_ino
            self._reopen_from(0, "rotated")
        elif st.st_size < self.offset:
            # Shrank without the identity changing - a truncate rather than a
            # rename. Not what the relay does, but a hand-edited file can.
            self._reopen_from(0, "shrank")

        if st.st_size == self.offset:
            return []

        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
                self.offset = fh.tell()
        except OSError as e:
            log.debug("could not read the stream (%s); will retry", e)
            return []

        chunk = self.partial + chunk
        # A trailing fragment means the writer is mid-append. Hold it.
        if chunk.endswith("\n"):
            self.partial = ""
        else:
            chunk, _, self.partial = chunk.rpartition("\n")
        return chunk.splitlines()


class Bruno:
    def __init__(self, port: str | None, stream: Path,
                 poll_interval: float = POLL_INTERVAL) -> None:
        self.port = port
        self.follower = Follower(stream)
        self.poll_interval = poll_interval
        self._stop = False
        self._serial = None
        self._unreachable_since: float | None = None

    def stop(self) -> None:
        """Named to match the relay's, so watch_herdr can drive either."""
        self._stop = True

    # -- the board ---------------------------------------------------------

    def _connect(self) -> bool:
        try:
            # reset=False is the whole point: opening a port the obvious way
            # asserts DTR, which on these boards is the auto-reset line, so
            # every reconnect would reboot Bruno. See shepherd/serialport.py.
            self._serial = open_port(self.port)
            log.info("writing to %s", self._serial.port)
            self._unreachable_since = None
            return True
        except Exception as e:  # noqa: BLE001 - pyserial raises several shapes
            log.warning("cannot open %s (%s); ports seen: %s",
                        self.port or "auto", e,
                        ", ".join(candidates()) or "none")
            return False

    def _drop(self) -> None:
        if self._serial is not None:
            with contextlib.suppress(Exception):
                self._serial.close()
        self._serial = None

    async def run(self, sleep=asyncio.sleep, clock=None) -> int:
        clock = clock or (lambda: asyncio.get_event_loop().time())
        delays = list(RETRY_DELAYS)
        attempt = 0

        while not self._stop:
            if self._serial is None:
                if self._connect():
                    attempt = 0
                else:
                    if self._unreachable_since is None:
                        self._unreachable_since = clock()
                    elif clock() - self._unreachable_since >= PORT_GONE_AFTER:
                        # Independent of any env plumbing being right, which
                        # is the point: a stuck follower holding nothing
                        # useful should make way for a fresh one.
                        log.error("board unreachable for %.0fs; exiting so a "
                                  "restart can try cleanly",
                                  clock() - self._unreachable_since)
                        return 1
                    await sleep(delays[min(attempt, len(delays) - 1)])
                    attempt += 1
                    continue

            lines = self.follower.poll()
            if lines:
                payloads, dropped = select(lines, datetime.now(timezone.utc))
                if not await self._write(payloads):
                    continue
                _log_drops(dropped, len(lines))

            await sleep(self.poll_interval)

        self._drop()
        return 0

    async def _write(self, payloads: list[bytes]) -> bool:
        """True when everything went out; False when the board went away."""
        for payload in payloads:
            try:
                self._serial.write(payload)
            except Exception as e:  # noqa: BLE001
                log.warning("lost the board mid-write (%s); reconnecting", e)
                self._drop()
                return False
        return True


def _log_drops(dropped: dict[str, int], total: int) -> None:
    # Superseded snapshots are normal and expected under load; the rest are
    # worth a line, because each one means the screen is missing something.
    noisy = {k: v for k, v in dropped.items() if v and k != "superseded"}
    if noisy:
        log.info("dropped %s of %d lines", noisy, total)
    elif dropped["superseded"]:
        log.debug("coalesced %d superseded snapshot(s) of %d lines",
                  dropped["superseded"], total)


async def watch_session(bruno: Bruno, interval: float = 5.0,
                        sleep=asyncio.sleep) -> None:
    """Stop when the Herdr session ends or is replaced.

    The relay's watch_herdr does exactly this and is duck-typed on .stop(),
    but it logs as "the relay" and takes a Runner-shaped argument, so this is
    the same rule said in Bruno's voice. The rule itself is not optional:
    Herdr does not kill startup-hook processes, so without it every restart
    leaves another orphan fighting for an exclusive-open COM port.
    """
    if not herdr_supervised():
        log.info("no HERDR_SOCKET_PATH; running unsupervised")
        return

    initial = herdr_liveness()
    while initial is None and not bruno._stop:
        # The stamp can be missing for a moment while Herdr writes it - this
        # process is started BY Herdr, so racing its startup is the normal
        # order. Waiting is right; disarming is what left a relay running for
        # two days after its session ended.
        await sleep(interval)
        initial = herdr_liveness()
    if bruno._stop:
        return
    log.info("supervising herdr session %s", initial)

    while not bruno._stop:
        await sleep(interval)
        if herdr_liveness() != initial:
            log.info("herdr session ended or restarted; exiting")
            bruno.stop()
            return


async def serve(args: argparse.Namespace) -> int:
    port = args.port if args.port is not None else read_port()
    if port is None:
        # The whole opt-in, and the whole off switch. One line, then gone.
        log.info("no %s; Bruno is not configured on this machine "
                 "(create that file with a port name to enable it)",
                 port_file())
        return 0

    try:
        lock = acquire(path=lock_path(), wait=args.wait)
    except AlreadyRunning as e:
        # Exit 0, not an error: the job is being done, just not by us. A red
        # plugin log would point at the wrong thing.
        log.warning("not starting: %s", e)
        return 0

    stream = Path(args.stream) if args.stream else default_path()
    log.info("bruno starting; stream=%s port=%s", stream, port or "auto-detect")

    bruno = Bruno(port=port or None, stream=stream)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, bruno.stop)

    watchdog = asyncio.create_task(watch_session(bruno))
    try:
        return await bruno.run()
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog
        lock.close()
        log.info("bruno stopped")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", help="serial port; overrides bruno.port")
    ap.add_argument("--stream", help="frames.ndjson to follow")
    ap.add_argument("--wait", type=float, default=20.0,
                    help="seconds to wait for a departing instance to let go")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Frames carry agent recaps, which are UTF-8. Printing one to a cp1252
    # stdout raises UnicodeEncodeError, and this process logs what it drops.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    return asyncio.run(serve(args))


if __name__ == "__main__":
    raise SystemExit(main())
