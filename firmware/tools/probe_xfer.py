#!/usr/bin/env python3
"""Prove the device's serial file receiver works: stream a character over
serial, watch the acks, verify it reloads.

Was `test_xfer.py`, renamed because it is a hardware probe rather than a test -
it needs a board and asserts nothing - and because the `test_` prefix made
pytest import it during collection, where its module-level `sys.exit()` took
down the whole run with "INTERNALERROR> SystemExit: no stick found".

This is the only harness that exercises `firmware/src/xfer.h`, which is why it
was fixed rather than deleted. Worth knowing before you trust a failure: the
Cardputer's LittleFS is currently corrupt (`Corrupted dir pair at {0x1,0x0}`,
mount fails), so a transfer can succeed on the wire and still not produce a
loadable character. See TODOS.md.

The DTR handling here is DELIBERATE and differs from probe_serial.py. Upstream
opened the port, asserted DTR and slept 2 seconds with the comment "let any
DTR-triggered reset finish booting" - it wanted the board reset before a
transfer, which is reasonable for a tool that needs a known-clean device. That
intent is now explicit as `reset=True` rather than being a side effect of
which pyserial call happened to get written. Anything that merely WATCHES a
running device must use the default, `reset=False`.

Port discovery was `/dev/cu.usbserial-*`, a macOS glob, so this could not run
on the Windows host this project is developed on. It is cross-platform now.

    py -3 firmware/tools/probe_xfer.py [SRC_DIR] [NAME] [--port PORT]
"""

import base64
import json
import os
import sys
import time
from glob import glob
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugin"))

from shepherd.serialport import candidates, open_port  # noqa: E402

CHUNK = 256


def send(s, obj) -> None:
    s.write((json.dumps(obj) + "\n").encode())


def wait_ack(s, what, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = s.readline().decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                a = json.loads(line)
            except ValueError:
                continue
            if a.get("ack") == what:
                return a
        else:
            print(f"  (skip: {line[:60]})", file=sys.stderr)
    return None


def send_file(s, name, path) -> bool:
    with open(path, "rb") as fh:
        data = fh.read()
    print(f"  {name}: {len(data)} bytes", end="", flush=True)
    send(s, {"cmd": "file", "path": name, "size": len(data)})
    a = wait_ack(s, "file")
    if not a or not a.get("ok"):
        print(" - open FAILED")
        return False

    for i in range(0, len(data), CHUNK):
        chunk = data[i:i + CHUNK]
        send(s, {"cmd": "chunk", "d": base64.b64encode(chunk).decode()})
        a = wait_ack(s, "chunk", timeout=3)
        if not a or not a.get("ok"):
            print(f" - chunk {i} FAILED")
            return False
        if i and i % 16384 == 0:
            print(".", end="", flush=True)

    send(s, {"cmd": "file_end"})
    a = wait_ack(s, "file_end", timeout=10)
    ok = bool(a and a.get("ok") and a.get("n") == len(data))
    print(f" - {'ok' if ok else 'FAILED'} ({a.get('n') if a else '?'} written)")
    return ok


def main(argv: list[str]) -> int:
    port = None
    if "--port" in argv:
        i = argv.index("--port")
        port = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]

    if port is None:
        found = candidates()
        if not found:
            print("no board found; pass --port COM5", file=sys.stderr)
            return 1
        port = found[0]

    here = os.path.dirname(os.path.abspath(__file__))
    src = argv[1] if len(argv) > 1 else f"{here}/../characters/bufo"
    name = argv[2] if len(argv) > 2 else "test"

    # reset=True on purpose: this wants a freshly booted device before it
    # starts a multi-file transfer. open_port asserts DTR before open, waits
    # for the boot, and flushes the boot chatter.
    try:
        s = open_port(port, reset=True, timeout=2)
    except Exception as e:  # noqa: BLE001 - pyserial raises several shapes
        print(f"could not open {port}: {e}", file=sys.stderr)
        print(f"ports seen: {', '.join(candidates()) or 'none'}", file=sys.stderr)
        return 1

    print(f"installing '{name}' from {src}")
    print("waiting for device...", end="", flush=True)
    try:
        for _ in range(8):
            s.reset_input_buffer()
            send(s, {"cmd": "char_begin", "name": name})
            a = wait_ack(s, "char_begin", timeout=2)
            if a and a.get("ok"):
                print(" ready")
                break
            print(".", end="", flush=True)
            time.sleep(1)
        else:
            print("\nchar_begin: device never responded", file=sys.stderr)
            return 1

        t0 = time.time()
        files = sorted(glob(f"{src}/*"))
        if not files:
            print(f"nothing to send: {src} is empty", file=sys.stderr)
            return 1
        total = sum(os.path.getsize(f) for f in files)
        print(f"{len(files)} files, {total} bytes total")

        for f in files:
            if not send_file(s, os.path.basename(f), f):
                print("transfer failed", file=sys.stderr)
                return 1

        send(s, {"cmd": "char_end"})
        a = wait_ack(s, "char_end", timeout=10)
        dt = time.time() - t0
        print(f"\nchar_end: {a}")
        if dt > 0:
            print(f"{total} bytes in {dt:.1f}s = {total / dt / 1024:.1f} KB/s")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
