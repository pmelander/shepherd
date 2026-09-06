"""The relay: the thing that actually runs.

Everything else in Shepherd is a component with a seam. This composes them,
and the composition is where the design decisions become behaviour:

* **Membership by poll, transitions by push.** Herdr has no push path for
  "an agent now exists" — `pane.agent_detected` does not stream — so
  `agent list` runs on a slow timer forever. Status transitions come over the
  named pipe, filtered to the states worth waking for.
* **Frames go out on change, or every 10s.** Pushing on a timer keeps the
  radio hot and costs the battery; pushing only on change makes a dead relay
  look like a calm afternoon. Both, therefore.
* **The blocking question is fetched once**, on the transition into blocked,
  and cached against that agent's `state_change_seq`. Re-reading a pane every
  tick would spawn a process per second per blocked agent.
* **The action gate is told what is on screen** after every frame, so its
  allowlist describes what was actually rendered rather than what the device
  claims.
* **Losing Herdr and losing the device are different failures.** The first
  produces an all-unknown frame; the second stops frames entirely and the
  device's own staleness rule takes over. Neither is allowed to look like a
  quiet herd.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from .actions import ActionGate, PendingDecision, fingerprint, parse_action
from .auth import (
    load_or_create_secret,
    new_secret,
    rekey_frame,
    save_secret,
    verify_ack,
)
from .events import EventStreamError, PipeEventSource
from .frame import MAX_OPTIONS, FrameBuilder, iso, truncate_option, utcnow
from .herdr import CliHerdrSource, HerdrSource
from .models import AgentStatus, HerdSnapshot
from .prompt import parse_prompt
from .transport import BleTransport, Transport, TransportError, backoff_delays

log = logging.getLogger("shepherd")


def herdr_liveness() -> str | None:
    """Herdr's own liveness stamp, or None when it is gone.

    HERDR_SOCKET_PATH names a file whose contents are `pid:start_time_nanos`.
    Herdr deletes it when a session stops and writes a new one on the next
    start, so this single string answers both "is Herdr still there" and "is
    it the same Herdr".
    """
    raw = os.environ.get("HERDR_SOCKET_PATH")
    if not raw:
        return None
    try:
        return Path(raw).read_text(encoding="utf-8").strip()
    except OSError:
        return None


async def watch_herdr(runner: "Runner", interval: float = 5.0,
                      sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Stop the relay when its Herdr session ends or is replaced.

    Herdr does NOT kill plugin startup processes when a session stops —
    verified by stopping a test session and finding the relay still running
    afterwards. Without this, every Herdr restart would leave another orphan
    competing for the same Cardputer, and the newest one would lose.
    """
    initial = herdr_liveness()
    if initial is None:
        log.debug("no HERDR_SOCKET_PATH; running unsupervised")
        return
    while not runner._stop:
        await sleep(interval)
        current = herdr_liveness()
        if current != initial:
            log.info("herdr session ended or restarted (%s -> %s); exiting",
                     initial, current)
            runner.stop()
            return


# Membership discovery. Slow on purpose: this is the only thing that spawns a
# process, and at 1Hz it would be ~86,000 subprocess launches per working day
# on a laptop already hosting five agents.
POLL_INTERVAL = 30.0

# Frames go out at least this often even when nothing changed, so the device
# can tell "nothing is happening" from "nobody is talking". Must stay
# comfortably below the device's 30s staleness threshold.
KEEPALIVE = 10.0

# How often the loop wakes to check whether anything moved. Cheap: it is a
# comparison against cached state, not a Herdr call.
TICK = 1.0

# Rotation is asked for by dropping this file in the state directory, which
# `start.py --rotate-key` does. A file rather than a socket or a signal
# because the relay that must perform the rotation is the one holding the BLE
# link, so the request has to reach a process that is already running, and a
# file is the one IPC that needs no new listener and survives the relay not
# being up yet.
ROTATE_MARKER = "rotate.request"

# How long to wait for the device to prove it stored the new key.
ROTATE_TIMEOUT = 10.0


def rotate_marker() -> Path:
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return (Path(base) if base else Path.home() / ".shepherd") / ROTATE_MARKER


@dataclass
class Runner:
    source: HerdrSource = field(default_factory=CliHerdrSource)
    transport_factory: Callable[[], Transport] = BleTransport
    builder: FrameBuilder = field(default_factory=FrameBuilder)
    poll_interval: float = POLL_INTERVAL
    keepalive: float = KEEPALIVE
    tick: float = TICK
    use_events: bool = True
    # None means unsigned, which the gate allows but logs loudly. The default
    # is to load (and on first run generate) the shared secret.
    secret: bytes | None = None
    require_signatures: bool = True
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = lambda: asyncio.get_event_loop().time()

    _snapshot: HerdSnapshot = field(default_factory=HerdSnapshot, init=False)
    _seqs: dict[str, int] = field(default_factory=dict, init=False)
    _prompts: dict[str, tuple[int, str]] = field(default_factory=dict, init=False)
    _dirty: bool = field(default=True, init=False)
    _stop: bool = field(default=False, init=False)
    # Set while a rotation is in flight; the action loop hands the ack here
    # rather than trying to interpret it, because only the push loop knows
    # which new key it is waiting to hear about.
    _rekey_pending: tuple[str, bytes] | None = field(default=None, init=False)
    _rekey_ok: bool = field(default=False, init=False)

    # -- herd state ---------------------------------------------------------

    async def refresh(self) -> None:
        """Ask Herdr who is out there and what they are doing."""
        snap = await self.source.list_agents()
        if not snap.ok:
            # Keep the previous membership so the frame builder can name the
            # agents it is marking unknown, but flag the frame as dirty so the
            # device hears about it immediately rather than at the keepalive.
            if self._snapshot.ok:
                self._dirty = True
            self._snapshot = snap
            return

        for a in snap.agents:
            if self._seqs.get(a.pane_id) != a.state_change_seq:
                self._seqs[a.pane_id] = a.state_change_seq
                self._dirty = True

        # An agent vanishing is a change too, and one the device must see:
        # otherwise it keeps offering to answer a pane that no longer exists.
        gone = set(self._seqs) - snap.pane_ids
        for pane_id in gone:
            self._seqs.pop(pane_id, None)
            self._prompts.pop(pane_id, None)
            self._dirty = True

        if not self._snapshot.ok:
            self._dirty = True   # recovering from a failure is worth a frame
        self._snapshot = snap
        await self._fetch_prompts()

    async def _fetch_prompts(self) -> None:
        """Read the question for each blocked agent, once per state change."""
        for a in self._snapshot.agents:
            if a.status is not AgentStatus.BLOCKED:
                self._prompts.pop(a.pane_id, None)
                continue
            cached = self._prompts.get(a.pane_id)
            if cached and cached[0] == a.state_change_seq:
                continue
            text = await self.source.read_pane(a.pane_id)
            if text:
                self._prompts[a.pane_id] = (a.state_change_seq, text)
                self._dirty = True

    def _prompt_texts(self) -> dict[str, str]:
        return {pane: text for pane, (_, text) in self._prompts.items()}

    # -- frames -------------------------------------------------------------

    def build(self) -> tuple[dict, dict[str, PendingDecision]]:
        raw = self._prompt_texts()

        # The frame carries the PARSED question, never the raw pane buffer.
        # Sending the buffer put the Claude Code splash banner in `q`, flagged
        # every prompt as truncated, and so made every blocked agent
        # unanswerable — the device drew the herd list and the approve key did
        # nothing, with no explanation on screen.
        #
        # It would also have churned the decision id on every poll, because
        # the buffer contains a live token counter: the id is hashed from this
        # text, so a stable question has to mean stable bytes.
        display: dict[str, str] = {}
        options: dict[str, tuple[tuple[str, str], ...]] = {}
        for pane_id, text in raw.items():
            parsed = parse_prompt(text)
            if parsed and parsed.question:
                display[pane_id] = parsed.question
                # Labels and their risk classification, in screen order. The
                # device cycles these and sends back an index; the gate
                # re-derives the same list at send time and refuses if it has
                # changed, so the index can only ever mean what was displayed.
                options[pane_id] = tuple(
                    (truncate_option(o.label), o.kind)
                    for o in parsed.options[:MAX_OPTIONS]
                )

        frame = self.builder.build(self._snapshot, display, options)
        pending: dict[str, PendingDecision] = {}
        for row in frame.get("a", []):
            decision = row.get("r")
            if not decision:
                continue
            # Fingerprints stay keyed on the raw buffer: the gate re-reads the
            # pane at send time and fingerprints that, so both sides must be
            # comparing the same kind of thing.
            full = raw.get(row["i"])
            if full is None:
                continue
            pending[decision] = PendingDecision(
                pane_id=row["i"],
                fingerprint=fingerprint(full),
                truncated=bool(row.get("x")),
                options=tuple(row.get("o") or ()),
            )
        return frame, pending

    # -- the loop -----------------------------------------------------------

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        """Connect, serve, reconnect. Runs until stop() or cancellation."""
        delays = backoff_delays()
        while not self._stop:
            transport = self.transport_factory()
            try:
                await transport.connect()
            except TransportError as e:
                delay = next(delays)
                log.warning("connect failed (%s); retrying in %.1fs", e, delay)
                await self.sleep(delay)
                continue

            log.info("device connected")
            delays = backoff_delays()   # a good connection resets the backoff
            try:
                await self._serve(transport)
            except TransportError as e:
                log.warning("link lost: %s", e)
            finally:
                with contextlib.suppress(Exception):
                    await transport.close()
            if not self._stop:
                await self.sleep(next(delays))

    async def _serve(self, transport: Transport) -> None:
        gate = ActionGate(source=self.source, secret=self._secret())
        tasks = [
            asyncio.create_task(self._push_loop(transport, gate)),
            asyncio.create_task(self._action_loop(transport, gate)),
        ]
        if self.use_events:
            tasks.append(asyncio.create_task(self._event_loop()))
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _secret(self) -> bytes | None:
        if not self.require_signatures:
            log.warning("signature checking DISABLED; any bonded peer can act")
            return None
        if self.secret is None:
            self.secret = load_or_create_secret()
        return self.secret

    async def _rotate(self, transport: Transport, gate: ActionGate) -> None:
        """Replace the shared secret, if the device will prove it took it.

        Ordering is the whole design. The relay writes nothing to disk until
        the device has returned a MAC computed with the NEW key, so every way
        this can be interrupted — link drop, power cut, the device refusing —
        leaves both sides still holding the OLD key and still working. The
        one outcome worth engineering against is the two halves disagreeing,
        because that bricks the link with no error message that says so.
        """
        marker = rotate_marker()
        fresh = new_secret()
        ts = iso(utcnow())
        current = self._secret()
        if current is None:
            log.warning("rotation asked for but signatures are disabled")
            marker.unlink(missing_ok=True)
            return

        self._rekey_pending = (ts, fresh)
        self._rekey_ok = False
        log.info("rotating the shared secret")
        await transport.send(self.builder.encode(rekey_frame(current, ts, fresh)))

        deadline = self.clock() + ROTATE_TIMEOUT
        while self.clock() < deadline and not self._rekey_ok and not self._stop:
            await self.sleep(self.tick)

        pending, self._rekey_pending = self._rekey_pending, None
        if not self._rekey_ok:
            log.error("rotation failed: no valid acknowledgement in %.0fs; "
                      "both sides still hold the old key", ROTATE_TIMEOUT)
            marker.unlink(missing_ok=True)
            return

        path = save_secret(fresh)
        self.secret = fresh
        gate.secret = fresh
        marker.unlink(missing_ok=True)
        log.info("rotation complete; new secret written to %s", path)
        log.info("update firmware/secret.ini before the next reflash, or the "
                 "device will fall back to its build-time key")

    async def _push_loop(self, transport: Transport, gate: ActionGate) -> None:
        last_poll = -1e9
        last_send = -1e9
        while not self._stop:
            now = self.clock()

            # Cheap: one stat per tick, and rotation is a thing that happens
            # by hand a few times in a device's life.
            if rotate_marker().exists():
                await self._rotate(transport, gate)
            if now - last_poll >= self.poll_interval:
                last_poll = now
                await self.refresh()

            if self._dirty or (now - last_send) >= self.keepalive:
                frame, pending = self.build()
                payload = self.builder.encode(frame)
                await transport.send(payload)
                # Only after the device has it does the gate consider those
                # panes answerable.
                gate.observe_frame(
                    frozenset(r["i"] for r in frame.get("a", [])), pending,
                    ts=frame.get("ts"),
                )
                self._dirty = False
                last_send = now

            await self.sleep(self.tick)

    async def _action_loop(self, transport: Transport, gate: ActionGate) -> None:
        import json

        async for line in transport.lines():
            if self._stop:
                return
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if isinstance(raw, dict) and raw.get("t") == "kack":
                self._on_rekey_ack(raw)
                continue
            req = parse_action(raw)
            if req is None:
                # Junk from the peer gets silence, not a diagnostic it can
                # iterate against.
                continue
            result = await gate.dispatch(req)
            if result.ok and result.body is not None:
                # A read, not a change. Answer it and move on without
                # touching the dirty flag: nothing about the herd moved.
                await transport.send(self.builder.encode(
                    self.builder.detail_frame(req.pane_id, result.body)))
                log.debug("detail %s -> %d chars", req.pane_id, len(result.body))
                continue
            log.info("action %s %s -> %s%s", req.action.value, req.pane_id,
                     "ok" if result.ok else "refused",
                     "" if result.ok else f" ({result.reason})")
            if result.restale or result.ok:
                # Either the world moved or we just changed it; the device's
                # picture is stale either way.
                self._dirty = True

    def _on_rekey_ack(self, raw: dict) -> None:
        """Accept the device's proof that it stored the new key.

        Checked against the key we sent and the timestamp we sent it with, so
        a replayed ack from an earlier rotation proves nothing about this one.
        """
        pending = self._rekey_pending
        if pending is None:
            log.debug("unexpected rekey ack; ignoring")
            return
        ts, fresh = pending
        if raw.get("ts") != ts or not verify_ack(fresh, ts, raw.get("f")):
            log.error("rekey ack did not verify; keeping the old key")
            return
        self._rekey_ok = True

    async def _event_loop(self) -> None:
        """Push subscriptions, best effort.

        Failing here is not fatal: the poll loop still finds every transition,
        just later. Losing the relay entirely because a subscription could not
        be established would be a worse trade.
        """
        while not self._stop:
            panes = sorted(self._snapshot.pane_ids)
            if not panes:
                await self.sleep(self.tick)
                continue
            try:
                src = PipeEventSource()
                async for ev in src.watch(
                    panes, statuses=(AgentStatus.BLOCKED, AgentStatus.DONE)
                ):
                    log.debug("event %s -> %s", ev.pane_id, ev.status.value)
                    await self.refresh()
                    if set(sorted(self._snapshot.pane_ids)) != set(panes):
                        break   # membership changed; re-subscribe
            except EventStreamError as e:
                log.debug("event stream unavailable (%s); polling only", e)
                await self.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("event stream error (%s); polling only", e)
                await self.sleep(self.poll_interval)
