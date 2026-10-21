"""Tests for the read-only Flight Deck route GET /api/ops/owner-inbox."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import operator_autopilot as autopilot
import operator_policy as op
import ui_ops


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path: Path):
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.delenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    yield hermes
    op.set_audit_log_override(None)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(Starlette(routes=ui_ops.ui_ops_routes()))


def test_owner_inbox_empty_root_envelope(client):
    resp = client.get("/api/ops/owner-inbox")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["items"] == [] and data["count"] == 0
    assert data["complete"] is True and data["read_only"] is True
    assert data["sources"] == {
        "missions": "empty", "autopilot": "empty", "controller_attention": "empty",
        "work_contract_review": "empty", "acks": "empty",
    }


def test_owner_inbox_lists_waiting_autopilot_run(client, isolate):
    autopilot._write_run("msn-routeauto", isolate, state="waiting_for_owner", attempt=1, max_concurrency=1)  # noqa: SLF001
    data = client.get("/api/ops/owner-inbox?limit=5").json()["data"]
    assert [i["kind"] for i in data["items"]] == ["autopilot_waiting_owner"]
    assert data["limit"] == 5


@pytest.mark.parametrize("raw", ["abc", "0", "201"])
def test_owner_inbox_rejects_bad_limit(client, raw):
    resp = client.get(f"/api/ops/owner-inbox?limit={raw}")
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_owner_inbox_is_get_only(client):
    assert client.post("/api/ops/owner-inbox", json={}).status_code == 405


def test_owner_inbox_respects_mission_allowlist(client, monkeypatch):
    monkeypatch.setenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", "")
    resp = client.get("/api/ops/owner-inbox")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "AUTHZ_DENIED"


def test_owner_inbox_allowlist_listed_surface_allows(client, monkeypatch):
    monkeypatch.setenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", "approvals")
    assert client.get("/api/ops/owner-inbox").status_code == 200
