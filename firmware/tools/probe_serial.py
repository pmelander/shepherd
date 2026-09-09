#!/usr/bin/env python3
"""Prove the device reacts to serial JSON. Cycles states every 3s.

Was `test_serial.py`, renamed for two reasons. It is a hardware probe, not a
test - it needs a board plugged in and it asserts nothing - and the `test_`
prefix meant pytest imported it during collection, where its `sys.exit()` at
module level killed the entire run:

    INTERNALERROR> ... SystemExit: no stick found

`pytest.ini` bounds collection to tests/ as well, but that is overridden the
moment anyone passes an explicit path, so the rename is the part that actually
holds.

Two other fixes from the pristine upstream version:

  * It opened the port with `serial.Serial(port, 115200)`, which asserts DTR
    and therefore RESETS the board on open. It now goes through
    shepherd.serialport.open_port, which deasserts DTR and RTS first.
  * It found ports by globbing `/dev/cu.usbserial-*`, a macOS path, so it
    could never run on the Windows host this project is developed on.

Note this speaks UPSTREAM's aggregate protocol (total/running/waiting), not
Shepherd's per-agent frames, so it exercises the buddy screens rather than the
herd screens.

    py -3 firmware/tools/probe_serial.py [PORT]
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugin"))

from shepherd.serialport import candidates, open_port  # noqa: E402

STATES = [
    {"total": 0, "running": 0, "waiting": 0},  # -> sleep
    {"total": 2, "running": 1, "waiting": 0},  # -> idle
    {"total": 4, "running": 3, "waiting": 0},  # -> busy
    {"total": 2, "running": 1, "waiting": 1},  # -> attention, LED blinks
]


def main(argv: list[str]) -> int:
    port = argv[1] if len(argv) > 1 else None
    if port is None:
        found = candidates()
        if not found:
            print("no board found; pass a port explicitly, e.g. COM5",
                  file=sys.stderr)
            return 1
        port = found[0]

    # reset=False: watch the device react, do not reboot it out from under
    # yourself. See shepherd/serialport.py for why that is not the default
    # pyserial behaviour.
    try:
        s = open_port(port)
    except Exception as e:  # noqa: BLE001 - pyserial raises several shapes
        print(f"could not open {port}: {e}", file=sys.stderr)
        print(f"ports seen: {', '.join(candidates()) or 'none'}", file=sys.stderr)
        return 1
    print(f"writing to {port} - watch the device\n")
    try:
        for i in range(20):
            st = STATES[i % len(STATES)]
            s.write((json.dumps(st) + "\n").encode())
            print(f"  -> {st}")
            time.sleep(3)
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
