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
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping

from .actions import ActionGate, PendingDecision, fingerprint, parse_action
from .events import EventStreamError, PipeEventSource
from .frame import FrameBuilder
from .herdr import CliHerdrSource, HerdrSource
from .models import AgentStatus, HerdSnapshot
from .transport import BleTransport, Transport, TransportError, backoff_delays

log = logging.getLogger("shepherd")

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


@dataclass
class Runner:
    source: HerdrSource = field(default_factory=CliHerdrSource)
    transport_factory: Callable[[], Transport] = BleTransport
    builder: FrameBuilder = field(default_factory=FrameBuilder)
    poll_interval: float = POLL_INTERVAL
    keepalive: float = KEEPALIVE
    tick: float = TICK
    use_events: bool = True
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = lambda: asyncio.get_event_loop().time()

    _snapshot: HerdSnapshot = field(default_factory=HerdSnapshot, init=False)
    _seqs: dict[str, int] = field(default_factory=dict, init=False)
    _prompts: dict[str, tuple[int, str]] = field(default_factory=dict, init=False)
    _dirty: bool = field(default=True, init=False)
    _stop: bool = field(default=False, init=False)

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
        prompts = self._prompt_texts()
        frame = self.builder.build(self._snapshot, prompts)
        pending: dict[str, PendingDecision] = {}
        for row in frame.get("a", []):
            decision = row.get("r")
            if not decision:
                continue
            full = prompts.get(row["i"])
            if full is None:
                continue
            pending[decision] = PendingDecision(
                pane_id=row["i"],
                fingerprint=fingerprint(full),
                truncated=bool(row.get("x")),
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
        gate = ActionGate(source=self.source)
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

    async def _push_loop(self, transport: Transport, gate: ActionGate) -> None:
        last_poll = -1e9
        last_send = -1e9
        while not self._stop:
            now = self.clock()
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
                    frozenset(r["i"] for r in frame.get("a", [])), pending
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
            req = parse_action(raw)
            if req is None:
                # Junk from the peer gets silence, not a diagnostic it can
                # iterate against.
                continue
            result = await gate.dispatch(req)
            log.info("action %s %s -> %s%s", req.action.value, req.pane_id,
                     "ok" if result.ok else "refused",
                     "" if result.ok else f" ({result.reason})")
            if result.restale or result.ok:
                # Either the world moved or we just changed it; the device's
                # picture is stale either way.
                self._dirty = True

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
