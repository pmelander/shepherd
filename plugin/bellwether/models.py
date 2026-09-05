"""What Bellwether knows about a herd of agents.

Field names and the status vocabulary come from Herdr's own `agent list`
output, verified live against Herdr 0.8.2 (socket protocol 20). Nothing here
is invented; if Herdr renames something this module is where it shows.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

# Herdr pane ids are workspace-qualified, e.g. "w2:p1" or "wA:p1". They are
# the only join key Bellwether uses, so workspace_id is redundant for lookup.
#
# This pattern is a cheap second line of defence, not the primary one. The
# real guard is the allowlist in the actions layer, which only accepts a
# pane_id that appeared in the most recent snapshot. But anything that ends
# up in an argv list gets shape-checked here too, so a malformed id cannot
# reach a subprocess even if a caller forgets.
PANE_ID_RE = re.compile(r"^w[0-9A-Za-z]+:p[0-9]+$")


def is_pane_id(value: object) -> bool:
    return isinstance(value, str) and bool(PANE_ID_RE.match(value))


class AgentStatus(enum.Enum):
    """Herdr's AgentStatus enum, verbatim.

    The wire values are exactly what `herdr api schema` declares. A
    conformance test asserts this set still matches the live schema, so a
    future Herdr release cannot quietly add a state the device renders blank.

    On the distinction that matters for this product: DONE and IDLE are the
    same underlying readiness state. Herdr separates them by whether the tab
    has been *seen* in the focused UI, so DONE means "finished, and you have
    not looked yet". CLI reads do not mark a tab seen. Measured block rates
    make DONE the common notification event and BLOCKED the rare one, so DONE
    is not a synonym to be folded into IDLE.
    """

    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: object) -> "AgentStatus":
        """Never raise on an unrecognised status.

        A status Bellwether does not know is strictly better rendered as
        UNKNOWN than as a crash or, worse, as a stale previous value.
        """
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass
        return cls.UNKNOWN

    @property
    def needs_attention(self) -> bool:
        """Whether a human is being waited on, or has something unread."""
        return self in (AgentStatus.BLOCKED, AgentStatus.DONE)


@dataclass(frozen=True, slots=True)
class Agent:
    """One agent, as Herdr reports it."""

    pane_id: str
    workspace_id: str
    status: AgentStatus
    # Monotonic per-agent counter that advances only on a state change.
    # Herdr maintains it, so it is authoritative for change detection and
    # cheaper than diffing whole records.
    state_change_seq: int
    # Working directory basename is the most human-recognisable label Herdr
    # offers. The display alias is derived later, in the frame builder.
    cwd: str
    terminal_title: str
    kind: str
    focused: bool

    @classmethod
    def from_cli(cls, raw: dict) -> "Agent":
        pane_id = raw.get("pane_id", "")
        return cls(
            pane_id=pane_id,
            workspace_id=raw.get("workspace_id", ""),
            status=AgentStatus.parse(raw.get("agent_status")),
            state_change_seq=int(raw.get("state_change_seq") or 0),
            cwd=raw.get("cwd", ""),
            terminal_title=raw.get("terminal_title_stripped", ""),
            kind=raw.get("agent", ""),
            focused=bool(raw.get("focused")),
        )


@dataclass(frozen=True, slots=True)
class HerdSnapshot:
    """The result of asking Herdr who is out there.

    `ok` is False when the CLI could not be reached or its output could not be
    parsed. In that case `agents` is empty and `reason` says why.

    Deliberately, a failed snapshot does NOT carry the previous agents marked
    unknown. This type reports what Herdr actually said; substituting
    last-known membership is the frame builder's job, because only it knows
    what the device is currently displaying. Keeping that out of here is what
    stops a transport-layer hiccup from silently inventing herd state.
    """

    agents: tuple[Agent, ...] = ()
    ok: bool = True
    reason: str | None = None

    def by_pane(self) -> dict[str, Agent]:
        return {a.pane_id: a for a in self.agents}

    @property
    def pane_ids(self) -> frozenset[str]:
        return frozenset(a.pane_id for a in self.agents)
