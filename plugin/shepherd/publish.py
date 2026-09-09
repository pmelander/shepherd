"""Publishing the frame stream to a local file, for subscribers that are not
the device.

The relay already builds a frame for the Cardputer. Anything else that wants
to know what the herd is doing - a desk companion on a serial cable, a
menu-bar app, someone else's board - can read the same frames instead of the
relay learning about each of them. The relay takes a singleton lock on
purpose, so a second poller against one Herdr is not an option; a tee is.

Four things here are not obvious, and three of them were established by
running something rather than reasoning about it.

**The allowlist is the security boundary, not a tidiness measure.** The
obvious implementation is to wrap `transport.send()` and tee every frame that
goes out. One of those frames is the key-rotation frame, and it carries the
new 32-byte shared secret as plain hex (`auth.py`, `"k": new_secret.hex()`).
Teeing it would write the HMAC secret, in the clear, into a file whose whole
purpose is to be read by other programs - and anyone who can read that secret
can forge approvals for any agent in the herd. So this publishes only frame
types it has been told to publish, and anything else - including a `t` value
that does not exist yet - is dropped. It fails closed: a future frame type is
invisible to subscribers until someone deliberately adds it here.

**Nothing holds the file open.** Windows will not rename a file another
process has open unless that process asked for delete-sharing, and Python's
`open()` cannot ask. Verified: with a follower holding the file,
`os.replace()` raises `PermissionError: [WinError 32]`. So the writer opens
per append and closes, exactly as the audit log does - otherwise it would
block its own rotation.

**Rotation is rename-and-reopen, and it is allowed to fail.** A follower that
tracks a byte offset silently duplicates lines after a truncate-in-place and
silently skips them after a rename it did not notice, so the rename is the
safe half of that trade and the follower's job is to notice. It can still
collide with a follower's brief read window, and when it does the right
answer is to leave the file alone and try again on the next publish - which
happens at least every keepalive. Frames are never dropped to make a rotation
succeed. A rotation that keeps failing is a different matter and says so once.

**The encoding is explicit.** Frames are encoded with `ensure_ascii=False`, so
an agent's recap can carry a typographic quote or a box-drawing character. On
a machine with a cp1252 locale a bare `open(path, "a")` raises
UnicodeEncodeError on the first such frame, and a bare read raises
UnicodeDecodeError. Bytes are written directly here, which sidesteps the
question entirely: the payload is already UTF-8 from the encoder.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping

log = logging.getLogger("shepherd.publish")

STREAM_NAME = "frames.ndjson"

# What subscribers are allowed to see. `snap` is the herd snapshot the device
# renders. `said` is the announcement frame carrying what an agent last said,
# published on the transition into done and NOT sent over BLE - the device
# applies a detail frame unconditionally, so an unsolicited one would swap the
# screen out from under someone reading it.
#
# NOT here, and never to be added: "key". See the module docstring.
PUBLISHED = frozenset({"snap", "said"})

# Rotate at 2MB and keep one previous file, so the stream costs at most 4MB on
# disk. At the 10s keepalive floor the relay emits at least 6 frames a minute,
# which is roughly 9MB a day at realistic frame sizes - so 2MB is about five
# hours of history. More than any subscriber needs, and small enough that
# nobody notices it.
CAP_BYTES = 2 * 1024 * 1024


class FramePublisher:
    """Appends published frames to a local NDJSON file.

    Failures are swallowed on purpose. This is a tee: if it breaks, the relay
    must carry on serving the device exactly as before. The audit log sets the
    same precedent for the same reason, and it catches `Exception` rather than
    `OSError` because a path containing a null byte raises `ValueError` out of
    `mkdir` - an OSError-only guard let that escape and took the caller down.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        cap_bytes: int = CAP_BYTES,
        published: frozenset[str] = PUBLISHED,
    ) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.cap_bytes = cap_bytes
        self.published = published
        # Whether the current failure has already been reported. One line per
        # outage, not one per frame: the relay publishes every keepalive, so
        # a broken disk would otherwise produce six log lines a minute
        # forever. The relay went two days reporting every agent as unknown
        # because a failure was logged at DEBUG and never again.
        self._write_failing = False
        self._rotate_failing = False

    # -- the tee -----------------------------------------------------------

    def publish(self, frame: Mapping, payload: bytes) -> bool:
        """Publish one frame if its type is allowed. True if written.

        `payload` is the already-encoded bytes the encoder produced, so the
        stream carries byte-identical framing to what the device is sent and
        nothing is serialised twice.
        """
        kind = frame.get("t")
        if kind not in self.published:
            # Not an error and not worth a line per frame. A `key` frame
            # reaching here is the guard doing its job.
            log.debug("not publishing frame type %r", kind)
            return False

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Open per append, hold nothing: a held handle blocks our own
            # rename on Windows. Binary, because payload is already UTF-8.
            with self.path.open("ab") as fh:
                fh.write(payload)
            if self._write_failing:
                log.info("frame stream writable again: %s", self.path)
                self._write_failing = False
        except Exception as e:  # noqa: BLE001 - deliberately broad, see above
            if not self._write_failing:
                log.warning("cannot write the frame stream (%s); subscribers "
                            "will go stale, the device is unaffected", e)
                self._write_failing = True
            return False

        self._rotate_if_full()
        return True

    # -- rotation ----------------------------------------------------------

    def _rotate_if_full(self) -> None:
        try:
            size = self.path.stat().st_size
        except Exception:  # noqa: BLE001 - it may have been swept up under us
            return
        if size < self.cap_bytes:
            return

        previous = self.path.with_suffix(self.path.suffix + ".1")
        try:
            # os.replace overwrites the previous archive atomically. A
            # follower detects the swap by the file's identity changing:
            # st_ino IS populated and nonzero on NTFS and does change here.
            os.replace(self.path, previous)
            if self._rotate_failing:
                log.info("frame stream rotation working again")
                self._rotate_failing = False
        except Exception as e:  # noqa: BLE001
            # Expected occasionally and self-healing: a follower had the file
            # open for the moment it took to read. Next publish tries again.
            # Only worth a warning once the file has grown well past the cap,
            # which means rotation is not merely unlucky but stuck.
            if size >= 2 * self.cap_bytes and not self._rotate_failing:
                log.warning(
                    "frame stream stuck at %d bytes; rotation keeps failing "
                    "(%s). A subscriber holding the file open continuously "
                    "will do this - it must close between polls.", size, e
                )
                self._rotate_failing = True
            else:
                log.debug("rotation deferred (%s)", e)


def default_path() -> Path:
    """Where the stream lives.

    Under HERDR_PLUGIN_STATE_DIR, which Herdr injects and which sits outside
    any repo. Frames carry prompt text and agent recaps from real work, so
    they belong with the audit log rather than next to the source. Same
    fallback shape as rotate_marker().
    """
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return (Path(base) if base else Path.home() / ".shepherd") / STREAM_NAME
