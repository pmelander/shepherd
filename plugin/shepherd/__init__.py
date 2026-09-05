"""Shepherd — per-agent Herdr state on a Cardputer ADV over BLE.

Layout mirrors the seams the design review insisted on, so every layer is
testable without Herdr running and without the device attached:

    models.py   what an agent is; Herdr's status vocabulary, verbatim
    herdr.py    the HerdrSource seam + the CLI-backed implementation
    events.py   per-pane push subscriptions over Herdr's named pipe
    frame.py    snapshot + events -> the bytes that go over BLE
    transport.py  the BLE link; chunking and reassembly
    prompt.py   reading a permission prompt, choosing which keys to send
    actions.py  the send-time safety layer: re-verify, or refuse
    runner.py   the relay: composes all of the above and runs
    auth.py     the shared secret and the action-frame signature
"""

from .models import Agent, AgentStatus, HerdSnapshot, is_pane_id
from .events import (
    EventSource,
    EventStreamError,
    PipeEventSource,
    StatusEvent,
    build_subscribe_request,
    parse_line,
    socket_path,
)
from .actions import (
    Action,
    ActionGate,
    ActionRequest,
    ActionResult,
    PendingDecision,
    fingerprint,
    parse_action,
)
from .auth import build_flag, load_or_create_secret, sign, verify
from .frame import PROTOCOL_VERSION, FrameBuilder
from .prompt import PromptError, parse_prompt, plan_approve, plan_deny
from .runner import Runner
from .transport import BleTransport, Transport, TransportError
from .herdr import (
    CliHerdrSource,
    CommandResult,
    HerdrError,
    HerdrSource,
    herdr_binary,
)

__all__ = [
    "Agent",
    "AgentStatus",
    "HerdSnapshot",
    "is_pane_id",
    "CliHerdrSource",
    "CommandResult",
    "HerdrError",
    "HerdrSource",
    "herdr_binary",
    "EventSource",
    "EventStreamError",
    "PipeEventSource",
    "StatusEvent",
    "build_subscribe_request",
    "parse_line",
    "socket_path",
    "FrameBuilder",
    "PROTOCOL_VERSION",
    "Action",
    "ActionGate",
    "ActionRequest",
    "ActionResult",
    "PendingDecision",
    "fingerprint",
    "parse_action",
    "PromptError",
    "parse_prompt",
    "plan_approve",
    "plan_deny",
    "BleTransport",
    "Transport",
    "TransportError",
    "Runner",
    "load_or_create_secret",
    "sign",
    "verify",
    "build_flag",
]
