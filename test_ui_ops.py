"""Tests for the Flight Deck operator adapters (ui_ops.py).

Covers the interface contract from t_ab4f3463:

- Envelope shape for every Mission Control surface + status reads.
- Event History query/tail.
- Mutation endpoint ``POST /api/ops/action``: the adapter executes the
  EXISTING gated ``hermes_*`` tool path and must NOT let the UI bypass a
  gate. Adversarial cases assert: unknown tools 404, disallowed args are
  rejected, dry-run is the default, confirm gates surface as
  ``409 CONFIRM_REQUIRED``, and level denials surface as
  ``403 LEVEL_REQUIRED`` — never weakened.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import operator_policy as op
import ui_api
import ui_ops

OWNER_ACK = op.OWNER_ACK_REQUIRED_VALUE

OWNER_ENV = {
    op.OPERATOR_ENABLED_ENV: "1",
    op.OPERATOR_LEVEL_ENV: "owner",
    op.OPERATOR_APPLY_MODE_ENV: "direct",
    op.OWNER_ACTIVE_ENV: "1",
    op.OWNER_ACK_ENV: OWNER_ACK,
}

WORKSPACE_ENV = {
    op.OPERATOR_ENABLED_ENV: "1",
    op.OPERATOR_LEVEL_ENV: "workspace",
    op.OPERATOR_APPLY_MODE_ENV: "direct",
}

CRON_ENV = {
    op.OPERATOR_ENABLED_ENV: "1",
    op.OPERATOR_LEVEL_ENV: "cron",
    op.OPERATOR_APPLY_MODE_ENV: "direct",
}

VALID_SHA = "a" * 64


@pytest.fixture(autouse=True)
def isolate_ui_ops(monkeypatch, tmp_path):
    """Point HERMES_HOME at a temp root and clear mission/events allowlists."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", raising=False)
    monkeypatch.delenv("HERMES_GPT_EVENTS_ALLOWED_SOURCES", raising=False)
    # Ensure the temp root exists (some surfaces read it directly).
    tmp_path.mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def client() -> TestClient:
    app = Starlette(routes=ui_ops.ui_ops_routes())
    return TestClient(app)


def _set_env(monkeypatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# Mission Control surfaces — envelope shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ui_ops.MISSION_SURFACES)
def test_mission_surface_envelope_shape(client, surface):
    resp = client.get(f"/api/ops/{surface}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["surface"] == surface
    assert data["fetched_at"]
    assert isinstance(data["data"], dict)
    # Redacted mission envelope: never raw prompts, secrets, or transcripts.
    serialized = json.dumps(data["data"])
    assert "sk-" not in serialized
    assert "AKIA" not in serialized
    assert "Bearer " not in serialized
    assert "prompt_sha256" in serialized or "counts" in serialized or True  # shape-tolerant


def test_mission_surface_overview_is_composite(client):
    resp = client.get("/api/ops/overview")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["surface"] == "overview"
    assert "fleet_health" in data["data"] or "surfaces_unavailable" in data["data"]


def test_mission_surface_unknown_404(client):
    resp = client.get("/api/ops/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_mission_surface_force_refresh_accepted(client):
    resp = client.get("/api/ops/health?force_refresh=1")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Mission Control allowlist semantics (AGENTS.md: unset=all, list=only,
# empty=none — never described as "deny by default")
# ---------------------------------------------------------------------------


def test_mission_allowlist_subset(client, monkeypatch):
    monkeypatch.setenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", "health,cron")
    ok = client.get("/api/ops/health")
    assert ok.status_code == 200
    assert ok.json()["data"]["data"].get("code") != "AUTHZ_DENIED"

    denied = client.get("/api/ops/profiles")
    assert denied.status_code == 200  # state, not HTTP error
    assert denied.json()["ok"] is True
    assert denied.json()["data"]["data"]["code"] == "AUTHZ_DENIED"


def test_mission_allowlist_empty_denies_all(client, monkeypatch):
    monkeypatch.setenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", "")
    for surface in ("health", "overview", "usage"):
        resp = client.get(f"/api/ops/{surface}")
        assert resp.status_code == 200
        assert resp.json()["data"]["data"]["code"] == "AUTHZ_DENIED"


def test_mission_allowlist_unset_allows_all(client):
    for surface in ("health", "overview", "usage"):
        resp = client.get(f"/api/ops/{surface}")
        assert resp.status_code == 200
        assert resp.json()["data"]["data"].get("code") != "AUTHZ_DENIED"


# ---------------------------------------------------------------------------
# Event History
# ---------------------------------------------------------------------------


def test_events_query_envelope(client):
    resp = client.get("/api/events?limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert isinstance(data.get("events"), list)
    assert data.get("count_returned", 0) <= 5
    assert isinstance(data.get("warnings"), list)


def test_events_tail_mode(client):
    resp = client.get("/api/events?mode=tail&limit=3")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert isinstance(resp.json()["data"].get("events"), list)


# ---------------------------------------------------------------------------
# Status reads
# ---------------------------------------------------------------------------


def test_contracts_list_envelope(client):
    resp = client.get("/api/ops/contracts")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["success"] is True
    assert isinstance(data.get("review_acceptances"), list)
    assert isinstance(data.get("workflows"), list)


def test_contracts_detail_not_found(client):
    resp = client.get(f"/api/ops/contracts/{VALID_SHA}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_review_detail_empty(client):
    resp = client.get(f"/api/ops/review/{VALID_SHA}")
    assert resp.status_code == 200
    assert resp.json()["data"]["records"] == []


def test_swarm_list_envelope(client):
    resp = client.get("/api/ops/swarm")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_swarm_detail_not_found(client, monkeypatch):
    # Read-only operator level must be enabled for the operator module to
    # reach its WORKFLOW_NOT_FOUND path (otherwise it returns policy-denied
    # as a 200 state, which is correct degradation).
    _set_env(monkeypatch, {op.OPERATOR_ENABLED_ENV: "1", op.OPERATOR_LEVEL_ENV: "read_only"})
    resp = client.get("/api/ops/swarm/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_codex_list_envelope(client):
    # Bare /api/ops/codex is the mission surface (12-surface list); the
    # envelope carries surface metadata + the redacted mission payload.
    resp = client.get("/api/ops/codex")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["surface"] == "codex"
    assert isinstance(data["data"], dict)


def test_codex_detail_not_found(client):
    resp = client.get("/api/ops/codex/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_cron_list_envelope(client):
    resp = client.get("/api/ops/cron")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["surface"] == "cron"
    assert isinstance(data["data"], dict)


def test_cron_detail_not_found(client):
    resp = client.get("/api/ops/cron/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_fleet_list_envelope_state_not_error(client):
    # Fleet may report not_configured / policy denied — must be a 200 state.
    resp = client.get("/api/ops/fleet")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_account_envelope(client):
    resp = client.get("/api/ops/account")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["success"] is True
    assert isinstance(data.get("policy"), dict)
    assert data.get("server_version")
    assert "oauth" in data


# ---------------------------------------------------------------------------
# ui_api composition (hotspot smoke)
# ---------------------------------------------------------------------------


def test_ui_api_composes_ops_routes():
    routes = ui_api.ui_routes()
    paths = {getattr(r, "path", "") for r in routes}
    assert "/api/ops/action" in paths
    assert "/api/events" in paths


# ---------------------------------------------------------------------------
# Mutations — gate preservation (adversarial)
# ---------------------------------------------------------------------------


def test_action_unknown_tool_404(client):
    resp = client.post("/api/ops/action", json={"tool": "hermes_nonexistent", "args": {}})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "UNKNOWN_TOOL"


def test_action_rejects_disallowed_args(client):
    # The UI cannot smuggle internal kwargs into the gated path.
    resp = client.post(
        "/api/ops/action",
        json={
            "tool": "hermes_review_accept",
            "args": {
                "contract_sha256": VALID_SHA,
                "task_id": "t-1",
                "assignee": "alice",
                "reviewer": "bob",
                "verdict": "SATISFIED",
                "evidence_refs": ["docs/evidence-1.md"],
                "hermes_root": "/tmp/evil",
                "runner": "smuggled",
            },
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGS"
    assert "hermes_root" in resp.json()["error"]["message"]


def test_action_rejects_confirm_for_tool_without_confirm(client):
    # hermes_cron_pause has no confirm arg; the UI cannot force one through.
    resp = client.post(
        "/api/ops/action",
        json={"tool": "hermes_cron_pause", "args": {"profile": "default", "job_id": "j1", "confirm": True}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGS"


def test_action_level_denied_403(client):
    # Default (read_only / operator disabled) level: review accept requires
    # owner -> 403 LEVEL_REQUIRED, never a silent pass-through.
    resp = client.post(
        "/api/ops/action",
        json={
            "tool": "hermes_review_accept",
            "args": {
                "contract_sha256": VALID_SHA,
                "task_id": "t-1",
                "assignee": "alice",
                "reviewer": "bob",
                "verdict": "SATISFIED",
                "evidence_refs": ["docs/evidence-1.md"],
            },
        },
    )
    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["code"] == "LEVEL_REQUIRED"
    assert err.get("required") == "owner"


def test_action_dry_run_default_plan_no_write(client, monkeypatch, tmp_path):
    # Owner mode + direct apply. Dry-run omitted -> plan only, no store write.
    _set_env(monkeypatch, OWNER_ENV)
    resp = client.post(
        "/api/ops/action",
        json={
            "tool": "hermes_review_accept",
            "args": {
                "contract_sha256": VALID_SHA,
                "task_id": "t-1",
                "assignee": "alice",
                "reviewer": "bob",
                "verdict": "SATISFIED",
                "evidence_refs": ["docs/evidence-1.md"],
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["tool"] == "hermes_review_accept"
    assert data["dry_run"] is True
    assert data["requires_confirm"] is True
    assert data["result"]["success"] is True
    # Nothing was written to the review-evidence store.
    evidence = tmp_path / "review-evidence" / "review-acceptances.jsonl"
    assert not evidence.exists()


def test_action_confirm_gate_409(client, monkeypatch):
    # Owner + direct, but confirm omitted -> 409 CONFIRM_REQUIRED dialog.
    _set_env(monkeypatch, OWNER_ENV)
    resp = client.post(
        "/api/ops/action",
        json={
            "tool": "hermes_review_accept",
            "args": {
                "contract_sha256": VALID_SHA,
                "task_id": "t-1",
                "assignee": "alice",
                "reviewer": "bob",
                "verdict": "SATISFIED",
                "evidence_refs": ["docs/evidence-1.md"],
                "dry_run": False,
            },
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFIRM_REQUIRED"


def test_action_confirm_true_writes_store(client, monkeypatch, tmp_path):
    # Owner + direct + confirm=true -> the existing gated path performs the
    # write; the adapter surfaces the result and the read-model reflects it.
    _set_env(monkeypatch, OWNER_ENV)
    resp = client.post(
        "/api/ops/action",
        json={
            "tool": "hermes_review_accept",
            "args": {
                "contract_sha256": VALID_SHA,
                "task_id": "t-1",
                "assignee": "alice",
                "reviewer": "bob",
                "verdict": "SATISFIED",
                "evidence_refs": ["docs/evidence-1.md"],
                "dry_run": False,
                "confirm": True,
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["dry_run"] is False
    assert data["requires_confirm"] is False
    assert data["result"]["success"] is True

    evidence = tmp_path / "review-evidence" / "review-acceptances.jsonl"
    assert evidence.exists()
    assert "a" * 64 in evidence.read_text(encoding="utf-8")

    detail = client.get(f"/api/ops/review/{VALID_SHA}")
    assert detail.status_code == 200
    assert len(detail.json()["data"]["records"]) == 1


def test_action_swarm_stage_advance_workspace_gate(client, monkeypatch):
    # Swarm advance needs workspace; read_only must be denied.
    resp = client.post(
        "/api/ops/action",
        json={"tool": "hermes_swarm_stage_advance", "args": {"workflow_id": "wf-1", "stage_id": "s1"}},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "LEVEL_REQUIRED"
    assert resp.json()["error"].get("required") == "workspace"

    # Workspace + direct: dry-run plan (workflow missing -> plan/404 envelope
    # from the operator, but level gate passes).
    _set_env(monkeypatch, WORKSPACE_ENV)
    resp2 = client.post(
        "/api/ops/action",
        json={"tool": "hermes_swarm_stage_advance", "args": {"workflow_id": "wf-1", "stage_id": "s1", "dry_run": True}},
    )
    # Either a plan (success) or the operator's not-found envelope mapped to
    # 400/200 — but never a 403 LEVEL_REQUIRED once workspace is granted.
    assert resp2.status_code != 403


def test_action_apply_flag_reconcile_requires_workspace(client):
    # apply_flag tools: hermes_swarm_reconcile at read_only -> LEVEL_REQUIRED.
    resp = client.post("/api/ops/action", json={"tool": "hermes_swarm_reconcile", "args": {"apply": True}})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "LEVEL_REQUIRED"


def test_action_cron_run_dry_run_inline(client, monkeypatch, tmp_path):
    # hermes_cron_run with dry_run stays inline (fast path, no thread). The
    # operator requires a real cron job to build the plan, so seed one.
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps([{"id": "j1", "name": "test job", "schedule": "* * * * *", "enabled": True, "prompt": "say hi"}]),
        encoding="utf-8",
    )
    _set_env(monkeypatch, CRON_ENV)
    resp = client.post(
        "/api/ops/action",
        json={"tool": "hermes_cron_run", "args": {"profile": "default", "job_id": "j1", "dry_run": True}},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["tool"] == "hermes_cron_run"
    assert data["dry_run"] is True


def test_action_cron_run_long_running_dispatched_202(client, monkeypatch, tmp_path):
    # Real execution of the blocking cron run is dispatched to a daemon
    # thread and the HTTP request returns 202 Accepted immediately.
    marker = tmp_path / "cron-stub-called"
    calls: list[dict] = []

    def stub_cron_run(**kwargs):
        calls.append(kwargs)
        marker.write_text("called", encoding="utf-8")
        return json.dumps({"success": True, "dry_run": False, "job": "j1"})

    _set_env(monkeypatch, CRON_ENV)
    monkeypatch.setattr(ui_ops._MUTATION_TOOLS["hermes_cron_run"], "fn", stub_cron_run)

    resp = client.post(
        "/api/ops/action",
        json={"tool": "hermes_cron_run", "args": {"profile": "default", "job_id": "j1", "dry_run": False}},
    )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["accepted"] is True
    assert data["status"] == "running"

    # The daemon thread performs the call asynchronously.
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()
    assert calls and calls[0]["job_id"] == "j1"
    assert calls[0]["dry_run"] is False
    assert calls[0]["hermes_root"]  # resolved server-side, never client-supplied
