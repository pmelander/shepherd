"""Authenticating the device, above the BLE link.

The bond proves the peer is a device Windows once paired with. It does not
prove the peer is *this* Cardputer running *this* firmware, because the link
is Just Works — bleak's WinRT backend cannot run a passkey ceremony, so
there is no MITM protection to lean on.

So every action frame carries a MAC over a canonical message, keyed by a
secret the relay generates and the firmware is built with. A peer that
manages to bond but does not hold the secret can make nothing happen.

The message is:

    ts | pane_id | action | decision_id

`ts` is the timestamp of the frame the device is answering, echoed back. The
design originally specified a sequence number here, but the protocol trim
removed `seq` from the frame; `ts` was already being sent and serves the same
purpose. Binding to it means a captured frame stops working as soon as the
relay moves on, which matters for `focus` — the one action with no decision
id, and therefore no other replay protection.

What this does NOT defend against: someone who can read the firmware image or
the relay's config file. Both hold the secret in the clear. Extracting flash
from a device in your pocket, or reading a file as your own user, are outside
what a shared secret can address.
"""

from __future__ import annotations

import hmac
import os
import secrets
from hashlib import sha256
from pathlib import Path

# The MAC is truncated for the wire. 16 hex characters is 64 bits, which for
# an online attacker who must also hold a BLE bond and guess against a relay
# that logs every refusal is a wide margin, and it costs 48 fewer bytes per
# frame on a link that sometimes negotiates a 20-byte payload.
MAC_HEX_LEN = 16

SECRET_BYTES = 32
SECRET_FILENAME = "shepherd.secret"


def config_dir() -> Path:
    """Where the secret lives.

    HERDR_PLUGIN_CONFIG_DIR is injected into plugin-spawned processes and is
    readable back via `herdr plugin config-dir shepherd`, so the secret has a
    canonical home rather than an invented one. This is the ordering the
    design review flagged as unsequenced.
    """
    raw = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if raw:
        return Path(raw)
    return Path.home() / ".shepherd"


def load_or_create_secret(path: Path | None = None) -> bytes:
    """Read the shared secret, generating it on first run.

    Written as lowercase hex so it can be pasted into a build flag without
    escaping. Permissions are set owner-only where the platform honours it;
    on Windows `chmod` does not map to a real ACL, so the file's protection
    there is the user profile directory it sits in, not this call. Said plainly
    because a comment claiming otherwise would be worse than none.
    """
    p = path or (config_dir() / SECRET_FILENAME)
    if p.exists():
        text = p.read_text(encoding="utf-8").strip()
        try:
            raw = bytes.fromhex(text)
        except ValueError as e:
            raise ValueError(f"{p} is not hex; delete it to regenerate") from e
        if len(raw) != SECRET_BYTES:
            raise ValueError(
                f"{p} holds {len(raw)} bytes, expected {SECRET_BYTES}; "
                "delete it to regenerate"
            )
        return raw

    raw = secrets.token_bytes(SECRET_BYTES)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(raw.hex(), encoding="utf-8")
    with_suppressed_oserror(lambda: os.chmod(p, 0o600))
    return raw


def with_suppressed_oserror(fn):
    try:
        return fn()
    except OSError:
        return None


def canonical_message(ts: str, pane_id: str, action: str,
                      decision_id: str | None) -> bytes:
    """The exact bytes both sides sign.

    Pipe-separated with an empty field for an absent decision id, so `focus`
    (no id) and a hypothetical action whose id is the empty string cannot
    produce the same message. Encoded UTF-8 and never locale-dependent — the
    device builds the same string with plain C, so anything clever here would
    diverge silently.
    """
    return "|".join([ts, pane_id, action, decision_id or ""]).encode("utf-8")


def sign(secret: bytes, ts: str, pane_id: str, action: str,
         decision_id: str | None) -> str:
    mac = hmac.new(secret, canonical_message(ts, pane_id, action, decision_id),
                   sha256).hexdigest()
    return mac[:MAC_HEX_LEN]


def verify(secret: bytes, presented: object, ts: str, pane_id: str,
           action: str, decision_id: str | None) -> bool:
    """Constant-time check. False for anything malformed rather than raising."""
    if not isinstance(presented, str) or len(presented) != MAC_HEX_LEN:
        return False
    expected = sign(secret, ts, pane_id, action, decision_id)
    return hmac.compare_digest(expected, presented.lower())


def build_flag(secret: bytes) -> str:
    """The PlatformIO flag that bakes this secret into the firmware."""
    return f"-DSHEPHERD_SECRET='\"{secret.hex()}\"'"
