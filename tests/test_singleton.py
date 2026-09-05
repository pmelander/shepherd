"""One relay at a time.

The interesting case cannot be tested in-process: on Windows a second lock
attempt from the SAME process succeeds, because the OS tracks locks per
handle-owner rather than per file descriptor. So the contender here is a real
second interpreter. Testing this with two handles in one process would pass
while the actual failure — two relays, two processes, one radio — went
uncaught.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.singleton import (  # noqa: E402
    LOCK_FILENAME,
    AlreadyRunning,
    acquire,
    lock_path,
)

PLUGIN = str(Path(__file__).resolve().parents[1] / "plugin")


def contend(path: Path) -> subprocess.CompletedProcess:
    """Try to take the lock from a separate process."""
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {PLUGIN!r})
        from shepherd.singleton import acquire, AlreadyRunning
        try:
            acquire({str(path)!r})
            print("TOOK")
        except AlreadyRunning:
            print("REFUSED")
        """
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=60)


def test_a_second_relay_is_refused_while_the_first_holds_the_lock(tmp_path):
    p = tmp_path / LOCK_FILENAME
    held = acquire(p)
    try:
        assert contend(p).stdout.strip() == "REFUSED"
    finally:
        held.close()


def test_the_lock_is_released_when_the_holder_exits(tmp_path):
    # The whole reason this is an OS lock and not a pid file. A relay killed
    # outright leaves a pid file naming nothing, or eventually something else.
    p = tmp_path / LOCK_FILENAME
    held = acquire(p)
    held.close()
    assert contend(p).stdout.strip() == "TOOK"


def test_a_leftover_lock_file_does_not_block_a_fresh_start(tmp_path):
    # A lock file on disk means nothing on its own; only a live holder does.
    p = tmp_path / LOCK_FILENAME
    p.write_text("99999\n", encoding="utf-8")
    handle = acquire(p)
    handle.close()


def test_the_file_names_exactly_one_holder(tmp_path):
    # The first version opened "a+", where every write lands at end-of-file
    # whatever the seek says. So each start appended another pid line and the
    # file grew forever - the live one had two relays' worth in it before
    # anyone looked. A pid file that names two processes names none.
    import os

    p = tmp_path / LOCK_FILENAME
    for _ in range(3):
        handle = acquire(p)
        assert p.read_text(encoding="utf-8").strip() == str(os.getpid())
        handle.close()
    assert p.read_text(encoding="utf-8").count(chr(10)) == 1


def test_a_shorter_pid_does_not_leave_digits_of_a_longer_one(tmp_path):
    p = tmp_path / LOCK_FILENAME
    p.write_text("4294967295" + chr(10), encoding="utf-8")
    handle = acquire(p)
    try:
        import os

        assert p.read_text(encoding="utf-8") == f"{os.getpid()}" + chr(10)
    finally:
        handle.close()


def test_the_lock_lives_in_the_state_dir_not_a_repo(monkeypatch, tmp_path):
    # It describes this run, not how to run, and it must not end up in a repo
    # or a backup.
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    assert lock_path() == tmp_path / "state" / LOCK_FILENAME

    monkeypatch.delenv("HERDR_PLUGIN_STATE_DIR", raising=False)
    assert lock_path().name == LOCK_FILENAME
    assert lock_path().parent.name == ".shepherd"


def test_acquire_creates_the_directory_it_needs(tmp_path):
    # Herdr creates the state dir, but the relay is also run by hand.
    p = tmp_path / "not" / "yet" / LOCK_FILENAME
    handle = acquire(p)
    try:
        assert p.exists()
    finally:
        handle.close()


def test_refusal_is_an_exception_not_a_silent_false(tmp_path):
    p = tmp_path / LOCK_FILENAME
    held = acquire(p)
    try:
        code = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {PLUGIN!r})
            from shepherd.singleton import acquire
            acquire({str(p)!r})
            """
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=60)
        assert r.returncode != 0
        assert "AlreadyRunning" in r.stderr
    finally:
        held.close()


# --------------------------------------------------- the handover window


def test_it_waits_for_a_departing_relay_rather_than_losing_to_it(tmp_path):
    # The race auto-start creates: Herdr restarts, fires the startup hook
    # immediately, and the previous session's relay is still alive because
    # its watchdog polls every 5s. Refusing here would leave the new session
    # with no relay at all - strictly worse than the duplicate the lock
    # exists to prevent.
    p = tmp_path / LOCK_FILENAME
    held = acquire(p)
    ticks = []

    def sleep(d):
        ticks.append(d)
        if len(ticks) == 3:
            held.close()          # the orphan finally notices and exits

    t = [0.0]

    def clock():
        t[0] += 1.0
        return t[0]

    handle = acquire(p, wait=20.0, sleep=sleep, clock=clock)
    handle.close()
    assert len(ticks) == 3, "should have kept trying until the lock freed"


def test_the_wait_is_bounded(tmp_path):
    # A genuinely duplicated relay - one nobody is about to stop - must say
    # so promptly rather than hanging the plugin log on a spinner.
    p = tmp_path / LOCK_FILENAME
    held = acquire(p)
    try:
        t = [0.0]

        def clock():
            t[0] += 1.0
            return t[0]

        with pytest.raises(AlreadyRunning):
            acquire(p, wait=5.0, sleep=lambda d: None, clock=clock)
    finally:
        held.close()


def test_no_wait_by_default(tmp_path):
    # Every caller but serve() wants the immediate answer.
    p = tmp_path / LOCK_FILENAME
    held = acquire(p)
    try:
        with pytest.raises(AlreadyRunning):
            acquire(p, sleep=lambda d: pytest.fail("should not have slept"))
    finally:
        held.close()
