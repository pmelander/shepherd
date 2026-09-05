"""One relay at a time.

There is exactly one Cardputer and exactly one BLE bond to it. A second relay
does not fail loudly — it sits in the reconnect backoff reporting *"no device
advertising Shepherd-* or NUS within 12s"* forever, because the first one is
holding the link and the device therefore is not advertising. That message
points at the radio, the pairing, the firmware, the antenna: everywhere
except the other copy of yourself.

That is not hypothetical. It cost an hour of this project's life, chasing a
device that had "stopped advertising" while a relay from a previous session
was quietly connected to it the whole time.

Auto-start makes it likelier, not less: the plugin now starts a relay with
every Herdr session, so any hand-run copy left over from debugging is a
collision waiting to happen.

The lock is an OS file lock rather than a pid file, because a pid file
outlives the process that wrote it. A relay killed with SIGKILL, or lost to a
power cut, leaves a pid file naming either nothing or — worse, eventually —
something else entirely. An OS lock is released by the kernel when the
process ends, however it ends.

Checking a pid for liveness would be the obvious alternative and is a trap on
this platform: `os.kill(pid, 0)` is the POSIX idiom, but on Windows os.kill
does not take a signal — it calls TerminateProcess, so the "check" kills the
very relay it was asking about.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO, Callable

LOCK_FILENAME = "relay.lock"

# The byte the lock is taken on, deliberately past the text.
#
# On Windows a locked byte range is MANDATORY, not advisory. Locking byte 0
# made even a Python read of the pid fail. Locking past end-of-file leaves the
# text readable to a plain open(), though PowerShell's Get-Content and Git
# Bash's cat still refuse the whole file while any region of it is locked —
# see acquire(). Keeping the lock off the text is worth it for the first of
# those; the other two are out of our hands.
LOCK_BYTE = 1024

# How often to re-try while waiting for a departing relay to let go. Shorter
# than the watchdog's 5s poll, so the handover costs a second or two rather
# than a whole poll interval.
RETRY_INTERVAL = 1.0


class AlreadyRunning(RuntimeError):
    """Another relay holds the lock."""


def lock_path() -> Path:
    """Beside the audit log, in Herdr's per-plugin state directory.

    State, not config: it describes this run rather than how to run, and it
    must not end up in a repo or a backup.
    """
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return (Path(base) if base else Path.home() / ".shepherd") / LOCK_FILENAME


def _lock(handle: IO[str]) -> bool:
    """Take an exclusive, non-blocking lock. False when someone else has it.

    Always LOCK_BYTE. `msvcrt.locking` locks a range starting at the CURRENT
    file position, so without the seek two relays could lock two different
    bytes of the same file and both believe they had won.
    """
    try:
        handle.seek(LOCK_BYTE)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def acquire(path: Path | str | None = None, wait: float = 0.0,
            sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic) -> IO[str]:
    """Become the relay, or raise AlreadyRunning.

    `wait` exists for one specific race, and it is the one auto-start
    creates. When Herdr restarts, the new session fires the startup hook
    immediately while the PREVIOUS session's relay is still alive — its
    watchdog only notices the socket stamp changed on its next poll, up to
    five seconds later. Without a wait, the incoming relay is refused by an
    orphan that is about to exit, the orphan then exits, and the session ends
    up with no relay at all: a worse outcome than the duplicate the lock was
    added to prevent.

    Returns the open handle, which the caller must keep alive for as long as
    it intends to hold the lock — closing it, or exiting, releases it.

    The pid goes into the file as a diagnostic, with a caveat worth knowing
    before you go looking for it: a Windows byte-range lock is mandatory, and
    PowerShell's Get-Content and Git Bash's cat both refuse a file that has
    any locked region, even one they are not reading. Python can read it, and
    anything can once the holder exits. `Get-CimInstance Win32_Process` is the
    reliable way to find a live relay.

    Opened "r+" and not "a+". Append mode sends every write to end-of-file
    whatever the seek says, so the first version of this appended a pid line
    per start and grew forever — two relays' worth of pid in the file was how
    it showed up.
    """
    p = Path(path) if path else lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    deadline = clock() + wait
    while True:
        if not p.exists():
            p.touch()
        handle = open(p, "r+", encoding="utf-8")
        if _lock(handle):
            break
        handle.close()
        if clock() >= deadline:
            raise AlreadyRunning(f"another relay holds {p}")
        sleep(min(RETRY_INTERVAL, max(0.0, deadline - clock())))
    try:
        # Truncate is safe here only because the lock sits past end-of-file:
        # shortening a region another handle has locked is not portable.
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError:
        # Losing the courtesy pid is not worth losing the lock over.
        pass
    return handle
