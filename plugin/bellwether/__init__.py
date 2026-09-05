"""Bellwether — per-agent Herdr state on a Cardputer ADV over BLE.

Layout mirrors the seams the design review insisted on, so every layer is
testable without Herdr running and without the device attached:

    models.py   what an agent is; Herdr's status vocabulary, verbatim
    herdr.py    the HerdrSource seam + the CLI-backed implementation
    events.py   per-pane push subscriptions over Herdr's named pipe
"""

from .models import Agent, AgentStatus, HerdSnapshot, is_pane_id
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
]
