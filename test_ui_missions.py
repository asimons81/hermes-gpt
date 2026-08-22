from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import operator_live_events as live_events
import operator_mission_runtime as missions
import operator_policy as op
import ui_api
import ui_missions


def _spec(mission_id: str = "msn-ui-test") -> str:
    return json.dumps(
        {
            "schema": missions.MISSION_SPEC_SCHEMA,
            "mission_id": mission_id,
            "title": "Flight Deck mission",
            "objective": "Observe mission progress without adding browser authority.",
            "owner_profile": "default",
            "acceptance_criteria": ["mission state visible", "delegations visible"],
            "context_refs": [
                {
                    "kind": "document",
                    "ref": "docs:v0.9-plan",
                    "label": "v0.9 plan",
                    "sha256": "a" * 64,
                }
            ],
            "skills": [
                {
                    "name": "compound-engineering",
                    "version": "1",
                    "ref": "skill:compound-engineering",
                }
            ],
            "final_approval_required": True,
        }
    )


@pytest.fixture
def mission_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "hermes"
    root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(workspace))
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    created = json.loads(
        missions.hermes_mission_create(
            _spec(),
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert created["success"] is True
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    return root


def _client() -> TestClient:
    return TestClient(Starlette(routes=ui_missions.ui_missions_routes()))


def test_mission_routes_are_get_only_and_composed(mission_root: Path):
    routes = ui_missions.ui_missions_routes()
    assert routes
    assert all(route.methods == {"GET", "HEAD"} for route in routes)
    composed_paths = {getattr(route, "path", "") for route in ui_api.routes()}
    assert "/api/ops/missions" in composed_paths
    assert "/api/ops/missions/{mission_id}" in composed_paths
    assert "/api/ops/missions/{mission_id}/events" in composed_paths


def test_mission_list_and_detail_surface_bounded_runtime_state(
    mission_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        ui_missions.delegations,
        "hermes_delegation_list",
        lambda **kwargs: json.dumps(
            {
                "success": True,
                "delegations": [
                    {
                        "delegation_id": "dlg-ui-test",
                        "mission_id": "msn-ui-test",
                        "task_id": "task-ui-test",
                        "backend": "opencode",
                        "state": "running",
                    }
                ],
                "count": 1,
            }
        ),
    )
    client = _client()
    listed = client.get("/api/ops/missions?limit=20")
    assert listed.status_code == 200
    list_body = listed.json()
    assert list_body["ok"] is True
    assert list_body["data"]["count"] == 1
    assert list_body["data"]["missions"][0]["mission_id"] == "msn-ui-test"
    assert list_body["data"]["read_only"] is True

    detail = client.get("/api/ops/missions/msn-ui-test")
    assert detail.status_code == 200
    body = detail.json()
    assert body["ok"] is True
    assert body["data"]["mission"]["mission_id"] == "msn-ui-test"
    assert body["data"]["mission"]["context_refs"][0]["sha256"] == "a" * 64
    assert body["data"]["mission"]["skills"][0]["name"] == "compound-engineering"
    assert body["data"]["delegations"][0]["backend"] == "opencode"
    assert body["data"]["delegation_count"] == 1
    assert body["data"]["read_only"] is True


def test_mission_events_deliver_cursor_filtered_wakeup(mission_root: Path):
    published = live_events.publish_event(
        topic="mission",
        kind="mission.test",
        subject_type="mission",
        subject_id="msn-ui-test",
        mission_id="msn-ui-test",
        source="test-ui-missions",
        payload={"state": "running"},
        hermes_root=mission_root,
    )
    assert published["mission_id"] == "msn-ui-test"
    client = _client()
    response = client.get("/api/ops/missions/msn-ui-test/events?cursor=0&limit=10")
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["count"] >= 1
    assert any(event["mission_id"] == "msn-ui-test" for event in body["events"])
    assert body["next_cursor"] >= 1
    assert body["read_only"] is True


def test_delegation_detail_uses_existing_read_surface(
    mission_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        ui_missions.delegations,
        "hermes_delegation_get",
        lambda delegation_id, hermes_root=None: json.dumps(
            {
                "success": True,
                "delegation": {
                    "delegation_id": delegation_id,
                    "task_id": "task-ui-test",
                    "backend": "pi_rpc",
                    "state": "succeeded",
                },
            }
        ),
    )
    response = _client().get("/api/ops/delegations/dlg-ui-test")
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["delegation"]["state"] == "succeeded"
    assert body["read_only"] is True


def test_missing_mission_returns_not_found(mission_root: Path):
    response = _client().get("/api/ops/missions/msn-missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MISSION_NOT_FOUND"
