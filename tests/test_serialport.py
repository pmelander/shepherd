"""Tests for the serial-open helper.

The thing worth testing here is ORDERING, and it is invisible once the port is
open: DTR must be deasserted BEFORE open(), because setting it afterwards
means the board has already reset. A test that only checked the final flag
values would pass against the bug. So the fake records the sequence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.serialport import (  # noqa: E402
    DEFAULT_BAUD,
    RESET_SETTLE,
    candidates,
    open_port,
)


class FakeSerial:
    """Records what happened to it, in order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.port = None
        self.baudrate = None
        self.timeout = None
        self._dtr = None
        self._rts = None
        self.opened = False

    # dtr/rts as properties so assignment is observable in sequence.
    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, v):
        self._dtr = v
        self.events.append(f"dtr={v}")

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, v):
        self._rts = v
        self.events.append(f"rts={v}")

    def open(self):
        self.opened = True
        self.events.append("open")

    def reset_input_buffer(self):
        self.events.append("reset_input_buffer")


class FakePort:
    def __init__(self, device, description="", hwid="", manufacturer="", product=""):
        self.device = device
        self.description = description
        self.hwid = hwid
        self.manufacturer = manufacturer
        self.product = product


def test_dtr_is_deasserted_before_open_not_after():
    # The bug this whole module exists to prevent. Asserting only on the final
    # value of dtr would pass even if it were set after open().
    fake = FakeSerial()
    open_port("COM5", factory=lambda: fake)

    assert fake.events.index("dtr=False") < fake.events.index("open")
    assert fake.events.index("rts=False") < fake.events.index("open")
    assert fake.opened


def test_the_default_does_not_reset_the_board():
    fake = FakeSerial()
    open_port("COM5", factory=lambda: fake)
    assert fake.dtr is False
    # No settle sleep and no buffer flush: nothing was reset, so there is no
    # boot chatter to discard.
    assert "reset_input_buffer" not in fake.events


def test_reset_true_asserts_dtr_and_waits_for_the_boot():
    fake = FakeSerial()
    slept: list[float] = []
    open_port("COM5", reset=True, factory=lambda: fake, sleep=slept.append)

    assert fake.dtr is True
    assert fake.events.index("dtr=True") < fake.events.index("open")
    # Settles AFTER open, then drops the boot chatter. Flushing before the
    # reset had finished would flush nothing useful.
    assert slept == [RESET_SETTLE]
    assert fake.events.index("open") < fake.events.index("reset_input_buffer")


def test_port_and_baud_are_set_before_open():
    fake = FakeSerial()
    open_port("COM9", baud=921600, timeout=2.0, factory=lambda: fake)
    assert fake.port == "COM9"
    assert fake.baudrate == 921600
    assert fake.timeout == 2.0


def test_timeout_is_left_alone_when_not_given():
    # pyserial's own default (None, meaning block) must survive; passing
    # timeout=None explicitly would be indistinguishable from not passing it.
    fake = FakeSerial()
    open_port("COM5", factory=lambda: fake)
    assert fake.timeout is None
    assert fake.baudrate == DEFAULT_BAUD


def test_a_recognised_bridge_sorts_ahead_of_an_unknown_one():
    ports = [
        FakePort("COM3", description="Standard Serial over Bluetooth link"),
        FakePort("COM7", description="USB-SERIAL CH340"),
    ]
    assert candidates(ports) == ["COM7", "COM3"]


def test_an_unrecognised_port_is_still_offered_last_not_hidden():
    # A board behind a bridge nobody listed is better offered than dropped.
    ports = [FakePort("COM4", description="Something nobody has heard of")]
    assert candidates(ports) == ["COM4"]


def test_detection_is_not_macos_only():
    # The two vendored probe scripts globbed /dev/cu.usbserial-*, so neither
    # could run on the Windows host this is developed on. Windows-shaped
    # device names must be found.
    ports = [FakePort("COM5", hwid="USB VID:PID=1A86:7523")]
    assert candidates(ports) == ["COM5"]
    ports = [FakePort("/dev/cu.usbserial-0001", description="USB-Serial")]
    assert candidates(ports) == ["/dev/cu.usbserial-0001"]


def test_no_port_raises_rather_than_exiting_the_interpreter():
    # sys.exit() at import is what made bare `pytest` die with
    # "INTERNALERROR> SystemExit: no stick found" instead of running tests.
    # finder is injected so this does not depend on what is plugged into the
    # machine running the suite.
    with pytest.raises(RuntimeError, match="no serial port found"):
        open_port(port=None, factory=lambda: FakeSerial(), finder=lambda: [])


def test_auto_detection_picks_the_best_candidate():
    fake = FakeSerial()
    open_port(port=None, factory=lambda: fake, finder=lambda: ["COM7", "COM3"])
    assert fake.port == "COM7"
