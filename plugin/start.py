"""Shepherd plugin entrypoint.

Herdr starts this at session start via the [[startup]] hook in
herdr-plugin.toml, restarts it if it dies, and captures its output in
`herdr plugin log list --plugin shepherd`.

It runs until Herdr stops it. Losing the Cardputer is not fatal — the runner
reconnects with a capped backoff — and losing Herdr is not fatal either: the
frame builder marks the herd unknown and the device says so, which is the
whole point of that path.

    py -3 start.py                 # run the relay
    py -3 start.py --probe-env     # dump the injected environment and exit
    py -3 start.py --once          # one frame to the device, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import json
import logging
import os
import platform
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shepherd.frame import FrameBuilder
from shepherd.herdr import CliHerdrSource, herdr_binary
from shepherd.runner import Runner, watch_herdr
from shepherd.transport import BleTransport

HERE = Path(__file__).resolve().parent

# Herdr injects a per-plugin state directory that sits outside any repo.
# Logs of prompt text belong there, not next to the source.
STATE = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR") or (HERE / ".state"))

HERDR_VARS = [
    "HERDR_ENV", "HERDR_BIN_PATH", "HERDR_SOCKET_PATH",
    "HERDR_PLUGIN_ID", "HERDR_PLUGIN_ROOT",
    "HERDR_PLUGIN_CONFIG_DIR", "HERDR_PLUGIN_STATE_DIR",
    "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID",
]


def setup_logging() -> None:
    # cp1252 stdout cannot encode the box-drawing and typographic characters
    # that arrive in prompt text, and an encoding error in a log call would
    # take the relay down. Force UTF-8 on the way out.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def probe_env() -> int:
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cwd": os.getcwd(),
        "executable": sys.executable,
        "python": platform.python_version(),
        "herdr_vars": {k: os.environ.get(k) for k in HERDR_VARS},
        "all_herdr_prefixed": {
            k: v for k, v in os.environ.items() if k.upper().startswith("HERDR")
        },
        "herdr_binary": herdr_binary(),
    }
    print(json.dumps(record, ensure_ascii=False, indent=1))
    return 0


async def once() -> int:
    """Build one frame from the live herd and send it. Useful by hand."""
    src = CliHerdrSource()
    snap = await src.list_agents()
    if not snap.ok:
        logging.error("herd unavailable: %s", snap.reason)
    b = FrameBuilder()
    frame = b.build(snap)
    logging.info("frame: %d agents, %d bytes",
                 len(frame["a"]), len(b.encode(frame)))
    t = BleTransport()
    await t.connect()
    try:
        await t.send(b.encode(frame))
        logging.info("sent at mtu %s", t.mtu)
    finally:
        await t.close()
    return 0


async def serve() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    logging.info("shepherd starting; herdr=%s state=%s", herdr_binary(), STATE)

    runner = Runner()
    loop = asyncio.get_running_loop()

    # Herdr stops plugins by signalling them. Stopping cleanly means the
    # device sees the link drop and falls to NO SIGNAL, rather than holding a
    # frame that will never be refreshed.
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, runner.stop)

    # Herdr does not kill plugin startup processes when its session stops —
    # verified by stopping a test session and finding this relay still
    # running afterwards. Without the watchdog every Herdr restart would
    # leave another orphan competing for the same Cardputer.
    watchdog = asyncio.create_task(watch_herdr(runner))
    try:
        await runner.run()
    except asyncio.CancelledError:
        pass
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog
    logging.info("shepherd stopped")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="startup", help="how Herdr spawned this")
    ap.add_argument("--probe-env", action="store_true",
                    help="dump the injected environment and exit")
    ap.add_argument("--once", action="store_true",
                    help="send a single frame and exit")
    args = ap.parse_args()

    setup_logging()
    if args.probe_env:
        return probe_env()
    if args.once:
        return asyncio.run(once())
    return asyncio.run(serve())


if __name__ == "__main__":
    sys.exit(main())
