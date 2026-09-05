"""Turning what Herdr said into what goes on the wire.

Pure logic, no I/O, no clock of its own beyond an injectable `now`. This is
the most testable layer in the project and it carries most of the decisions
the review argued about, so the tests here are the record of those decisions.

The frame is newline-delimited UTF-8 JSON over Nordic UART. Field names are
one or two characters because the device has no PSRAM, a 2048-byte receive
ring, and an ATT MTU that Windows may negotiate down to 23 — every byte is
another notify round trip.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping

from .models import Agent, AgentStatus, HerdSnapshot

# Bumped whenever the frame's shape changes. The device refuses a mismatch
# and says so on screen, rather than rendering fields it does not understand.
# This is the one piece of protocol ceremony kept after the review trimmed
# resync and gap detection: those guard against reordering that an ACKed link
# cannot produce, whereas flashing firmware and forgetting which relay it
# matches is a thing that genuinely happens.
PROTOCOL_VERSION = 1

# Display alias width. Long enough to tell Yield_PriceManager_API from
# Yield_InitialPricing_Service, short enough for a 40-column grid.
ALIAS_LEN = 8

# Prompt text is truncated by the host so the device never has to wrap or
# reflow. The action layer refuses to *approve* anything that was truncated,
# so this bound is a display concern that deliberately has a safety
# consequence: a command too long to show in full cannot be approved blind.
PROMPT_MAX = 100

# Upper bound on agents in one frame. Not a product limit — a buffer limit.
# The device's RX ring is 2048 bytes; a frame is roughly 65 bytes per idle
# agent and ~185 for a blocked one, so twelve leaves comfortable headroom
# even with two prompts in flight.
MAX_AGENTS = 12

_ALIAS_STRIP = re.compile(r"[^A-Za-z0-9]+")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_alias(cwd: str) -> str:
    """A stable, human-recognisable short name for a workspace.

    Truncation was the review's original plan and it is the wrong tool for an
    identity you are about to approve a command against: `Yield_PriceManager`
    and `Yield_PricingManager` truncate to the same thing. Splitting on
    separators and taking leading characters from each part keeps the
    distinguishing letters, which is what matters when the label is the only
    thing telling you which repo is about to be force-pushed.
    """
    base = (cwd or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    parts = [p for p in _ALIAS_STRIP.split(base) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:ALIAS_LEN]

    # Spread the budget across parts so every word contributes something.
    per = max(1, ALIAS_LEN // len(parts))
    out = "".join(p[:per] for p in parts)
    if len(out) < ALIAS_LEN:
        # Spend any slack on the first part, which is usually the distinctive one.
        out = parts[0][: ALIAS_LEN - len(out) + per] + "".join(
            p[:per] for p in parts[1:]
        )
    return out[:ALIAS_LEN]


def disambiguate(aliases: Mapping[str, str]) -> dict[str, str]:
    """Make aliases unique across the current herd.

    Collisions are resolved by numbering in pane-id order, so the same herd
    always produces the same labels. A label that shuffled between frames
    would be worse than a duplicate.
    """
    by_alias: dict[str, list[str]] = {}
    for pane_id in sorted(aliases):
        by_alias.setdefault(aliases[pane_id], []).append(pane_id)

    out: dict[str, str] = {}
    for alias, panes in by_alias.items():
        if len(panes) == 1:
            out[panes[0]] = alias
            continue
        for n, pane_id in enumerate(panes, start=1):
            suffix = str(n)
            out[pane_id] = alias[: ALIAS_LEN - len(suffix)] + suffix
    return out


def truncate_prompt(text: str) -> tuple[str, bool]:
    """Return the display text and whether anything was cut.

    The flag travels with the frame because the action layer needs it: deny
    is always allowed, approve is not, when the human was not shown the whole
    command.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= PROMPT_MAX:
        return collapsed, False
    return collapsed[: PROMPT_MAX - 1] + "…", True


def decision_id(pane_id: str, state_change_seq: int, prompt: str) -> str:
    """Mint a stable id for one blocking prompt.

    Herdr supplies no request id, so this is derived rather than received.
    Deriving it from the prompt text is the point: if the agent moves on to a
    different question, the id changes, so a decision minted against the old
    prompt is detectably stale. That is what makes send-time re-verification
    cheap — compare ids, not text.
    """
    h = hashlib.sha256(
        f"{pane_id}|{state_change_seq}|{prompt}".encode("utf-8")
    ).hexdigest()
    return h[:12]


@dataclass(slots=True)
class _Seen:
    seq: int
    since: datetime
    # False when `since` is the moment this pane first appeared rather than a
    # transition actually witnessed. Herdr reports no timestamp with
    # `state_change_seq`, so on relay startup an agent blocked twenty minutes
    # ago is indistinguishable from one blocked this second. Rather than
    # assert a duration we cannot know, the frame omits `e` entirely in that
    # case: no duration on screen is honest, "waiting 0m" is not.
    observed: bool


@dataclass(slots=True)
class FrameBuilder:
    """Builds wire frames, and remembers just enough to be honest.

    Two pieces of state, both necessary:

    * When each agent last changed state. Herdr gives a monotonic
      `state_change_seq` but no timestamp, so the first time a new seq is
      seen the wall clock is recorded. The frame then carries an absolute
      timestamp rather than an elapsed count, so the device never has to tick
      its own clock and cannot drift.

    * The last known membership. When a snapshot fails, the frame must still
      name the agents the device is displaying and mark them unknown.
      `HerdSnapshot` deliberately refuses to invent that; this is where it
      legitimately belongs, because this layer knows what is on screen.
    """

    now: Callable[[], datetime] = utcnow
    alias_overrides: Mapping[str, str] = field(default_factory=dict)
    _seen: dict[str, _Seen] = field(default_factory=dict, init=False)
    _last_agents: tuple[Agent, ...] = field(default=(), init=False)

    # -- ordering ------------------------------------------------------

    @staticmethod
    def _priority(status: AgentStatus) -> int:
        # Blocked first: someone is waiting. Then done, which means finished
        # and unseen — measured to be the common event. Then the rest.
        return {
            AgentStatus.BLOCKED: 0,
            AgentStatus.DONE: 1,
            AgentStatus.WORKING: 2,
            AgentStatus.IDLE: 3,
            AgentStatus.UNKNOWN: 4,
        }.get(status, 5)

    def _since(self, agent: Agent, now: datetime) -> _Seen:
        """When this agent entered its current state, and whether we know.

        A seq change while running is a transition we witnessed. A pane we
        have never seen before is not: its state may predate the relay by
        hours.
        """
        prev = self._seen.get(agent.pane_id)
        if prev is None:
            seen = _Seen(agent.state_change_seq, now, observed=False)
        elif prev.seq != agent.state_change_seq:
            seen = _Seen(agent.state_change_seq, now, observed=True)
        else:
            return prev
        self._seen[agent.pane_id] = seen
        return seen

    def _aliases(self, agents: Iterable[Agent]) -> dict[str, str]:
        derived = {
            a.pane_id: self.alias_overrides.get(a.pane_id) or derive_alias(a.cwd)
            for a in agents
        }
        return disambiguate(derived)

    # -- the frame -----------------------------------------------------

    def build(
        self,
        snapshot: HerdSnapshot,
        prompts: Mapping[str, str] | None = None,
    ) -> dict:
        """Build one snapshot frame.

        `prompts` maps pane_id to the blocking question text, fetched by the
        caller only on transition into blocked and cached against that
        agent's seq. Absent text is fine: the agent still renders as blocked,
        the device just cannot show the question.
        """
        now = self.now()
        prompts = prompts or {}

        if snapshot.ok:
            agents = snapshot.agents
            self._last_agents = agents
            reason = None
        else:
            # Everything the device is showing becomes unknown. Not the last
            # good frame, and not an empty herd either — either would be a
            # lie about a different thing.
            agents = tuple(
                Agent(
                    pane_id=a.pane_id,
                    workspace_id=a.workspace_id,
                    status=AgentStatus.UNKNOWN,
                    state_change_seq=a.state_change_seq,
                    cwd=a.cwd,
                    terminal_title=a.terminal_title,
                    kind=a.kind,
                    focused=False,
                )
                for a in self._last_agents
            )
            reason = (snapshot.reason or "herdr unreachable")[:80]

        aliases = self._aliases(agents)
        seen = {a.pane_id: self._since(a, now) for a in agents}
        ordered = sorted(
            agents,
            key=lambda a: (
                self._priority(a.status),
                seen[a.pane_id].since,        # longest-waiting first
                a.pane_id,                    # stable tiebreak
            ),
        )
        shown, overflow = ordered[:MAX_AGENTS], max(0, len(ordered) - MAX_AGENTS)

        rows = []
        for a in shown:
            row: dict = {
                "i": a.pane_id,
                "n": aliases[a.pane_id],
                "s": a.status.value,
            }
            # Omitted when we cannot honestly claim to know it — see _Seen.
            if seen[a.pane_id].observed:
                row["e"] = iso(seen[a.pane_id].since)
            if a.status is AgentStatus.BLOCKED:
                text = prompts.get(a.pane_id)
                if text:
                    shown_text, cut = truncate_prompt(text)
                    row["q"] = shown_text
                    row["r"] = decision_id(a.pane_id, a.state_change_seq, text)
                    # Only present when true, so the common case costs nothing.
                    if cut:
                        row["x"] = True
            rows.append(row)

        frame: dict = {
            "t": "snap",
            "v": PROTOCOL_VERSION,
            "ts": iso(now),
            "a": rows,
        }
        # Reinstated after the review dropped it. The argument for dropping
        # was that a five-slot cap against exactly five agents made this
        # always zero; a sixth workspace appeared the same afternoon. Silently
        # dropping an agent from a device whose whole job is "does anything
        # need me" is the wrong failure.
        if overflow:
            frame["more"] = overflow
        if reason:
            frame["why"] = reason
        return frame

    def encode(self, frame: Mapping) -> bytes:
        return (json.dumps(frame, separators=(",", ":"), ensure_ascii=False)
                + "\n").encode("utf-8")
