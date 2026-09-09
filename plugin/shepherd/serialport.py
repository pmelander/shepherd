"""Opening a serial port to an M5Stack board without rebooting it.

This exists because the obvious call is a trap, and the trap costs a whole
diagnosis rather than an error message:

    serial.Serial("COM5", 115200)          # reboots the board

pyserial asserts DTR (and RTS) when it opens a port, and on these boards DTR
is wired to the auto-reset line. So the act of opening the port to read the
device resets the device. On Shepherd this presented as *BLE instability* -
the relay logged repeated "link lost / device connected" cycles - and the tell
was that every uptime sample came back at 13-21 seconds. An uptime that is
always small is not a device rebooting on its own schedule, it is one
rebooting on yours.

The fix is to deassert both lines BEFORE open():

    p = serial.Serial()
    p.port = "COM5"; p.baudrate = 115200
    p.dtr = False; p.rts = False
    p.open()

`pio device monitor` does this correctly, which is why it is safe and a
hand-rolled reader is not.

Two modes, because both are legitimate:

  * `reset=False` (the default) is for anything that watches or streams to a
    running device - Bruno's frame follower above all. A reset mid-session
    loses the screen and the device's uptime.
  * `reset=True` deliberately pulses the board and waits for it to boot. This
    is what a file-transfer tool wants: upstream's xfer script did exactly
    that on purpose, and the point here is to make the intent explicit rather
    than incidental to whichever pyserial call got written.

`serial` is imported lazily, inside the functions. The relay's hard dependency
set is bleak alone, and a missing pyserial must not stop a BLE relay from
starting on a machine that has no Bruno. The same idiom is used in
singleton.py, which imports msvcrt or fcntl inside the function that needs it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Sequence

log = logging.getLogger("shepherd.serialport")

DEFAULT_BAUD = 115200

# How long a board needs after a deliberate reset before it will answer.
# Upstream used 2s and it works; kept rather than tuned.
RESET_SETTLE = 2.0

# USB-serial bridges these boards actually appear as. The Cardputer ADV is an
# ESP32-S3 with native USB (USB-Serial/JTAG), the Core Basic is a classic
# ESP32 behind an external bridge, so both families need covering. Matched
# case-insensitively against the port description and hwid.
_LIKELY = (
    "usb-serial",       # generic
    "usb serial",
    "ch340",            # very common M5Stack bridge
    "ch910",            # CH9102
    "cp210",            # Silicon Labs, the other common one
    "ftdi",
    "usb-enhanced-serial",
    "esp32",
    "usb jtag",         # S3 native USB-Serial/JTAG
    "usb-serial/jtag",
    "m5stack",
)


def candidates(ports: Sequence[Any] | None = None) -> list[str]:
    """Serial ports that look like an M5Stack board, best guess first.

    Cross-platform on purpose. The two vendored probe scripts globbed
    `/dev/cu.usbserial-*`, a macOS path, so neither could ever run on the
    Windows host this project is developed on - "fixed but still unrunnable"
    is not fixed.

    Pass `ports` (a sequence of pyserial ListPortInfo-alikes) to test this
    without hardware.
    """
    if ports is None:
        from serial.tools import list_ports  # noqa: PLC0415 - see module docstring

        ports = list(list_ports.comports())

    def looks_right(p: Any) -> bool:
        blob = " ".join(
            str(getattr(p, attr, "") or "")
            for attr in ("description", "hwid", "manufacturer", "product")
        ).lower()
        return any(token in blob for token in _LIKELY)

    likely = [str(p.device) for p in ports if looks_right(p)]
    rest = [str(p.device) for p in ports if not looks_right(p)]
    # Everything else still gets returned, just last: a board behind an
    # unrecognised bridge is better offered than hidden.
    return likely + rest


def open_port(
    port: str | None = None,
    baud: int = DEFAULT_BAUD,
    *,
    reset: bool = False,
    timeout: float | None = None,
    factory: Callable[[], Any] | None = None,
    finder: Callable[[], list[str]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Open `port` (or the best candidate) without resetting the board.

    `factory` builds the unopened serial object and exists so tests can assert
    the flag-before-open ordering, which is the entire point of this function
    and is invisible from the outside once the port is open. `finder` is
    injectable for the same reason: a test for "no port found" must not depend
    on what happens to be plugged into the machine running it.

    Raises RuntimeError when no port can be found, rather than sys.exit() -
    the vendored scripts exited at import, which is what made bare `pytest`
    collapse with "SystemExit: no stick found".
    """
    if port is None:
        found = (finder or candidates)()
        if not found:
            raise RuntimeError(
                "no serial port found; pass one explicitly, or check the "
                "board is plugged in and its USB bridge driver is installed"
            )
        port = found[0]
        log.info("using serial port %s (auto-detected)", port)

    if factory is None:
        import serial  # noqa: PLC0415 - see module docstring

        factory = serial.Serial

    p = factory()
    p.port = port
    p.baudrate = baud
    if timeout is not None:
        p.timeout = timeout

    # THE WHOLE POINT. These two assignments must happen before open(), not
    # after: setting them afterwards means the reset has already fired.
    p.dtr = bool(reset)
    p.rts = False

    p.open()

    if reset:
        # Deliberate: let the DTR-triggered reset finish booting before
        # anything is written, then drop whatever boot chatter arrived.
        sleep(RESET_SETTLE)
        p.reset_input_buffer()

    return p
