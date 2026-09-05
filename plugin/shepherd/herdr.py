"""Talking to Herdr.

`HerdrSource` is the seam. Everything above it — the frame builder, the
action dispatcher, the BLE transport — is written against this protocol and
therefore testable with no Herdr running and no Cardputer attached.

`CliHerdrSource` is the implementation that shells out to the `herdr` binary.
A second implementation speaking the named pipe directly is planned behind
the same protocol; see TODOS.md. The push half of the design (per-pane
`pane.agent_status_changed` subscriptions) lives in `events.py` and composes
with this rather than replacing it, because Herdr has no push path for
*membership* — `pane.agent_detected` does not stream, so discovering that an
agent exists at all will always require polling.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, Sequence

from .models import Agent, HerdSnapshot, is_pane_id

DEFAULT_TIMEOUT = 15.0


class HerdrError(RuntimeError):
    """A Herdr call failed in a way the caller must handle, not ignore."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# The seam. Tests substitute a runner that returns canned output; nothing
# spawns a process, so the whole of this module's behaviour — including every
# failure path — runs on CI with no Herdr installed.
Runner = Callable[[Sequence[str], float], Awaitable[CommandResult]]


def herdr_binary() -> str:
    """Where the `herdr` binary is.

    Herdr injects HERDR_BIN_PATH into every process it spawns, including
    plugin startup hooks and actions (verified on 0.8.2). Preferring it over
    a PATH lookup is what the plugin docs ask for, and it means the relay
    works even when PATH is not what the CLI expects.
    """
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"


async def subprocess_runner(argv: Sequence[str], timeout: float) -> CommandResult:
    """Run a command as an argv list. Never a shell, never a format string.

    `create_subprocess_exec`, not `run`, because this process also drives a
    BLE link: a blocking subprocess call would stall the event loop and the
    device would see the link go quiet for the duration.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HerdrError(f"timed out after {timeout:g}s: {argv[0]} {' '.join(argv[1:3])}")
    return CommandResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
    )


class HerdrSource(Protocol):
    """What Shepherd needs from Herdr. Nothing more."""

    async def list_agents(self) -> HerdSnapshot: ...

    async def read_pane(self, pane_id: str, lines: int = ...) -> str | None: ...

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None: ...

    async def focus(self, pane_id: str) -> None: ...


class CliHerdrSource:
    """HerdrSource backed by the `herdr` CLI."""

    def __init__(
        self,
        runner: Runner | None = None,
        binary: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._run = runner or subprocess_runner
        self._bin = binary or herdr_binary()
        self._timeout = timeout

    # -- reads ---------------------------------------------------------

    async def list_agents(self) -> HerdSnapshot:
        """Who is out there, and what are they doing.

        Returns a snapshot with ok=False rather than raising, because the
        caller must render *something* and "I cannot see" is a renderable
        state. Raising here would tempt callers into keeping the last good
        frame on screen, which is the one failure this product cannot have.
        """
        try:
            res = await self._run([self._bin, "agent", "list"], self._timeout)
        except HerdrError as e:
            return HerdSnapshot(ok=False, reason=str(e))
        except OSError as e:
            return HerdSnapshot(ok=False, reason=f"cannot run {self._bin}: {e}")

        if not res.ok:
            detail = (res.stderr or res.stdout or "").strip().splitlines()
            first = detail[0][:200] if detail else f"exit {res.returncode}"
            return HerdSnapshot(ok=False, reason=first)

        try:
            payload = json.loads(res.stdout)
            raw_agents = payload["result"]["agents"]
        except (ValueError, KeyError, TypeError) as e:
            return HerdSnapshot(ok=False, reason=f"unparseable agent list: {e}")

        # `agents` being present but not a list is its own failure mode, and
        # it has to be caught here rather than by the comprehension below —
        # iterating a non-iterable would raise past every guard in this
        # method and reach the event loop as an unhandled exception.
        if not isinstance(raw_agents, list):
            return HerdSnapshot(
                ok=False,
                reason=f"agent list is {type(raw_agents).__name__}, not a list",
            )

        agents = tuple(
            Agent.from_cli(r)
            for r in raw_agents
            if isinstance(r, dict) and is_pane_id(r.get("pane_id"))
        )
        return HerdSnapshot(agents=agents, ok=True)

    async def read_pane(self, pane_id: str, lines: int = 40) -> str | None:
        """The pane's detection buffer — where a permission prompt lives.

        `--source detection` is Herdr's own plain-text bottom-buffer snapshot,
        the same view its agent-status rules run against. That makes it the
        right surface for reading a prompt's option labels, which is how the
        action layer decides what to send.

        Returns None on any failure. A missing prompt is not an error worth
        propagating: the agent is still blocked, the device just cannot show
        the question text.
        """
        self._require_pane(pane_id)
        argv = [
            self._bin, "agent", "read", pane_id,
            "--source", "detection",
            "--lines", str(int(lines)),
        ]
        try:
            res = await self._run(argv, self._timeout)
        except (HerdrError, OSError):
            return None
        return res.stdout if res.ok else None

    # -- writes --------------------------------------------------------
    #
    # Both writes take a pane_id that must already have been allowlisted by
    # the caller against the latest snapshot. The shape check below is a
    # backstop, not the policy.

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        """Send logical keys to the agent in a pane.

        Herdr validates key names before writing any bytes, so an unknown key
        fails loudly here rather than landing as literal text in someone's
        prompt. Keys arrive as separate argv entries; they are never joined
        into a string.
        """
        self._require_pane(pane_id)
        if not keys:
            raise HerdrError("send_keys requires at least one key")
        for k in keys:
            if not isinstance(k, str) or not k or k.startswith("-"):
                raise HerdrError(f"refusing suspicious key: {k!r}")
        res = await self._run(
            [self._bin, "agent", "send-keys", pane_id, *keys], self._timeout
        )
        if not res.ok:
            raise HerdrError(
                f"send-keys failed on {pane_id}: "
                f"{(res.stderr or res.stdout).strip()[:200]}"
            )

    async def focus(self, pane_id: str) -> None:
        self._require_pane(pane_id)
        res = await self._run([self._bin, "agent", "focus", pane_id], self._timeout)
        if not res.ok:
            raise HerdrError(
                f"focus failed on {pane_id}: "
                f"{(res.stderr or res.stdout).strip()[:200]}"
            )

    # -- internals -----------------------------------------------------

    @staticmethod
    def _require_pane(pane_id: str) -> None:
        if not is_pane_id(pane_id):
            raise HerdrError(f"not a pane id: {pane_id!r}")
