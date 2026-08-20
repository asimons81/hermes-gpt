from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import ui_api
import ui_fabric


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture()
def client() -> TestClient:
    return TestClient(Starlette(routes=ui_fabric.ui_fabric_routes()))


def test_fabric_nodes_route_uses_redacted_read_model(client, monkeypatch):
    monkeypatch.setattr(
        ui_fabric.view,
        "nodes_view",
        lambda **_kwargs: {
            "success": True,
            "available": True,
            "nodes": [
                {
                    "name": "node-a",
                    "identity": "identity-a",
                    "availability": "stale",
                    "accidental_secret": "Bearer abcdefghijklmnopqrstuvwxyz123456",
                }
            ],
        },
    )
    resp = client.get("/api/ops/fabric/nodes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    serialized = resp.text
    assert "node-a" in serialized
    assert "Bearer abcdef" not in serialized


def test_fabric_attempts_route_is_get_only(client, monkeypatch):
    monkeypatch.setattr(
        ui_fabric.view,
        "attempts_view",
        lambda **_kwargs: {"success": True, "available": True, "attempts": [], "count": 0},
    )
    assert client.get("/api/ops/fabric/attempts").status_code == 200
    assert client.post("/api/ops/fabric/attempts", json={"confirm": True}).status_code == 405


def test_fabric_attempt_detail_rejects_invalid_id(client):
    resp = client.get("/api/ops/fabric/attempts/not valid")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "FABRIC_ATTEMPT_INVALID"


def test_fabric_attempt_detail_never_exposes_private_artifact_path(client, monkeypatch):
    monkeypatch.setattr(
        ui_fabric.view,
        "attempt_detail",
        lambda *_args, **_kwargs: {
            "success": True,
            "available": True,
            "attempt": {
                "attempt_id": "fatt-123",
                "artifacts": [
                    {
                        "logical_name": "result.html",
                        "active_content": True,
                        "render_policy": "isolated_metadata_only",
                    }
                ],
            },
        },
    )
    resp = client.get("/api/ops/fabric/attempts/fatt-123")
    assert resp.status_code == 200
    data = resp.json()["data"]["attempt"]
    assert data["artifacts"][0]["render_policy"] == "isolated_metadata_only"
    assert "admission_path" not in resp.text
    assert "/home/" not in resp.text


def test_ui_api_composes_fabric_routes():
    paths = {getattr(route, "path", "") for route in ui_api.ui_routes()}
    assert "/api/ops/fabric/nodes" in paths
    assert "/api/ops/fabric/attempts" in paths
    assert "/api/ops/fabric/attempts/{attempt_id}" in paths
    assert "/api/ops/fabric/routing" in paths
