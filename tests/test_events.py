"""T7b coverage: the push-event wire format.

The parsing and request-building here are pure, so these tests pin the exact
shapes verified against Herdr 0.8.2 without touching the socket. That matters
more than usual for this module: Herdr spells this one event four different
ways across four surfaces, and getting it wrong produces silence rather than
an error. A test that asserts the dotted form is the thing standing between
a working push feed and an afternoon of debugging nothing happening.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.events import (  # noqa: E402
    EVENT_AGENT_STATUS_CHANGED,
    Ack,
    Failure,
    StatusEvent,
    build_subscribe_request,
    encode_request,
    parse_line,
    socket_path,
)
from shepherd.models import AgentStatus  # noqa: E402


# ------------------------------------------------------- request building


def test_subscription_type_is_the_dotted_spelling():
    # events.subscribe takes the DOTTED form. The underscored form belongs to
    # events.wait and to the event record, and sending it here yields a
    # subscription that silently never fires.
    req = build_subscribe_request(["w2:p1"])
    assert EVENT_AGENT_STATUS_CHANGED == "pane.agent_status_changed"
    assert req["params"]["subscriptions"][0]["type"] == "pane.agent_status_changed"
    assert "_" not in req["params"]["subscriptions"][0]["type"].split(".")[0]


def test_subscription_is_per_pane_and_carries_pane_id():
    # The schema marks pane_id required; a global subscription is not a thing.
    req = build_subscribe_request(["w2:p1", "w9:p1", "wA:p1"])
    subs = req["params"]["subscriptions"]
    assert len(subs) == 3
    assert [s["pane_id"] for s in subs] == ["w2:p1", "w9:p1", "wA:p1"]
    assert all("pane_id" in s for s in subs)


def test_status_filter_narrows_the_feed_at_the_source():
    req = build_subscribe_request(["w2:p1"], statuses=(AgentStatus.BLOCKED,))
    subs = req["params"]["subscriptions"]
    assert len(subs) == 1
    assert subs[0]["agent_status"] == "blocked"


def test_status_filter_expands_per_status():
    req = build_subscribe_request(
        ["w2:p1"], statuses=(AgentStatus.BLOCKED, AgentStatus.DONE)
    )
    got = {(s["pane_id"], s["agent_status"]) for s in req["params"]["subscriptions"]}
    assert got == {("w2:p1", "blocked"), ("w2:p1", "done")}


def test_unfiltered_subscription_omits_agent_status():
    req = build_subscribe_request(["w2:p1"], statuses=None)
    assert "agent_status" not in req["params"]["subscriptions"][0]


def test_request_envelope_shape():
    req = build_subscribe_request(["w2:p1"], request_id="probe")
    assert set(req) == {"id", "method", "params"}
    assert req["id"] == "probe"
    assert req["method"] == "events.subscribe"


def test_invalid_pane_ids_are_dropped_and_empty_is_refused():
    req = build_subscribe_request(["w2:p1", "nonsense", "; rm -rf /"])
    assert [s["pane_id"] for s in req["params"]["subscriptions"]] == ["w2:p1"]
    with pytest.raises(ValueError):
        build_subscribe_request(["nonsense"])
    with pytest.raises(ValueError):
        build_subscribe_request([])


def test_encode_request_is_one_line_terminated():
    blob = encode_request(build_subscribe_request(["w2:p1"]))
    assert blob.endswith(b"\n")
    assert blob.count(b"\n") == 1
    assert json.loads(blob.decode())["method"] == "events.subscribe"


# ------------------------------------------------------------- parsing


def test_parses_a_real_pushed_event():
    # Captured verbatim from Herdr 0.8.2 during probe 4.
    line = json.dumps({
        "data": {
            "agent": "claude",
            "agent_status": "done",
            "pane_id": "wA:p1",
            "workspace_id": "wA",
        },
        "event": "pane.agent_status_changed",
    })
    ev = parse_line(line)
    assert isinstance(ev, StatusEvent)
    assert ev.pane_id == "wA:p1"
    assert ev.workspace_id == "wA"
    assert ev.status is AgentStatus.DONE
    assert ev.kind == "claude"


def test_parses_the_subscription_ack():
    line = json.dumps({"id": "probe4", "result": {"type": "subscription_started"}})
    ack = parse_line(line)
    assert isinstance(ack, Ack)
    assert ack.kind == "subscription_started"
    assert ack.request_id == "probe4"


def test_parses_an_error_frame():
    # The shape Herdr returned when pane_id was omitted.
    line = json.dumps({
        "error": {
            "code": "invalid_request",
            "message": "invalid request: missing field `pane_id` at line 1 column 116",
        }
    })
    f = parse_line(line)
    assert isinstance(f, Failure)
    assert f.code == "invalid_request"
    assert "pane_id" in f.message


def test_ignores_other_event_kinds():
    # scroll_changed and output_matched also stream; they are not ours.
    for kind in ("pane.scroll_changed", "pane.output_matched"):
        line = json.dumps({"event": kind, "data": {"pane_id": "w2:p1"}})
        assert parse_line(line) is None


def test_underscored_spelling_is_not_mistaken_for_the_push_form():
    # The event record uses underscores; the push envelope does not. If Herdr
    # ever sent the underscored form here we would rather see nothing than
    # silently half-support two spellings.
    line = json.dumps({
        "event": "pane_agent_status_changed",
        "data": {"pane_id": "w2:p1", "agent_status": "blocked"},
    })
    assert parse_line(line) is None


def test_parse_is_total_on_junk():
    for junk in ("", "   ", "not json", "[]", "3", '"str"', "{}",
                 '{"event": "pane.agent_status_changed"}',
                 '{"event": "pane.agent_status_changed", "data": 7}'):
        assert parse_line(junk) is None, junk


def test_event_with_unusable_pane_id_is_dropped():
    line = json.dumps({
        "event": "pane.agent_status_changed",
        "data": {"pane_id": "not-a-pane", "agent_status": "blocked"},
    })
    assert parse_line(line) is None


def test_unknown_status_in_push_becomes_unknown_not_a_crash():
    line = json.dumps({
        "event": "pane.agent_status_changed",
        "data": {"pane_id": "w2:p1", "agent_status": "reticulating"},
    })
    ev = parse_line(line)
    assert isinstance(ev, StatusEvent)
    assert ev.status is AgentStatus.UNKNOWN


# --------------------------------------------------------- socket path


def test_socket_path_prefers_injected_env(monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", r"C:\Users\x\AppData\Roaming\herdr\herdr.sock")
    p = socket_path()
    if sys.platform == "win32":
        assert p == r"\\.\pipe\C:\Users\x\AppData\Roaming\herdr\herdr.sock"
    else:
        assert p.endswith("herdr.sock")
