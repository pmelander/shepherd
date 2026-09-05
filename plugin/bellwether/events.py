"""Push notifications from Herdr, over its named pipe.

Why this exists alongside the polling in `herdr.py` rather than replacing it:
Herdr's subscription for `pane.agent_status_changed` **requires a pane_id**.
It is per-pane, not global. And `pane.agent_detected` does not stream at all
— `SubscriptionEventKind` carries only `pane.output_matched`,
`pane.agent_status_changed` and `pane.scroll_changed`. So discovering that an
agent *exists* will always need a poll; this module only makes transitions
instant once you know who to watch.

All of the following was verified live against Herdr 0.8.2, protocol 20:

  request   {"id": ..., "method": ..., "params": ...}   newline-delimited
  ack       {"id": ..., "result": {"type": "subscription_started"}}
  error     {"error": {"code": ..., "message": ...}}
  push      {"event": "pane.agent_status_changed",
             "data": {"agent", "agent_status", "pane_id", "workspace_id"}}

Note the spelling. Herdr uses four different forms for this one event and
mixing them fails silently rather than loudly:

  events.subscribe  params.subscriptions[].type   DOTTED       <- we send this
  events.wait       params.match_event.event      underscored
  the event record  type const                    underscored
  the push envelope event                         DOTTED       <- we match this

The push payload is thin: it says which pane changed and to what, and carries
no state_change_seq, cwd or title. Rendering a row still needs the poll.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Protocol, Sequence

from .models import AgentStatus, is_pane_id

# Dotted, because this is both what events.subscribe accepts and what the
# push envelope carries. See the module docstring.
EVENT_AGENT_STATUS_CHANGED = "pane.agent_status_changed"

DEFAULT_CONNECT_TIMEOUT = 10.0


class EventStreamError(RuntimeError):
    """The event stream could not be established or has ended."""


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """One pushed agent-status transition."""

    pane_id: str
    workspace_id: str
    status: AgentStatus
    kind: str

    @classmethod
    def from_data(cls, data: dict) -> "StatusEvent | None":
        pane_id = data.get("pane_id")
        if not is_pane_id(pane_id):
            return None
        return cls(
            pane_id=pane_id,
            workspace_id=data.get("workspace_id", ""),
            status=AgentStatus.parse(data.get("agent_status")),
            kind=data.get("agent", ""),
        )


def socket_path() -> str:
    """Where Herdr's socket is.

    HERDR_SOCKET_PATH is injected into plugin-spawned processes (verified on
    0.8.2), so it is preferred over reconstructing the path. On Windows the
    pipe is named after that path under \\\\.\\pipe\\.
    """
    raw = os.environ.get("HERDR_SOCKET_PATH")
    if not raw:
        appdata = os.environ.get("APPDATA", "")
        raw = os.path.join(appdata, "herdr", "herdr.sock")
    if sys.platform == "win32":
        return r"\\.\pipe" + "\\" + raw
    return raw


# ---------------------------------------------------------------- pure bits
#
# Kept free of I/O so the wire format is pinned by fast tests rather than by
# a live Herdr. These are where the four-way naming trap would bite.


def build_subscribe_request(
    pane_ids: Sequence[str],
    statuses: Iterable[AgentStatus] | None = None,
    request_id: str = "bellwether-events",
) -> dict:
    """Build an events.subscribe request for a set of panes.

    `statuses` narrows the stream at the source. Passing (BLOCKED,) means
    Herdr only pushes transitions into blocked, which is the cheapest possible
    feed for a battery device. Passing None subscribes to every transition.
    """
    panes = [p for p in pane_ids if is_pane_id(p)]
    if not panes:
        raise ValueError("no valid pane ids to subscribe to")

    subs: list[dict] = []
    for pane_id in panes:
        if statuses is None:
            subs.append({"type": EVENT_AGENT_STATUS_CHANGED, "pane_id": pane_id})
        else:
            for st in statuses:
                subs.append({
                    "type": EVENT_AGENT_STATUS_CHANGED,
                    "pane_id": pane_id,
                    "agent_status": st.value,
                })
    return {
        "id": request_id,
        "method": "events.subscribe",
        "params": {"subscriptions": subs},
    }


def encode_request(req: dict) -> bytes:
    return (json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class Ack:
    request_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class Failure:
    code: str
    message: str


def parse_line(line: str) -> StatusEvent | Ack | Failure | None:
    """Classify one newline-delimited frame from the socket.

    Returns None for anything unrecognised rather than raising: an unknown
    frame type is not a reason to tear down a working event stream.
    """
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None

    if "error" in msg and isinstance(msg["error"], dict):
        err = msg["error"]
        return Failure(
            code=str(err.get("code", "")), message=str(err.get("message", ""))
        )

    if "event" in msg:
        if msg.get("event") != EVENT_AGENT_STATUS_CHANGED:
            return None
        data = msg.get("data")
        return StatusEvent.from_data(data) if isinstance(data, dict) else None

    if "result" in msg and isinstance(msg["result"], dict):
        return Ack(
            request_id=str(msg.get("id", "")),
            kind=str(msg["result"].get("type", "")),
        )
    return None


# ------------------------------------------------------------- the seam


class EventSource(Protocol):
    """Push transitions for a known set of panes."""

    def watch(
        self,
        pane_ids: Sequence[str],
        statuses: Iterable[AgentStatus] | None = ...,
    ) -> AsyncIterator[StatusEvent]: ...


class PipeEventSource:
    """EventSource over Herdr's local socket.

    On Windows this is a named pipe, reached through the Proactor loop's
    `create_pipe_connection` rather than a blocking `open()` in a thread. The
    blocking version works — the probe used it — but it cannot be cancelled
    cleanly, and this process also drives a BLE link that must shut down
    promptly. Proper cancellation is worth the platform-specific call.
    """

    def __init__(
        self,
        path: str | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self._path = path or socket_path()
        self._connect_timeout = connect_timeout

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        if sys.platform == "win32":
            connect = loop.create_pipe_connection(lambda: protocol, self._path)
        else:
            connect = loop.create_unix_connection(lambda: protocol, self._path)
        transport, _ = await asyncio.wait_for(connect, timeout=self._connect_timeout)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        return reader, writer

    async def watch(
        self,
        pane_ids: Sequence[str],
        statuses: Iterable[AgentStatus] | None = None,
    ) -> AsyncIterator[StatusEvent]:
        """Yield transitions until the stream ends or the caller stops.

        Ending is normal, not exceptional — Herdr restarting closes the pipe.
        The caller is expected to notice, mark the herd unknown, and
        re-establish once a poll succeeds again. That is deliberately not
        handled in here: silent auto-reconnect would let the device keep
        rendering a stale herd while this layer quietly retried.
        """
        req = build_subscribe_request(pane_ids, statuses)
        try:
            reader, writer = await self._open()
        except (OSError, asyncio.TimeoutError) as e:
            raise EventStreamError(f"cannot open {self._path}: {e}") from e

        try:
            writer.write(encode_request(req))
            await writer.drain()

            while True:
                raw = await reader.readline()
                if not raw:
                    return  # pipe closed
                parsed = parse_line(raw.decode("utf-8", "replace"))
                if isinstance(parsed, Failure):
                    raise EventStreamError(
                        f"herdr refused subscription: {parsed.code}: {parsed.message}"
                    )
                if isinstance(parsed, StatusEvent):
                    yield parsed
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - shutdown must not mask the real error
                pass
