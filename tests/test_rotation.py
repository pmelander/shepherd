"""Rotating the shared secret.

The property under test is not "the crypto works" — it is the ORDERING. A
rotation that half-succeeds leaves the two sides holding different keys, and
that failure is silent: the device pairs, connects, draws the herd perfectly,
and refuses every action as a bad signature. So every test here is about what
is true after an interruption.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.auth import (  # noqa: E402
    MAC_HEX_LEN,
    REKEY_PANE,
    SECRET_BYTES,
    ack_proof,
    canonical_message,
    load_or_create_secret,
    new_secret,
    rekey_frame,
    save_secret,
    sign,
    sign_rekey,
    verify_ack,
)

OLD = bytes(range(32))
NEW = bytes(range(32, 64))
TS = "2026-09-05T18:00:00Z"


def test_the_request_is_signed_with_the_key_it_replaces():
    # Changing the key requires already holding the key. This is the whole
    # security argument for doing rotation over the link at all.
    frame = rekey_frame(OLD, TS, NEW)
    assert frame["t"] == "key" and frame["k"] == NEW.hex()
    assert frame["mac"] == sign(OLD, TS, REKEY_PANE, "rekey", NEW.hex())
    assert frame["mac"] != sign(NEW, TS, REKEY_PANE, "rekey", NEW.hex())


def test_the_new_key_is_inside_what_was_signed():
    # Otherwise a peer could keep a captured MAC and swap in a key of its own.
    assert NEW.hex().encode() in canonical_message(TS, REKEY_PANE, "rekey", NEW.hex())
    assert sign_rekey(OLD, TS, NEW) != sign_rekey(OLD, TS, bytes(32))


def test_the_ack_proves_possession_of_the_new_key_not_the_old():
    assert verify_ack(NEW, TS, ack_proof(NEW, TS))
    assert not verify_ack(NEW, TS, ack_proof(OLD, TS))


def test_a_replayed_request_is_not_an_acknowledgement_of_itself():
    # The two MACs sign different actions - "rekey" and "rekeyed" - so a peer
    # that can only echo the frame it was sent cannot pass as having stored
    # anything. Without that, the relay would commit on a mirror.
    frame = rekey_frame(OLD, TS, NEW)
    assert not verify_ack(NEW, TS, frame["mac"])


def test_an_ack_from_a_different_rotation_does_not_count():
    # Bound to the timestamp the request went out with, so yesterday's
    # successful rotation cannot confirm today's.
    assert not verify_ack(NEW, TS, ack_proof(NEW, "2026-01-01T00:00:00Z"))


def test_a_malformed_ack_is_refused_rather_than_crashing():
    for bad in (None, "", "short", "z" * MAC_HEX_LEN, 12345, []):
        assert not verify_ack(NEW, TS, bad), bad


def test_the_ack_is_case_insensitive_but_not_content_insensitive():
    proof = ack_proof(NEW, TS)
    assert verify_ack(NEW, TS, proof.upper())
    assert not verify_ack(NEW, TS, proof[:-1] + ("0" if proof[-1] != "0" else "1"))


def test_a_generated_secret_is_the_right_size_and_not_reused():
    a, b = new_secret(), new_secret()
    assert len(a) == len(b) == SECRET_BYTES
    assert a != b


def test_saving_replaces_the_stored_secret_and_reads_back(tmp_path):
    p = tmp_path / "shepherd.secret"
    first = load_or_create_secret(p)
    save_secret(NEW, p)
    assert load_or_create_secret(p) == NEW != first
    assert p.read_text(encoding="utf-8").strip() == NEW.hex()


def test_saving_refuses_a_wrong_sized_key(tmp_path):
    # The device validates length before storing; so must this side, or a
    # short key would be written to disk and every action refused after.
    p = tmp_path / "shepherd.secret"
    load_or_create_secret(p)
    before = p.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        save_secret(b"\x01\x02\x03", p)
    assert p.read_text(encoding="utf-8") == before, "must not have been touched"


# ------------------------------------------------- the ordering, end to end


def test_the_relay_commits_only_after_the_device_proves_it(monkeypatch, tmp_path):
    """The property the whole design exists for.

    Interrupt the exchange anywhere before the proof verifies and BOTH sides
    must still hold the old key. The alternative - relay commits, device did
    not - is a link that looks healthy and refuses everything, with no error
    anywhere that says why.
    """
    import asyncio
    import json

    from shepherd.runner import Runner
    from shepherd.transport import FakeTransport

    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    secret_file = tmp_path / "shepherd.secret"
    save_secret(OLD, secret_file)

    class Gate:
        secret = OLD

    def run_rotation(answer):
        # Fake clock and sleep: the no-answer case waits out ROTATE_TIMEOUT,
        # and ten real seconds per failure mode is ten seconds nobody will
        # keep paying to run the suite.
        ticks = [0.0]

        async def sleep(_d):
            ticks[0] += 1.0
            await asyncio.sleep(0)

        r = Runner(secret=OLD, sleep=sleep, clock=lambda: ticks[0])
        r.tick = 0.0
        t = FakeTransport()
        gate = Gate()
        marker = tmp_path / "rotate.request"
        marker.write_text("go", encoding="utf-8")

        async def drive():
            task = asyncio.create_task(r._rotate(t, gate))
            for _ in range(200):
                await asyncio.sleep(0)
                if t.sent:
                    break
            sent = json.loads(t.sent[0].decode("utf-8"))
            if answer is not None:
                r._on_rekey_ack(answer(sent))
            await task
            return sent

        return r, run_coro(drive()), marker

    def run_coro(c):
        return asyncio.run(c)

    # 1. The device never answers.
    r, sent, marker = run_rotation(None)
    assert load_or_create_secret(secret_file) == OLD, "must not have committed"
    assert not marker.exists(), "a failed request must not retry forever"

    # 2. The device answers with a proof it could only have made by echoing.
    r, sent, marker = run_rotation(lambda s: {"t": "kack", "ts": s["ts"],
                                              "f": s["mac"]})
    assert load_or_create_secret(secret_file) == OLD, "an echo is not a proof"

    # 3. The device answers properly.
    fresh_holder = {}

    def real_ack(s):
        fresh_holder["key"] = bytes.fromhex(s["k"])
        return {"t": "kack", "ts": s["ts"],
                "f": ack_proof(fresh_holder["key"], s["ts"])}

    r, sent, marker = run_rotation(real_ack)
    stored = load_or_create_secret(secret_file)
    assert stored == fresh_holder["key"] != OLD, "should have committed"
    assert r.secret == stored, "the live relay must use the new key too"
    assert not marker.exists()
