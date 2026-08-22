from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import oauth_auth
import operator_live_events as live
import operator_policy as op
import server


@pytest.fixture
def hermes_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "dry_run")
    return root


def test_read_before_publish_is_noncreating(hermes_root: Path):
    out = json.loads(live.hermes_live_events_since(0, hermes_root=hermes_root))
    assert out["success"] is True
    assert out["events"] == []
    assert out["high_watermark"] == 0
    assert not (hermes_root / "live-events" / "events.db").exists()


def test_publish_cursor_filter_and_secret_redaction(hermes_root: Path):
    one = live.publish_event(
        topic="mission",
        kind="mission.updated",
        subject_type="mission",
        subject_id="msn-one",
        mission_id="msn-one",
        source="test",
        payload={"status": "running", "prompt": "sensitive prompt", "nested": {"api_key": "secret"}},
        hermes_root=hermes_root,
    )
    two = live.publish_event(
        topic="swarm",
        kind="hermes_swarm_stage_advance",
        subject_type="workflow-stage",
        subject_id="sw-one:build",
        mission_id="msn-one",
        source="test",
        payload={"verdict": "SATISFIED"},
        hermes_root=hermes_root,
    )
    assert one["seq"] < two["seq"]
    assert one["payload"]["prompt"] == "[REDACTED]"
    assert one["payload"]["nested"]["api_key"] == "[REDACTED]"

    out = json.loads(live.hermes_live_events_since(0, mission_id="msn-one", topic="swarm", hermes_root=hermes_root))
    assert out["count"] == 1
    assert out["events"][0]["event_id"] == two["event_id"]
    assert out["next_cursor"] == two["seq"]
    assert out["high_watermark"] == two["seq"]


def test_event_id_is_idempotent(hermes_root: Path):
    kwargs = {
        "topic": "delegation",
        "kind": "delegation.completed",
        "subject_type": "delegation",
        "subject_id": "dlg-one",
        "source": "test",
        "payload": {"state": "succeeded"},
        "event_id": "lev-fixed",
        "hermes_root": hermes_root,
    }
    first = live.publish_event(**kwargs)
    second = live.publish_event(**kwargs)
    assert first["seq"] == second["seq"]
    out = json.loads(live.hermes_live_events_since(0, hermes_root=hermes_root))
    assert out["count"] == 1


def test_mission_runtime_event_bridge(hermes_root: Path, monkeypatch):
    import operator_mission_runtime as mission

    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    spec = json.dumps(
        {
            "schema": mission.MISSION_SPEC_SCHEMA,
            "mission_id": "msn-live",
            "title": "Live mission",
            "objective": "Prove Mission events wake the live bus.",
        }
    )
    created = json.loads(mission.hermes_mission_create(spec, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert created["success"] is True
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    out = json.loads(live.hermes_live_events_since(0, mission_id="msn-live", hermes_root=hermes_root))
    assert out["count"] >= 1
    assert any(event["kind"] == "mission.created" for event in out["events"])


def test_mission_live_wakeup_is_published_after_authoritative_commit(hermes_root: Path, monkeypatch):
    import operator_mission_runtime as mission

    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    observed: dict[str, str] = {}
    real_publish = live.publish_event

    def publish_after_commit(**kwargs):
        snapshot = json.loads(mission.hermes_mission_get("msn-live-committed", hermes_root=hermes_root))
        assert snapshot["success"] is True
        observed["status"] = snapshot["status"]
        return real_publish(**kwargs)

    monkeypatch.setattr(live, "publish_event", publish_after_commit)
    spec = json.dumps(
        {
            "schema": mission.MISSION_SPEC_SCHEMA,
            "mission_id": "msn-live-committed",
            "title": "Committed live mission",
            "objective": "Wake clients only after durable state commits.",
        }
    )
    created = json.loads(mission.hermes_mission_create(spec, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert created["success"] is True
    assert observed["status"] == "draft"


def test_websocket_stream_and_control_frames(hermes_root: Path):
    app = Starlette(routes=live.websocket_routes(lambda: hermes_root))
    client = TestClient(app)
    live.publish_event(
        topic="mission",
        kind="mission.created",
        subject_type="mission",
        subject_id="msn-ws",
        mission_id="msn-ws",
        source="test",
        payload={"status": "draft"},
        hermes_root=hermes_root,
    )
    with client.websocket_connect("/events/ws?cursor=0&mission_id=msn-ws") as ws:
        batch = ws.receive_json()
        assert batch["type"] == "events"
        assert batch["events"][0]["subject_id"] == "msn-ws"
        ws.send_json({"action": "ping"})
        pong = ws.receive_json()
        assert pong["type"] == "pong"
        ws.send_json({"action": "subscribe", "topic": "swarm", "cursor": batch["cursor"]})
        subscribed = ws.receive_json()
        assert subscribed["type"] == "subscribed"
        assert subscribed["topic"] == "swarm"


def test_websocket_refuses_when_operator_disabled(tmp_path: Path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.delenv(op.OPERATOR_ENABLED_ENV, raising=False)
    app = Starlette(routes=live.websocket_routes(lambda: root))
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/events/ws"):
        pass


def test_server_registers_live_tools_and_composed_websocket(hermes_root: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    built = server.build_server(http=True)
    names = {tool.name for tool in asyncio.run(built.list_tools())}
    assert "hermes_live_events_cursor" in names
    assert "hermes_live_events_since" in names

    app = server.build_asgi_app(built, http=True)
    live.publish_event(
        topic="mission",
        kind="mission.updated",
        subject_type="mission",
        subject_id="msn-server-ws",
        mission_id="msn-server-ws",
        source="test",
        payload={"status": "running"},
        hermes_root=hermes_root,
    )
    with TestClient(app) as client, client.websocket_connect("/events/ws?mission_id=msn-server-ws") as ws:
        batch = ws.receive_json()
        assert batch["type"] == "events"
        assert batch["events"][0]["subject_id"] == "msn-server-ws"


def test_composed_websocket_reuses_server_bearer_boundary(hermes_root: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    bearer = "live-events-test-bearer-value-1234567890-abcdefghi"
    monkeypatch.setenv(oauth_auth.AUTH_TOKEN_ENV, bearer)
    built = server.build_server(http=True)
    app = server.build_asgi_app(built, http=True)

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/events/ws"):
            pass
        with client.websocket_connect(
            "/events/ws",
            headers={"Authorization": f"Bearer {bearer}"},
        ) as ws:
            ws.send_json({"action": "ping"})
            assert ws.receive_json()["type"] == "pong"
