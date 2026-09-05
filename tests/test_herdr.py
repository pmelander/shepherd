"""T7 coverage: the HerdrSource seam and its CLI implementation.

Every test here runs with no Herdr installed and no device attached. That is
the whole point of the seam — the failure paths, which are the ones that
decide whether the device ever shows a stale lie, are the easiest things to
exercise.

Sync tests calling asyncio.run keep pytest-asyncio out of the dependency list.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from bellwether.herdr import (  # noqa: E402
    CliHerdrSource,
    CommandResult,
    HerdrError,
    herdr_binary,
)
from bellwether.models import Agent, AgentStatus, is_pane_id  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def agent_list_payload(*agents: dict) -> str:
    return json.dumps({"id": "cli:agent:list", "result": {"agents": list(agents)}})


def raw_agent(pane_id="w2:p1", status="idle", seq=230, **kw) -> dict:
    base = {
        "agent": "claude",
        "agent_status": status,
        "cwd": r"C:\.workspaces\new-pricemanager-client",
        "focused": False,
        "pane_id": pane_id,
        "state_change_seq": seq,
        "tab_id": "w2:t1",
        "terminal_title_stripped": "PriceComponentManager migration",
        "workspace_id": pane_id.split(":")[0],
    }
    base.update(kw)
    return base


class FakeRunner:
    """Records argv and returns canned results. No process is ever spawned."""

    def __init__(self, *results, raises: Exception | None = None):
        self.results = list(results)
        self.raises = raises
        self.calls: list[list[str]] = []

    async def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        return self.results.pop(0) if self.results else CommandResult(0, "", "")


def ok(stdout: str) -> CommandResult:
    return CommandResult(0, stdout, "")


# ---------------------------------------------------------------- models


def test_pane_id_shapes():
    assert is_pane_id("w2:p1")
    assert is_pane_id("wA:p1")
    assert is_pane_id("wB:p12")
    assert not is_pane_id("")
    assert not is_pane_id("w2")
    assert not is_pane_id("p1")
    assert not is_pane_id("w2:p1; rm -rf /")
    assert not is_pane_id("--upload-port")
    assert not is_pane_id(None)
    assert not is_pane_id(3)


def test_status_parse_is_total():
    assert AgentStatus.parse("blocked") is AgentStatus.BLOCKED
    assert AgentStatus.parse("done") is AgentStatus.DONE
    # A status Herdr adds later must render as unknown, never crash and never
    # silently look like the previous value.
    assert AgentStatus.parse("reticulating") is AgentStatus.UNKNOWN
    assert AgentStatus.parse(None) is AgentStatus.UNKNOWN
    assert AgentStatus.parse(42) is AgentStatus.UNKNOWN


def test_done_and_blocked_need_attention_idle_does_not():
    # done means "finished, and you have not looked yet" - measured to be the
    # common event, so it must not be folded into idle.
    assert AgentStatus.DONE.needs_attention
    assert AgentStatus.BLOCKED.needs_attention
    assert not AgentStatus.IDLE.needs_attention
    assert not AgentStatus.WORKING.needs_attention
    assert not AgentStatus.UNKNOWN.needs_attention


def test_agent_from_cli_maps_verified_fields():
    a = Agent.from_cli(raw_agent(status="working", seq=363))
    assert a.pane_id == "w2:p1"
    assert a.workspace_id == "w2"
    assert a.status is AgentStatus.WORKING
    assert a.state_change_seq == 363
    assert a.kind == "claude"
    assert a.terminal_title == "PriceComponentManager migration"


def test_agent_from_cli_survives_missing_fields():
    a = Agent.from_cli({"pane_id": "w9:p1"})
    assert a.status is AgentStatus.UNKNOWN
    assert a.state_change_seq == 0
    assert a.cwd == ""


# ------------------------------------------------------------ list_agents


def test_list_agents_happy():
    runner = FakeRunner(ok(agent_list_payload(
        raw_agent("w2:p1", "idle"),
        raw_agent("w9:p1", "blocked", seq=441),
    )))
    snap = run(CliHerdrSource(runner=runner, binary="herdr").list_agents())
    assert snap.ok
    assert len(snap.agents) == 2
    assert snap.pane_ids == {"w2:p1", "w9:p1"}
    assert snap.by_pane()["w9:p1"].status is AgentStatus.BLOCKED
    assert runner.calls == [["herdr", "agent", "list"]]


def test_list_agents_zero_agents_is_success_not_failure():
    snap = run(CliHerdrSource(runner=FakeRunner(ok(agent_list_payload())),
                              binary="herdr").list_agents())
    assert snap.ok
    assert snap.agents == ()


def test_list_agents_nonzero_exit_reports_not_raises():
    runner = FakeRunner(CommandResult(1, "", "server not running"))
    snap = run(CliHerdrSource(runner=runner, binary="herdr").list_agents())
    assert not snap.ok
    assert "server not running" in snap.reason
    assert snap.agents == ()


def test_list_agents_timeout_reports_not_raises():
    runner = FakeRunner(raises=HerdrError("timed out after 15s: herdr agent"))
    snap = run(CliHerdrSource(runner=runner, binary="herdr").list_agents())
    assert not snap.ok
    assert "timed out" in snap.reason


def test_list_agents_binary_missing_reports_not_raises():
    runner = FakeRunner(raises=FileNotFoundError("no such file"))
    snap = run(CliHerdrSource(runner=runner, binary="herdr").list_agents())
    assert not snap.ok
    assert "cannot run herdr" in snap.reason


def test_list_agents_malformed_json_reports_not_raises():
    for bad in ("not json at all", "{}", '{"result": {}}', '{"result": {"agents": 3}}'):
        snap = run(CliHerdrSource(runner=FakeRunner(ok(bad)),
                                  binary="herdr").list_agents())
        assert not snap.ok, bad
        assert snap.agents == ()


def test_list_agents_drops_records_with_unusable_pane_id():
    runner = FakeRunner(ok(agent_list_payload(
        raw_agent("w2:p1"),
        raw_agent("not-a-pane"),
        {"agent_status": "idle"},
    )))
    snap = run(CliHerdrSource(runner=runner, binary="herdr").list_agents())
    assert snap.ok
    assert snap.pane_ids == {"w2:p1"}


# -------------------------------------------------------------- read_pane


def test_read_pane_returns_buffer():
    runner = FakeRunner(ok("Do you want to proceed?\n  1. Yes\n  4. No\n"))
    out = run(CliHerdrSource(runner=runner, binary="herdr").read_pane("w9:p1"))
    assert "1. Yes" in out
    assert runner.calls[0] == [
        "herdr", "agent", "read", "w9:p1",
        "--source", "detection", "--lines", "40",
    ]


def test_read_pane_failure_is_none_not_raise():
    runner = FakeRunner(CommandResult(1, "", "no such pane"))
    assert run(CliHerdrSource(runner=runner, binary="herdr").read_pane("w9:p1")) is None


def test_read_pane_rejects_bad_pane_id():
    src = CliHerdrSource(runner=FakeRunner(), binary="herdr")
    with pytest.raises(HerdrError):
        run(src.read_pane("w9:p1 && calc.exe"))


# -------------------------------------------------------------- send_keys


def test_send_keys_builds_argv_list_never_a_string():
    runner = FakeRunner(ok(""))
    run(CliHerdrSource(runner=runner, binary="herdr").send_keys("w9:p1", ["down", "enter"]))
    # Keys stay separate argv entries; nothing is ever joined or quoted.
    assert runner.calls[0] == ["herdr", "agent", "send-keys", "w9:p1", "down", "enter"]


def test_send_keys_rejects_injection_shaped_pane_id():
    src = CliHerdrSource(runner=FakeRunner(ok("")), binary="herdr")
    for bad in ("w9:p1; rm -rf /", "$(whoami)", "--help", "", "w9:p1\nenter"):
        with pytest.raises(HerdrError):
            run(src.send_keys(bad, ["enter"]))


def test_send_keys_rejects_flag_shaped_keys():
    src = CliHerdrSource(runner=FakeRunner(ok("")), binary="herdr")
    with pytest.raises(HerdrError):
        run(src.send_keys("w9:p1", ["--upload-port"]))
    with pytest.raises(HerdrError):
        run(src.send_keys("w9:p1", [""]))
    with pytest.raises(HerdrError):
        run(src.send_keys("w9:p1", []))


def test_send_keys_surfaces_failure():
    runner = FakeRunner(CommandResult(2, "", "invalid key: yy"))
    src = CliHerdrSource(runner=runner, binary="herdr")
    with pytest.raises(HerdrError) as e:
        run(src.send_keys("w9:p1", ["yy"]))
    assert "invalid key" in str(e.value)


def test_focus_argv_and_failure():
    runner = FakeRunner(ok(""))
    run(CliHerdrSource(runner=runner, binary="herdr").focus("w5:p1"))
    assert runner.calls[0] == ["herdr", "agent", "focus", "w5:p1"]

    src = CliHerdrSource(runner=FakeRunner(CommandResult(1, "", "gone")), binary="herdr")
    with pytest.raises(HerdrError):
        run(src.focus("w5:p1"))


# ----------------------------------------------------------- binary lookup


def test_herdr_binary_prefers_injected_env(monkeypatch):
    # Herdr injects HERDR_BIN_PATH into plugin-spawned processes; preferring
    # it means the relay works even when PATH is not what the CLI expects.
    monkeypatch.setenv("HERDR_BIN_PATH", r"C:\Programs\Herdr\bin\herdr.exe")
    assert herdr_binary() == r"C:\Programs\Herdr\bin\herdr.exe"


def test_herdr_binary_falls_back(monkeypatch):
    monkeypatch.delenv("HERDR_BIN_PATH", raising=False)
    assert herdr_binary().lower().endswith("herdr") or "herdr" in herdr_binary().lower()
