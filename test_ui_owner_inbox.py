"""Tests for the read-only Flight Deck route GET /api/ops/owner-inbox."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import operator_autopilot as autopilot
import operator_owner_inbox as op_owner_inbox
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


# ---------------------------------------------------------------------------
# v0.16: the autopilot item carries structured plan and frontier ids so the
# Flight Deck can offer per-node actions. Ids stay keyed on kind/mission/frontier.
# ---------------------------------------------------------------------------


def _gate(node_id: str, parents=()) -> dict:
    from test_operator_autopilot_scheduler import _node

    return _node(node_id, parents, kind="approval", owner="owner")


def test_autopilot_item_exposes_plan_and_frontier_node_ids(client, isolate, monkeypatch, tmp_path):
    from test_operator_autopilot_advance import _observe, _task
    from test_operator_autopilot_scheduler import MID, _mk, _node, _tick, make_env

    (tmp_path / "scenario").mkdir()
    root, backend = make_env(tmp_path / "scenario", monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(root))
    _mk(root, [_node("a"), _gate("gate", ["a"]), _node("c", ["gate"])])
    _tick(root)
    _observe(root, _task(backend), state="completed")
    _tick(root)

    data = client.get("/api/ops/owner-inbox?limit=10").json()["data"]
    items = [i for i in data["items"] if i["kind"] == "autopilot_waiting_owner"]
    assert len(items) == 1, data
    item = items[0]
    assert item["mission_id"] == MID
    assert item["plan_id"] == MID
    assert item["frontier_node_ids"] == ["gate"]
    assert item["suggested_tool"] == "hermes_plan_approve_gate"
    assert item["item_id"] == op_owner_inbox._item_id("autopilot_waiting_owner", MID, "gate")  # noqa: SLF001


def test_autopilot_item_id_is_unchanged_by_the_structured_fields(isolate):
    # The stable id is derived from kind, mission and the sorted frontier only.
    mid = "msn-routeauto"
    autopilot._write_run(mid, isolate, state="waiting_for_owner", attempt=1, max_concurrency=1)  # noqa: SLF001
    first = op_owner_inbox.build_inbox(hermes_root=isolate, limit=5)["items"]
    second = op_owner_inbox.build_inbox(hermes_root=isolate, limit=5)["items"]
    assert [i["item_id"] for i in first] == [i["item_id"] for i in second]
    assert len(first) == 1 and first[0]["frontier_node_ids"] == []
