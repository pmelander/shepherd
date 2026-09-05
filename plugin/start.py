"""Bellwether plugin entrypoint.

Right now this is the probe-3 instrument: it records what environment Herdr
hands a plugin-spawned process, so the relay can be written against facts
rather than assumptions. It appends one JSON record per invocation and exits.

The open question it answers: does a hook-spawned process inherit HERDR_ENV=1
and the caller context vars (HERDR_WORKSPACE_ID / HERDR_TAB_ID / HERDR_PANE_ID),
and is HERDR_BIN_PATH set? The plugin docs say HERDR_BIN_PATH is how a plugin
should reach the CLI portably; everything else is unverified.

This becomes the relay entrypoint once the answer is in.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECORD = HERE / ".probe3-env.jsonl"

# Everything Herdr might plausibly inject. Recorded even when absent, because
# "absent" is the finding for most of these.
HERDR_VARS = [
    "HERDR_ENV",
    "HERDR_BIN_PATH",
    "HERDR_WORKSPACE_ID",
    "HERDR_TAB_ID",
    "HERDR_PANE_ID",
    "HERDR_SESSION",
    "HERDR_PLUGIN_ID",
    "HERDR_PLUGIN_DIR",
    "HERDR_PLUGIN_CONFIG_DIR",
    "HERDR_SOCKET",
]


def cli_reachable() -> dict:
    """Can this process actually drive Herdr? That is the point of the probe."""
    binpath = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        out = subprocess.run(
            [binpath, "agent", "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001 - probe records the failure verbatim
        return {"ok": False, "via": binpath, "error": repr(e)}

    if out.returncode != 0:
        return {
            "ok": False,
            "via": binpath,
            "returncode": out.returncode,
            "stderr": out.stderr[:400],
        }
    try:
        agents = json.loads(out.stdout)["result"]["agents"]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "via": binpath, "unparseable": repr(e)}
    return {"ok": True, "via": binpath, "agent_count": len(agents)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="unknown",
                    help="how this invocation was spawned: startup | action")
    args = ap.parse_args()

    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "mode": args.mode,
        "cwd": os.getcwd(),
        "argv": sys.argv,
        "executable": sys.executable,
        "python": platform.python_version(),
        "pid": os.getpid(),
        "herdr_vars": {k: os.environ.get(k) for k in HERDR_VARS},
        "herdr_var_count_present": sum(
            1 for k in HERDR_VARS if os.environ.get(k) is not None
        ),
        "all_herdr_prefixed": {
            k: v for k, v in os.environ.items() if k.upper().startswith("HERDR")
        },
        "cli": cli_reachable(),
    }

    with RECORD.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Also to stdout, which should land in `herdr plugin log list`.
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
