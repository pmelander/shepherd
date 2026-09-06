"""Acting on what the device asked for, and refusing when that is unsafe.

The device shows a snapshot. Between that snapshot and a thumb press, the
agent can finish one question and ask a different one. Nothing about the
frame protocol prevents an approval landing on a prompt the human never saw —
`state_change_seq` proves the snapshot was recent, not that the question is
still the same question.

So every approve re-reads the pane and compares the question against the one
the device was actually shown, and aborts on any difference. That single
check is the difference between approving `git status` and approving whatever
replaced it.

Layered with it:

* A closed action set. `k` indexes a literal dict; anything else is dropped.
* A pane allowlist rebuilt from every snapshot, so an action can only target
  a pane that was actually rendered.
* Decision ids are one-shot. A replayed frame does the nothing it should.
* Approve is refused when the prompt did not fit on screen untruncated. Deny
  is always allowed. The device can always say no; it may only say yes to
  something it showed you in full.
* Every attempt is appended to an audit log, including the refusals.

* Every frame is signed. The bond proves the peer is a device Windows once
  paired with; it does not prove it is this Cardputer, because Just Works
  gives no MITM protection. A MAC over `ts|pane|action|decision`, keyed by a
  secret the relay generates and the firmware is built with, does. See
  `auth.py`.

Signature checking comes first, before the allowlist and before anything is
read. A peer that cannot sign should not be able to make the relay do work,
including the work of reading a pane.
"""

from __future__ import annotations

import enum
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from typing import Callable, Deque, Mapping

from .auth import verify as verify_mac
from .herdr import HerdrError, HerdrSource
from .frame import truncate_option
from .models import is_pane_id
from .prompt import (
    PromptError,
    parse_prompt,
    plan_approve,
    plan_choice,
    plan_deny,
)
from .recap import extract_answer


class Action(enum.Enum):
    APPROVE = "approve"
    DENY = "deny"
    FOCUS = "focus"
    # Read-only: fetch what the agent last said, for the device to display.
    # It goes through this gate rather than around it because it still reads
    # a pane, and the rule that an unauthenticated peer cannot make the relay
    # do work applies to reads as much as to writes.
    DETAIL = "detail"


# The closed set, as a literal map. Membership here is the only way to reach
# Herdr; there is no path that builds an action from device-supplied text.
ACTIONS: Mapping[str, Action] = {
    "approve": Action.APPROVE,
    "deny": Action.DENY,
    "focus": Action.FOCUS,
    "detail": Action.DETAIL,
}


class Refusal(str, enum.Enum):
    UNKNOWN_ACTION = "unknown action"
    UNKNOWN_PANE = "pane not in the last snapshot"
    UNKNOWN_DECISION = "decision id not recognised"
    REPLAYED = "decision id already used"
    TRUNCATED = "prompt was truncated on screen; approve refused"
    PROMPT_GONE = "no prompt on the pane any more"
    PROMPT_CHANGED = "the prompt changed since it was shown"
    UNSAFE_OPTIONS = "no safe option to select"
    HERDR_FAILED = "herdr rejected the keys"
    BAD_MAC = "signature did not verify"
    STALE_TS = "signed against a frame we no longer recognise"
    UNSIGNED = "no signature, and this relay requires one"
    UNKNOWN_CHOICE = "chose an option the device was never shown"


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """One `act` frame off the wire, already shape-checked."""

    pane_id: str
    action: Action
    decision_id: str | None = None
    # The timestamp of the frame the device was answering, echoed back, and
    # the MAC over it. Both absent on an unsigned frame, which a relay holding
    # a secret refuses.
    ts: str | None = None
    mac: str | None = None
    # Which option the human cycled to, when the prompt had more than the
    # one the relay would pick on its own. None means "use the safe path",
    # which is what a device that does not know about options still sends.
    choice: int | None = None


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    request: ActionRequest | None
    reason: str = ""
    keys: tuple[str, ...] = ()
    # True when the device should be told to refresh, because what it is
    # showing no longer matches reality.
    restale: bool = False
    # Set only by DETAIL: the text to send back. Empty string is a real
    # answer meaning "this agent has not said anything readable", which the
    # device renders as such; None means this was not a detail request.
    body: str | None = None


@dataclass(frozen=True, slots=True)
class PendingDecision:
    """What the device was actually shown for one blocking prompt."""

    pane_id: str
    fingerprint: str   # the question + options as rendered when the frame was built
    truncated: bool
    # The option labels the device was shown, in order. Empty when the frame
    # carried none, in which case only the safe path is available.
    options: tuple[str, ...] = ()


def parse_action(raw: object) -> ActionRequest | None:
    """Turn a decoded `act` frame into a request, or None.

    Returns None rather than raising for anything malformed. A peer sending
    junk should get silence, not a diagnostic it can iterate against.
    """
    if not isinstance(raw, dict) or raw.get("t") != "act":
        return None
    action = ACTIONS.get(raw.get("k")) if isinstance(raw.get("k"), str) else None
    if action is None:
        return None
    pane_id = raw.get("i")
    if not is_pane_id(pane_id):
        return None
    decision_id = raw.get("r")
    if decision_id is not None and (
        not isinstance(decision_id, str) or not decision_id.isalnum()
    ):
        return None
    ts = raw.get("ts")
    mac = raw.get("mac")
    if ts is not None and not isinstance(ts, str):
        return None
    if mac is not None and not isinstance(mac, str):
        return None
    choice = raw.get("c")
    # bool is an int in Python, and `true` would sail through an isinstance
    # check and index options[1]. Excluded explicitly.
    if choice is not None and (isinstance(choice, bool)
                               or not isinstance(choice, int)
                               or not 0 <= choice < 64):
        return None
    return ActionRequest(pane_id=pane_id, action=action, decision_id=decision_id,
                         ts=ts, mac=mac, choice=choice)


def fingerprint(prompt_text: str | None) -> str:
    """A comparable summary of 'which question is this'.

    Built from the parsed question and option labels rather than the raw
    buffer, so cosmetic churn — a spinner, a token counter, a redrawn border —
    does not read as a changed prompt, while a genuinely different question
    always does.
    """
    p = parse_prompt(prompt_text)
    if p is None:
        return ""
    parts = [p.question.strip()] + [o.normalized for o in p.options]
    return "␟".join(parts)


def audit_path() -> Path:
    """Where the action log lives.

    HERDR_PLUGIN_STATE_DIR is injected into plugin-spawned processes and sits
    outside any repo, which is where a log of full prompt text belongs.
    """
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if base:
        return Path(base) / "actions.jsonl"
    return Path.home() / ".shepherd" / "actions.jsonl"


@dataclass(slots=True)
class ActionGate:
    """Validates, re-verifies, dispatches, and records."""

    source: HerdrSource
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    audit: Path | None = None
    # None disables signature checking. Only for tests and for bringing a new
    # device up before its firmware carries the secret; the relay logs loudly.
    secret: bytes | None = None
    _allowed: frozenset[str] = field(default_factory=frozenset, init=False)
    _pending: dict[str, PendingDecision] = field(default_factory=dict, init=False)
    _burned: set[str] = field(default_factory=set, init=False)
    # A few frames' worth of timestamps. More than one because a thumb is
    # slower than a keepalive: answering a question you have been reading for
    # fifteen seconds must still work.
    _recent_ts: Deque[str] = field(default_factory=lambda: deque(maxlen=6), init=False)

    # -- state kept in step with what the device is showing ----------------

    def observe_frame(
        self, pane_ids: frozenset[str], pending: Mapping[str, PendingDecision],
        ts: str | None = None,
    ) -> None:
        """Record what the device now has on screen.

        Called by the runner every time a frame is sent. The allowlist and the
        set of answerable decisions are derived from what was rendered, never
        from what the device claims.
        """
        self._allowed = frozenset(p for p in pane_ids if is_pane_id(p))
        self._pending = dict(pending)
        if ts and ts not in self._recent_ts:
            self._recent_ts.append(ts)

    # -- dispatch ----------------------------------------------------------

    def _authentic(self, req: ActionRequest) -> Refusal | None:
        if self.secret is None:
            return None
        if not req.mac or not req.ts:
            return Refusal.UNSIGNED
        if req.ts not in self._recent_ts:
            # Binding to a frame we actually sent is what stops a captured
            # frame being replayed later. It is the only replay protection
            # `focus` has, since it carries no decision id.
            return Refusal.STALE_TS
        if not verify_mac(self.secret, req.mac, req.ts, req.pane_id,
                          req.action.value, req.decision_id, req.choice):
            return Refusal.BAD_MAC
        return None

    async def dispatch(self, req: ActionRequest) -> ActionResult:
        # Before the allowlist and before any pane is read: an unauthenticated
        # peer should not be able to make the relay do work on its behalf.
        bad = self._authentic(req)
        if bad is not None:
            return self._refuse(req, bad)

        if req.pane_id not in self._allowed:
            return self._refuse(req, Refusal.UNKNOWN_PANE)

        if req.action is Action.DETAIL:
            # Reads nothing but the pane the device is already showing, and
            # sends no keys. Not audited as an action for that reason: the
            # log is a record of things that changed the world, and burying
            # real approvals under a scroll of reads would make it useless.
            try:
                text = await self.source.read_pane(req.pane_id, lines=80)
            except HerdrError as e:
                return self._refuse(req, Refusal.HERDR_FAILED, detail=str(e))
            return ActionResult(ok=True, request=req, body=extract_answer(text))

        if req.action is Action.FOCUS:
            return await self._run(req, "focus", ())

        if req.action is Action.DENY:
            # Always permitted, and needs no re-verification: cancelling a
            # prompt the human did not mean to cancel is a nuisance, not a
            # hazard, and refusing to deny would strand a blocked agent.
            if req.decision_id:
                self._burned.add(req.decision_id)
            return await self._run(req, "send_keys", tuple(plan_deny()))

        # --- approve, the only path that can make something happen --------
        if not req.decision_id:
            return self._refuse(req, Refusal.UNKNOWN_DECISION)
        if req.decision_id in self._burned:
            return self._refuse(req, Refusal.REPLAYED)

        shown = self._pending.get(req.decision_id)
        if shown is None or shown.pane_id != req.pane_id:
            return self._refuse(req, Refusal.UNKNOWN_DECISION)
        if shown.truncated:
            return self._refuse(req, Refusal.TRUNCATED)

        fresh = await self.source.read_pane(req.pane_id)
        prompt = parse_prompt(fresh)
        if prompt is None:
            return self._refuse(req, Refusal.PROMPT_GONE, restale=True)
        if fingerprint(fresh) != shown.fingerprint:
            # The agent moved on. This is the whole reason this layer exists.
            return self._refuse(req, Refusal.PROMPT_CHANGED, restale=True)

        try:
            if req.choice is None:
                keys = plan_approve(prompt)
            else:
                # The human picked one. The fingerprint above already proved
                # the option block is unchanged since it was displayed, so the
                # index is meaningful; passing the label too means an
                # off-by-one in either implementation is caught rather than
                # silently granting the neighbouring option — which on this
                # prompt is often a permanent permission.
                if req.choice >= len(shown.options):
                    return self._refuse(req, Refusal.UNKNOWN_CHOICE)
                # truncate_option is how the frame shortened the label for
                # the wire; the comparison inside applies it to the fresh
                # label so both sides are the same shape.
                keys = plan_choice(prompt, req.choice,
                                   shown.options[req.choice],
                                   cut=truncate_option)
        except PromptError as e:
            return self._refuse(req, Refusal.UNSAFE_OPTIONS, detail=str(e))

        self._burned.add(req.decision_id)
        return await self._run(req, "send_keys", tuple(keys))

    # -- plumbing ----------------------------------------------------------

    async def _run(self, req: ActionRequest, how: str, keys: tuple[str, ...]) -> ActionResult:
        try:
            if how == "focus":
                await self.source.focus(req.pane_id)
            else:
                await self.source.send_keys(req.pane_id, list(keys))
        except HerdrError as e:
            return self._refuse(req, Refusal.HERDR_FAILED, detail=str(e))
        res = ActionResult(ok=True, request=req, keys=keys)
        self._record(req, res, "")
        return res

    def _refuse(
        self,
        req: ActionRequest,
        why: Refusal,
        detail: str = "",
        restale: bool = False,
    ) -> ActionResult:
        reason = f"{why.value}{': ' + detail if detail else ''}"
        res = ActionResult(ok=False, request=req, reason=reason, restale=restale)
        self._record(req, res, reason)
        return res

    def _record(self, req: ActionRequest, res: ActionResult, reason: str) -> None:
        """Append-only. Refusals are logged too — a device that tried to do
        something and was stopped is exactly what you want a record of."""
        shown = self._pending.get(req.decision_id or "")
        entry = {
            "ts": self.now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pane_id": req.pane_id,
            "action": req.action.value,
            "decision_id": req.decision_id,
            "keys": list(res.keys),
            "ok": res.ok,
            "reason": reason,
            # The full question as shown, not the truncated display text, so
            # the log answers "what was approved" without ambiguity.
            "shown": shown.fingerprint if shown else None,
        }
        path = self.audit or audit_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            # Deliberately broad. A failed write must never prevent a deny
            # from reaching a blocked agent, and the failure modes are wider
            # than OSError — a path with a null byte raises ValueError from
            # mkdir, which an OSError-only guard let escape and take the
            # action down with it. The action is the point; the record is
            # important but secondary.
            pass
