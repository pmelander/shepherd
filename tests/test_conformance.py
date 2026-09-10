"""The host's limits and the device's limits, checked against each other.

`firmware/platformio.ini` says the reason out loud: "The device-side rules
must stay in step with plugin/shepherd/frame.py, and a board on a desk is the
worst possible place to discover they have drifted - it needs a flash, a
physical reset, and gives no assertion output."

Up to now the two sides were kept in step by comments. `shepherd_frame.h`
says "Mirrors MAX_OPTIONS / OPTION_MAX in plugin/shepherd/frame.py";
`tests/test_transport.py` says "Read off firmware/src/ble_bridge.cpp. If these
drift, the device is..." Comments do not fail a build. This does.

The relationships are NOT all equality, and encoding that is the point. Some
device buffers are deliberately larger than the host's limit - the host cuts a
recap to 64 characters with an ASCII "..." and the device holds 72, rounded up
for headroom - so asserting blind equality would force the two to move
together for no reason. Each pair below states the relationship it actually
needs, and says what breaks if it is violated.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugin"))

from shepherd import frame as F  # noqa: E402
from shepherd.recap import BODY_MAX  # noqa: E402

_DEFINE = re.compile(r"^\s*#define\s+([A-Z_][A-Z0-9_]*)\s+(.+?)\s*$", re.M)


def defines(*names: str) -> dict[str, int]:
    """Integer #defines scraped from the firmware headers.

    Deliberately reads the real headers rather than duplicating their values
    here, because a copy in this file would be one more thing to drift.
    """
    raw: dict[str, str] = {}
    for header in ("shepherd_frame.h", "bruno_frame.h", "line_buf.h"):
        text = (REPO / "firmware" / "src" / header).read_text(encoding="utf-8")
        for name, value in _DEFINE.findall(text):
            # Strip trailing line comments; keep the first definition seen.
            raw.setdefault(name, value.split("//")[0].split("/*")[0].strip())

    out: dict[str, int] = {}
    for name in names:
        if name not in raw:
            pytest.fail(f"{name} is not defined in any firmware header - it "
                        f"was renamed or removed, and this test is now the "
                        f"only thing that knows the pair existed")
        expr = raw[name]
        # Resolve references to other defines, then evaluate the arithmetic.
        for other, val in raw.items():
            expr = re.sub(rf"\b{other}\b", f"({val})", expr)
        try:
            out[name] = int(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"cannot read {name} = {raw[name]!r} as an int ({e})")
    return out


# --------------------------------------------------------- must be equal


def test_the_protocol_version_matches():
    # The one that fails loudest and most usefully: the device REFUSES a
    # version it does not recognise and says so on screen, so a mismatch
    # blacks out the Cardputer until it is reflashed.
    d = defines("SHEPHERD_PROTOCOL_VERSION")
    assert d["SHEPHERD_PROTOCOL_VERSION"] == F.PROTOCOL_VERSION


def test_the_agent_cap_matches():
    # The device sizes its frame struct on this. A host that sends more agents
    # than the device can hold would have the extra silently dropped, and the
    # frame would also grow past the line buffer.
    d = defines("SHEPHERD_MAX_AGENTS")
    assert d["SHEPHERD_MAX_AGENTS"] == F.MAX_AGENTS


def test_the_option_cap_matches():
    # The device cycles options and sends back an INDEX. If the two sides
    # disagree about how many there can be, an index means two different
    # things - which is the class of bug the fifth signed field exists to
    # prevent.
    d = defines("SHEPHERD_MAX_OPTIONS")
    assert d["SHEPHERD_MAX_OPTIONS"] == F.MAX_OPTIONS


def test_the_bubble_limit_matches():
    # BRUNO_SAID_MAX says it mirrors SAID_MAX. A device limit BELOW the host's
    # would silently cut the end off a sentence - and the end is the half
    # worth keeping, which is why truncate_body drops the beginning.
    d = defines("BRUNO_SAID_MAX")
    assert d["BRUNO_SAID_MAX"] == F.SAID_MAX


# ------------------------------------------- device must have room to spare


@pytest.mark.parametrize(
    "device_name, host_limit, what",
    [
        ("SHEPHERD_OPTION_LEN", F.OPTION_MAX, "an option label"),
        ("SHEPHERD_QUESTION_LEN", F.PROMPT_MAX, "a blocking question"),
        ("SHEPHERD_RECAP_LEN", F.RECAP_MAX, "an agent's recap"),
        ("SHEPHERD_BODY_LEN", BODY_MAX, "a detail body"),
        ("BRUNO_SAID_LEN", F.SAID_MAX, "an announcement sentence"),
    ],
)
def test_the_device_buffer_holds_what_the_host_sends(device_name, host_limit, what):
    # Room for the host's limit plus a NUL, at minimum. These are NOT equal on
    # purpose - the device rounds up for headroom - so this is >=, not ==.
    d = defines(device_name)
    assert d[device_name] >= host_limit + 1, (
        f"{device_name} is {d[device_name]}, which cannot hold {what} of "
        f"{host_limit} characters plus a NUL. The host would send text the "
        f"device silently truncates."
    )


def test_the_device_alias_buffer_holds_the_hosts_alias():
    d = defines("SHEPHERD_ALIAS_LEN")
    assert d["SHEPHERD_ALIAS_LEN"] >= F.ALIAS_LEN + 1


# ----------------------------------------------- the frame must fit the line


def test_the_line_buffer_holds_the_worst_case_the_host_can_build():
    # The bug this whole pairing exists to catch, and the one that actually
    # shipped: both receive buffers were smaller than the largest frame the
    # host could emit, and the failure was a screen that stopped updating with
    # nothing logged anywhere.
    d = defines("SHEPHERD_LINE_MAX", "SHEPHERD_WORST_FRAME")
    from shepherd.models import Agent, AgentStatus, HerdSnapshot

    agents = tuple(
        Agent(
            pane_id=f"w{i:02d}:pane{i:02d}",
            workspace_id=f"w{i:02d}",
            status=AgentStatus.BLOCKED,
            state_change_seq=1000 + i,
            cwd=rf"C:\.workspaces\some-fairly-long-workspace-name-{i}",
            terminal_title="x" * F.RECAP_MAX,
            kind="claude",
            focused=False,
        )
        for i in range(F.MAX_AGENTS)
    )
    prompts = {a.pane_id: "y" * 400 for a in agents}
    options = {
        a.pane_id: tuple(("O" * F.OPTION_MAX, "w") for _ in range(F.MAX_OPTIONS))
        for a in agents
    }
    b = F.FrameBuilder()
    snap = HerdSnapshot(agents=agents, ok=True)
    b.build(snap, prompts, options)          # seed _seen so `e` appears
    worst = len(b.encode(b.build(snap, prompts, options)))

    assert worst < d["SHEPHERD_LINE_MAX"], (
        f"the worst-case frame is {worst} bytes and the device's line buffer "
        f"is {d['SHEPHERD_LINE_MAX']}. An over-long line is discarded, so "
        f"this presents as a stale screen, not an error."
    )
    assert worst <= d["SHEPHERD_WORST_FRAME"], (
        f"the worst-case frame grew to {worst}, past the "
        f"{d['SHEPHERD_WORST_FRAME']} recorded in firmware/src/line_buf.h. "
        f"Update SHEPHERD_WORST_FRAME (and check the headroom note beside "
        f"SHEPHERD_LINE_MAX) rather than deleting this assertion."
    )


def test_the_scraper_actually_read_the_headers():
    # Guards every test above from passing vacuously. If the regex stopped
    # matching, defines() would fail the tests it is used in - but only if it
    # is used, so assert the mechanism itself works too.
    d = defines("SHEPHERD_PANE_LEN", "SHEPHERD_PROTOCOL_VERSION")
    assert d["SHEPHERD_PANE_LEN"] > 0
    assert d["SHEPHERD_PROTOCOL_VERSION"] > 0
    # pytest.fail raises Failed, which derives from BaseException rather than
    # Exception - so `pytest.raises(Exception)` here silently would not catch
    # it and this guard would fail rather than pass.
    with pytest.raises(pytest.fail.Exception):
        defines("SHEPHERD_DEFINITELY_NOT_A_REAL_DEFINE")
