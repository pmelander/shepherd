"""Frame stream coverage.

The first test in this file is the one that matters. Everything else is
behaviour; that one is the reason the allowlist exists at all.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.auth import rekey_frame  # noqa: E402
from shepherd.frame import FrameBuilder  # noqa: E402
from shepherd.publish import (  # noqa: E402
    CAP_BYTES,
    PUBLISHED,
    STREAM_NAME,
    FramePublisher,
    default_path,
)


def pub(tmp_path, **kw):
    return FramePublisher(path=tmp_path / STREAM_NAME, **kw)


def lines(p: FramePublisher) -> list[str]:
    if not p.path.exists():
        return []
    return p.path.read_text(encoding="utf-8").splitlines()


# ------------------------------------------------------- the allowlist


def test_a_real_rekey_frame_is_never_published(tmp_path):
    # THE test. Built from the real rekey_frame() rather than a hand-written
    # dict, so it stays true if that frame's shape changes: whatever fields it
    # grows, the secret must not reach the stream.
    p = pub(tmp_path)
    current = b"\x11" * 32
    fresh = b"\xab" * 32
    frame = rekey_frame(current, "2026-09-09T12:00:00Z", fresh)

    assert p.publish(frame, json.dumps(frame).encode()) is False
    assert lines(p) == []

    # And belt-and-braces: the secret's hex must not appear anywhere in the
    # stream even if some future change starts writing part of the frame.
    blob = p.path.read_text(encoding="utf-8") if p.path.exists() else ""
    assert fresh.hex() not in blob
    assert current.hex() not in blob


def test_the_secret_is_actually_in_the_frame_we_just_refused(tmp_path):
    # Guards the test above from becoming vacuous. If rekey_frame ever stopped
    # carrying the key, the refusal test would still pass while proving
    # nothing, so assert the danger is real.
    fresh = b"\xab" * 32
    frame = rekey_frame(b"\x11" * 32, "2026-09-09T12:00:00Z", fresh)
    assert fresh.hex() in json.dumps(frame)


def test_key_is_not_in_the_allowlist():
    assert "key" not in PUBLISHED
    assert PUBLISHED == frozenset({"snap", "said"})


def test_an_unknown_frame_type_fails_closed(tmp_path):
    # A `t` value nobody has thought of yet must not reach subscribers. This
    # is the difference between an allowlist and a denylist, and the reason
    # for the allowlist: a denylist only excludes what someone remembered.
    p = pub(tmp_path)
    for kind in ("kack", "deet", "telemetry", "", None, "SNAP"):
        frame = {"t": kind, "v": 3}
        assert p.publish(frame, b'{"t":"whatever"}\n') is False
    assert lines(p) == []


def test_a_frame_with_no_type_at_all_is_refused(tmp_path):
    p = pub(tmp_path)
    assert p.publish({"v": 3, "a": []}, b"{}\n") is False
    assert lines(p) == []


def test_snap_and_said_are_published(tmp_path):
    p = pub(tmp_path)
    assert p.publish({"t": "snap", "v": 3}, b'{"t":"snap"}\n') is True
    assert p.publish({"t": "said", "v": 3}, b'{"t":"said"}\n') is True
    assert lines(p) == ['{"t":"snap"}', '{"t":"said"}']


# ------------------------------------------------------- writing


def test_the_exact_payload_bytes_are_written_not_a_re_encoding(tmp_path):
    # Subscribers get byte-identical framing to what the device is sent, and
    # nothing is serialised twice.
    p = pub(tmp_path)
    payload = b'{"t":"snap","v":3,"a":[]}\n'
    p.publish({"t": "snap"}, payload)
    assert p.path.read_bytes() == payload


def test_appends_rather_than_overwriting(tmp_path):
    p = pub(tmp_path)
    for i in range(5):
        p.publish({"t": "snap"}, f'{{"t":"snap","n":{i}}}\n'.encode())
    assert len(lines(p)) == 5


def test_a_real_frame_with_non_ascii_survives_the_round_trip(tmp_path):
    # Frames are encoded with ensure_ascii=False, so an agent's recap can
    # carry a typographic quote. On a cp1252 locale a text-mode append would
    # raise UnicodeEncodeError on exactly this frame.
    b = FrameBuilder()
    frame = {"t": "snap", "v": 3, "a": [
        {"i": "w1:p1", "n": "agent", "s": "done", "d": "Don’t — café ✓"}
    ]}
    payload = b.encode(frame)
    p = pub(tmp_path)
    assert p.publish(frame, payload) is True

    got = json.loads(p.path.read_text(encoding="utf-8").splitlines()[0])
    assert got["a"][0]["d"] == "Don’t — café ✓"


def test_the_directory_is_created_if_missing(tmp_path):
    p = FramePublisher(path=tmp_path / "deep" / "deeper" / STREAM_NAME)
    assert p.publish({"t": "snap"}, b'{"t":"snap"}\n') is True
    assert p.path.exists()


# ------------------------------------------------------- failure is survivable


def test_a_write_failure_is_swallowed_and_logged_once(tmp_path, caplog):
    # The tee must never take the relay down. A directory where the file
    # should be makes every open fail.
    p = pub(tmp_path)
    p.path.mkdir(parents=True)

    with caplog.at_level("WARNING", logger="shepherd.publish"):
        for _ in range(5):
            assert p.publish({"t": "snap"}, b'{"t":"snap"}\n') is False

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, "one line per outage, not one per frame"
    assert "device is unaffected" in warnings[0].getMessage()


def test_recovery_after_a_write_failure_is_reported(tmp_path, caplog):
    p = pub(tmp_path)
    p.path.mkdir(parents=True)
    assert p.publish({"t": "snap"}, b"x\n") is False
    p.path.rmdir()

    with caplog.at_level("INFO", logger="shepherd.publish"):
        assert p.publish({"t": "snap"}, b'{"t":"snap"}\n') is True
    assert any("writable again" in r.getMessage() for r in caplog.records)


def test_a_null_byte_in_the_path_does_not_escape_as_valueerror(tmp_path):
    # The audit log's guard catches Exception rather than OSError precisely
    # because mkdir raises ValueError on a path like this, and an OSError-only
    # guard let it through and took the caller down.
    p = FramePublisher(path=str(tmp_path) + "\x00bad" + os.sep + STREAM_NAME)
    assert p.publish({"t": "snap"}, b'{"t":"snap"}\n') is False


# ------------------------------------------------------- rotation


def test_rotation_renames_and_starts_a_fresh_file(tmp_path):
    p = pub(tmp_path, cap_bytes=200)
    payload = b'{"t":"snap","pad":"' + b"x" * 100 + b'"}\n'

    p.publish({"t": "snap"}, payload)          # under the cap
    assert p.path.exists()
    assert not p.path.with_suffix(".ndjson.1").exists()

    p.publish({"t": "snap"}, payload)          # crosses it
    previous = p.path.with_suffix(".ndjson.1")
    assert previous.exists(), "nothing was rotated"
    assert not p.path.exists(), "a fresh file should not exist until the next write"
    assert len(previous.read_text(encoding="utf-8").splitlines()) == 2


def test_the_stream_is_bounded_by_two_files(tmp_path):
    p = pub(tmp_path, cap_bytes=200)
    payload = b'{"t":"snap","pad":"' + b"x" * 100 + b'"}\n'
    for _ in range(40):
        p.publish({"t": "snap"}, payload)

    present = sorted(q.name for q in tmp_path.iterdir())
    # Never more than two files. The live one may legitimately be ABSENT at
    # this instant: rotation happens immediately after the write that crosses
    # the cap, so the last thing to happen was the rename. A follower has to
    # tolerate that gap, which is why "file missing -> wait" is one of its
    # branches rather than an error.
    assert set(present) <= {STREAM_NAME, STREAM_NAME + ".1"}, present
    assert STREAM_NAME + ".1" in present, "nothing ever rotated"
    total = sum((tmp_path / n).stat().st_size for n in present)
    assert total <= 4 * 200, f"stream grew past two caps: {total}"


def test_the_file_identity_changes_across_a_rotation(tmp_path):
    # How a follower is expected to notice. st_ino is populated and nonzero on
    # NTFS and must differ, or a byte-offset subscriber silently keeps reading
    # from a stale position.
    p = pub(tmp_path, cap_bytes=200)
    payload = b'{"t":"snap","pad":"' + b"x" * 100 + b'"}\n'
    p.publish({"t": "snap"}, payload)
    before = p.path.stat().st_ino

    p.publish({"t": "snap"}, payload)   # rotates
    p.publish({"t": "snap"}, payload)   # fresh file exists again
    after = p.path.stat().st_ino

    assert before != 0 and after != 0, "st_ino unusable on this filesystem"
    assert before != after


def test_a_failed_rotation_keeps_frames_and_retries_next_time(tmp_path, monkeypatch):
    # A rotation colliding with a follower's read is expected. Losing frames
    # to make the rotation succeed would not be.
    p = pub(tmp_path, cap_bytes=200)
    payload = b'{"t":"snap","pad":"' + b"x" * 100 + b'"}\n'

    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(32, "being used by another process")
        return real(src, dst)

    monkeypatch.setattr("shepherd.publish.os.replace", flaky)

    p.publish({"t": "snap"}, payload)
    p.publish({"t": "snap"}, payload)   # rotation attempt 1: refused
    assert p.path.exists(), "the frames were lost when rotation failed"
    assert len(lines(p)) == 2

    p.publish({"t": "snap"}, payload)   # attempt 2: succeeds
    assert p.path.with_suffix(".ndjson.1").exists()
    assert calls["n"] == 2


def test_rotation_stuck_well_past_the_cap_says_so_once(tmp_path, monkeypatch, caplog):
    # Occasional failure is silent because it is normal. A subscriber holding
    # the file open continuously is not normal, and the file grows without
    # bound - that has to reach the log, and name the likely cause.
    p = pub(tmp_path, cap_bytes=100)
    payload = b'{"t":"snap","pad":"' + b"x" * 60 + b'"}\n'

    def always_refuse(src, dst):
        raise PermissionError(32, "being used by another process")

    monkeypatch.setattr("shepherd.publish.os.replace", always_refuse)

    with caplog.at_level("WARNING", logger="shepherd.publish"):
        for _ in range(10):
            p.publish({"t": "snap"}, payload)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, "one warning per stuck rotation, not per frame"
    msg = warnings[0].getMessage()
    assert "rotation keeps failing" in msg
    assert "close between polls" in msg, "the warning should name the cause"


def test_a_refused_frame_never_triggers_rotation(tmp_path):
    p = pub(tmp_path, cap_bytes=10)
    for _ in range(20):
        p.publish({"t": "key", "k": "deadbeef"}, b"x" * 100)
    assert not p.path.exists()
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------- defaults


def test_the_default_path_follows_the_state_dir(monkeypatch, tmp_path):
    # Frames carry prompt text and agent recaps from real work, so the stream
    # belongs in HERDR_PLUGIN_STATE_DIR beside the audit log, never in a repo.
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    assert default_path() == tmp_path / STREAM_NAME


def test_the_default_path_has_a_fallback_outside_any_repo(monkeypatch):
    monkeypatch.delenv("HERDR_PLUGIN_STATE_DIR", raising=False)
    p = default_path()
    assert p.name == STREAM_NAME
    assert p.parent == Path.home() / ".shepherd"


def test_the_cap_is_a_sane_size():
    # Two files at this cap is the whole on-disk cost of the stream.
    assert 1024 * 1024 <= CAP_BYTES <= 16 * 1024 * 1024
