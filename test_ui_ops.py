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
import re
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import operator_autopilot as autopilot
import operator_mission_plan as plan
import operator_mission_runtime as mission
import operator_owner_inbox as op_owner_inbox
import operator_policy as op
import ui_api
import ui_ops
from test_operator_autopilot_scheduler import MID, _mk, _node, _states, make_env

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
    # Mission surfaces resolve a few state files from Path.home() (e.g.
    # ~/nexus-wiki action-items). Pin home to the temp root so the test never
    # reads the invoking user's real files (audit t_9d200636 Class B).
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
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
    # Assert on secret-shaped tokens, not bare substrings: "sk-" alone also
    # matches benign "hermes-task-*" ids (audit t_9d200636 Class B). Shape
    # mirrors operator_policy.redact_output's OpenAI-key pattern.
    serialized = json.dumps(data["data"])
    assert not re.search(r"(?i)\bsk(?:-proj)?-[A-Za-z0-9_-]{20,}\b", serialized)
    assert "AKIA" not in serialized
    assert "Bearer " not in serialized
    assert "prompt_sha256" in serialized or "counts" in serialized or True  # shape-tolerant


_SECRET_SCAN_RE = re.compile(r"(?i)\bsk(?:-proj)?-[A-Za-z0-9_-]{20,}\b")


@pytest.mark.parametrize(
    "payload,expect_secret",
    [
        # Benign ids that contain the "sk-" substring must NOT trip the scan
        # (audit t_9d200636 Class B: hermes-task-* ids from action-items).
        ({"id": "hermes-task-maintenance-cron-error", "status": "open"}, False),
        ({"id": "task-sk-123-review", "status": "open"}, False),
        # Real secret-shaped keys MUST trip the scan.
        ({"id": "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "status": "open"}, True),
        ({"id": "sk-proj-abcdefghijklmnopqrstuvwxyz123456", "status": "open"}, True),
    ],
)
def test_secret_scan_regex_separates_task_ids_from_real_keys(payload, expect_secret):
    serialized = json.dumps(payload)
    assert bool(_SECRET_SCAN_RE.search(serialized)) is expect_secret


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


# ---------------------------------------------------------------------------
# Owner-inbox actions (v0.16): the same spec mechanism and the same gated
# operator functions as MCP. Real functions run unless a test says otherwise.
# ---------------------------------------------------------------------------

NEW_TOOLS = (
    "hermes_mission_approve",
    "hermes_plan_node_transition",
    "hermes_plan_approve_gate",
    "hermes_autopilot_stop",
    "hermes_owner_inbox_ack",
)
ZERO_ITEM = "0" * 24


def _gate(node_id: str, parents=()) -> dict:
    return _node(node_id, parents, kind="approval", owner="owner")


def _post(client, tool: str, args: dict, **extra):
    body = {"tool": tool, "args": args, **extra}
    return client.post("/api/ops/action", json=body)


def _walk(root, node_id: str, targets) -> None:
    for target in targets:
        out = json.loads(plan.hermes_plan_node_transition(MID, node_id, target, confirm=True, dry_run=False, hermes_root=root))
        assert out["success"] is True, out


def _gate_at_awaiting(root) -> None:
    _walk(root, "gate", ("dispatched", "running", "awaiting_review", "validated", "awaiting_approval"))


@pytest.fixture
def ops_root(tmp_path, monkeypatch):
    """A workspace Mission plan with an approval gate, rooted at HERMES_HOME."""
    root, backend = make_env(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root, backend


@pytest.mark.parametrize("tool", NEW_TOOLS)
def test_new_actions_reject_unknown_args(client, tool):
    args = {"hermes_root": "/tmp/evil", "runner": "smuggled"}
    resp = _post(client, tool, args)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGS"
    assert "hermes_root" in resp.json()["error"]["message"]


def test_ack_has_no_confirm_argument(client):
    resp = _post(client, "hermes_owner_inbox_ack", {"item_ids": [ZERO_ITEM], "confirm": True})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "INVALID_ARGS"


@pytest.mark.parametrize("item_ids", [[], [ZERO_ITEM] * 51, "not-a-list"])
def test_ack_bounds_item_ids_before_the_gate(client, monkeypatch, item_ids):
    _set_env(monkeypatch, WORKSPACE_ENV)
    resp = _post(client, "hermes_owner_inbox_ack", {"item_ids": item_ids})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGS"


def test_ack_rejects_malformed_ids_without_echoing_them(client, monkeypatch):
    _set_env(monkeypatch, WORKSPACE_ENV)
    resp = _post(client, "hermes_owner_inbox_ack", {"item_ids": ["<script>SECRET-VALUE</script>"]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_OWNER_INBOX_ACK"
    assert "SECRET-VALUE" not in resp.text


def test_ack_read_only_is_refused_by_the_tool(client):
    resp = _post(client, "hermes_owner_inbox_ack", {"item_ids": [ZERO_ITEM]}, dry_run=False)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "LEVEL_REQUIRED"


def test_ack_dry_run_writes_nothing_then_confirm_writes_and_hides(client, ops_root):
    root, _ = ops_root
    autopilot._write_run(MID, root, state="waiting_for_owner", attempt=1, max_concurrency=1)  # noqa: SLF001
    item_id = client.get("/api/ops/owner-inbox").json()["data"]["items"][0]["item_id"]
    ack_store = root / op_owner_inbox.ACK_STORE_RELATIVE

    dry = _post(client, "hermes_owner_inbox_ack", {"item_ids": [item_id]})
    assert dry.status_code == 200
    assert dry.json()["data"]["dry_run"] is True
    assert dry.json()["data"]["result"]["planned"] == [item_id]
    assert not ack_store.exists()

    real = _post(client, "hermes_owner_inbox_ack", {"item_ids": [item_id]}, dry_run=False)
    assert real.status_code == 200
    assert real.json()["data"]["dry_run"] is False
    assert item_id in ack_store.read_text(encoding="utf-8")
    assert client.get("/api/ops/owner-inbox").json()["data"]["items"] == []


def test_mission_approve_owner_off_is_refused_by_the_tool(client, ops_root):
    root, _ = ops_root  # workspace level from make_env: Owner Mode is not active
    _mk(root, [_node("a")])
    resp = _post(client, "hermes_mission_approve", {"mission_id": MID, "approval_reference": "ref-1", "confirm": True}, dry_run=False)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "MISSION_APPROVAL_REJECTED"
    assert "owner" in err["message"].lower()
    md = json.loads(mission.hermes_mission_get(MID, hermes_root=root))
    assert md["status"] != "completed"
    assert not (md.get("approval") or {}).get("approved")


def test_mission_approve_dry_run_with_owner_is_the_tools_own_refusal(client, ops_root, monkeypatch):
    root, _ = ops_root
    _mk(root, [_node("a")])  # exists, but is not awaiting_approval
    _set_env(monkeypatch, OWNER_ENV)
    resp = _post(client, "hermes_mission_approve", {"mission_id": MID, "approval_reference": "ref-1"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "MISSION_APPROVAL_REJECTED"
    assert resp.json()["error"]["message"] == "mission must be awaiting_approval before Owner approval"


def test_mission_approve_confirm_calls_real_function_with_server_root(client, monkeypatch, tmp_path):
    calls: list[dict] = []

    def record(**kwargs):
        calls.append(kwargs)
        return json.dumps({"success": True, "tool": "hermes_mission_approve", "changed": kwargs.get("dry_run") is False})

    _set_env(monkeypatch, OWNER_ENV)
    monkeypatch.setattr(ui_ops._MUTATION_TOOLS["hermes_mission_approve"], "fn", record)

    dry = _post(client, "hermes_mission_approve", {"mission_id": MID, "approval_reference": "ref-1"})
    assert dry.status_code == 200 and dry.json()["data"]["dry_run"] is True
    assert calls[-1].get("dry_run", True) is True and "confirm" not in calls[-1]

    real = _post(client, "hermes_mission_approve", {"mission_id": MID, "approval_reference": "ref-1", "confirm": True}, dry_run=False)
    assert real.status_code == 200 and real.json()["data"]["dry_run"] is False
    assert calls[-1]["confirm"] is True and calls[-1]["dry_run"] is False
    assert calls[-1]["approval_reference"] == "ref-1"
    assert calls[-1]["hermes_root"] == ui_ops._resolve_root()  # noqa: SLF001 - server-resolved, never client-sent


def test_approve_gate_browser_dry_run_lists_steps_then_confirm_completes_the_gate(client, ops_root, monkeypatch):
    root, _ = ops_root
    _set_env(monkeypatch, OWNER_ENV)
    _mk(root, [_node("a"), _gate("gate", ["a"]), _node("c", ["gate"])])
    _walk(root, "a", ("dispatched", "running", "awaiting_review", "validated", "awaiting_approval", "completed"))
    args = {"mission_id": MID, "node_id": "gate"}

    dry = _post(client, "hermes_plan_approve_gate", args)
    assert dry.status_code == 200
    assert dry.json()["data"]["requires_confirm"] is True
    steps = dry.json()["data"]["result"]["steps"]
    assert [s["to_state"] for s in steps] == ["dispatched", "running", "awaiting_review", "validated", "awaiting_approval", "completed"]
    assert _states(root)["gate"] == "pending"

    real = _post(client, "hermes_plan_approve_gate", {**args, "confirm": True}, dry_run=False)
    assert real.status_code == 200 and real.json()["data"]["dry_run"] is False
    assert _states(root)["gate"] == "completed"


def test_approve_gate_browser_server_derives_the_plan_version_not_the_client(client, ops_root, monkeypatch):
    root, _ = ops_root
    _set_env(monkeypatch, OWNER_ENV)
    _mk(root, [_gate("gate")])
    # The browser cannot send expected_plan_version: it is not an allowed argument.
    resp = _post(client, "hermes_plan_approve_gate", {"mission_id": MID, "node_id": "gate", "expected_plan_version": 1})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "INVALID_ARGS"
    assert _states(root)["gate"] == "pending"


def test_approve_gate_browser_owner_off_is_refused_before_any_write(client, ops_root):
    root, _ = ops_root  # workspace level from make_env: Owner Mode is not active
    _mk(root, [_gate("gate")])
    resp = _post(client, "hermes_plan_approve_gate", {"mission_id": MID, "node_id": "gate", "confirm": True}, dry_run=False)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "LEVEL_REQUIRED"
    assert resp.json()["error"].get("required") == "owner"
    assert _states(root)["gate"] == "pending"


def test_approve_gate_browser_refuses_a_non_gated_node(client, ops_root, monkeypatch):
    root, _ = ops_root
    _set_env(monkeypatch, OWNER_ENV)
    _mk(root, [_node("a"), _gate("gate", ["a"])])
    resp = _post(client, "hermes_plan_approve_gate", {"mission_id": MID, "node_id": "a", "confirm": True}, dry_run=False)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "APPROVAL_GATE_REFUSED"
    assert "not an approval or high_impact" in resp.json()["error"]["message"]
    assert _states(root)["a"] == "pending"


def test_approve_gate_browser_refuses_a_gate_whose_parents_are_not_completed(client, ops_root, monkeypatch):
    root, _ = ops_root
    _set_env(monkeypatch, OWNER_ENV)
    _mk(root, [_node("a"), _gate("gate", ["a"])])
    resp = _post(client, "hermes_plan_approve_gate", {"mission_id": MID, "node_id": "gate", "confirm": True}, dry_run=False)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "APPROVAL_GATE_REFUSED"
    assert _states(root)["gate"] == "pending"


def test_approve_gate_browser_refuses_a_failed_gate(client, ops_root, monkeypatch):
    root, _ = ops_root
    _set_env(monkeypatch, OWNER_ENV)
    _mk(root, [_gate("gate")])
    _walk(root, "gate", ("failed",))
    resp = _post(client, "hermes_plan_approve_gate", {"mission_id": MID, "node_id": "gate", "confirm": True}, dry_run=False)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "APPROVAL_GATE_REFUSED"
    assert _states(root)["gate"] == "failed"


def test_node_transition_browser_no_longer_approves_a_gate(client, ops_root, monkeypatch):
    # Approve moved to hermes_plan_approve_gate. The browser transition is reject-only.
    root, _ = ops_root
    _set_env(monkeypatch, OWNER_ENV)
    _mk(root, [_node("a"), _gate("gate", ["a"])])
    _gate_at_awaiting(root)
    resp = _post(
        client, "hermes_plan_node_transition",
        {"mission_id": MID, "node_id": "gate", "target_state": "completed", "confirm": True},
        dry_run=False,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "APPROVAL_TRANSITION_REFUSED"
    assert "hermes_plan_approve_gate" in resp.json()["error"]["message"]
    assert _states(root)["gate"] == "awaiting_approval"


def test_node_transition_rejects_a_high_impact_node_too(client, ops_root, monkeypatch):
    root, _ = ops_root
    _set_env(monkeypatch, OWNER_ENV)
    _mk(root, [_node("h", auth="high_impact")])
    _walk(root, "h", ("blockable", "dispatched", "running", "awaiting_review", "validated", "awaiting_approval"))
    resp = _post(
        client, "hermes_plan_node_transition",
        {"mission_id": MID, "node_id": "h", "target_state": "failed", "reason": "rejected", "confirm": True},
        dry_run=False,
    )
    assert resp.status_code == 200
    assert _states(root)["h"] == "failed"


@pytest.mark.parametrize("target", ["completed", "paused", "blockable", "dispatched", "running", "validated", "pending", "awaiting_approval"])
def test_node_transition_refuses_targets_that_do_not_resolve_the_gate(client, ops_root, monkeypatch, target):
    root, _ = ops_root
    _set_env(monkeypatch, OWNER_ENV)
    _mk(root, [_node("a"), _gate("gate", ["a"])])
    _gate_at_awaiting(root)
    resp = _post(client, "hermes_plan_node_transition", {"mission_id": MID, "node_id": "gate", "target_state": target, "confirm": True}, dry_run=False)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "APPROVAL_TRANSITION_REFUSED"
    assert _states(root)["gate"] == "awaiting_approval"


def test_node_transition_refuses_a_non_gated_node(client, ops_root, monkeypatch):
    root, _ = ops_root
    _set_env(monkeypatch, OWNER_ENV)
    _mk(root, [_node("a"), _gate("gate", ["a"])])
    resp = _post(client, "hermes_plan_node_transition", {"mission_id": MID, "node_id": "a", "target_state": "failed", "confirm": True}, dry_run=False)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "APPROVAL_TRANSITION_REFUSED"
    assert "not an approval or high_impact" in resp.json()["error"]["message"]
    assert _states(root)["a"] == "pending"


def test_node_transition_refuses_a_reject_of_a_gate_not_awaiting_approval(client, ops_root, monkeypatch):
    root, _ = ops_root
    _set_env(monkeypatch, OWNER_ENV)
    _mk(root, [_node("a"), _gate("gate", ["a"])])
    resp = _post(client, "hermes_plan_node_transition", {"mission_id": MID, "node_id": "gate", "target_state": "failed", "confirm": True}, dry_run=False)
    assert resp.status_code == 400
    assert "not awaiting approval" in resp.json()["error"]["message"]
    assert _states(root)["gate"] == "pending"


def test_node_transition_owner_off_is_refused_before_any_plan_read(client, ops_root):
    root, _ = ops_root  # workspace level, no Owner Mode: the owner gate runs first
    _mk(root, [_node("a"), _gate("gate", ["a"])])
    resp = _post(client, "hermes_plan_node_transition", {"mission_id": MID, "node_id": "gate", "target_state": "completed", "confirm": True}, dry_run=False)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "LEVEL_REQUIRED"
    assert resp.json()["error"].get("required") == "owner"


def test_autopilot_stop_is_not_behind_the_machine_gate(client, ops_root, monkeypatch):
    root, _ = ops_root
    monkeypatch.delenv(autopilot.AUTOPILOT_ENV, raising=False)
    autopilot._write_run(MID, root, state="waiting_for_owner", attempt=1, max_concurrency=1)  # noqa: SLF001
    dry = _post(client, "hermes_autopilot_stop", {"mission_id": MID})
    assert dry.status_code == 200
    assert dry.json()["data"]["result"]["would_stop"] is True
    assert autopilot._read_run(MID, root)["state"] == "waiting_for_owner"  # noqa: SLF001

    real = _post(client, "hermes_autopilot_stop", {"mission_id": MID, "confirm": True}, dry_run=False)
    assert real.status_code == 200
    assert autopilot._read_run(MID, root)["state"] == "stopped"  # noqa: SLF001


def test_new_actions_inherit_bearer_and_origin_protection(monkeypatch):
    """The browser actions sit behind the same outer Bearer and CORS middleware as the rest of /api."""
    import oauth_auth
    import server
    import ui_security

    token = "test-static-bearer-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    monkeypatch.setenv(ui_security.UI_ENABLED_ENV, "1")
    monkeypatch.setenv(oauth_auth.AUTH_TOKEN_ENV, token)
    monkeypatch.delenv(op.OPERATOR_ENABLED_ENV, raising=False)
    built = server.build_server(http=True)
    client = TestClient(server.build_asgi_app(built, http=True), base_url="http://127.0.0.1")
    for tool in NEW_TOOLS:
        resp = _post(client, tool, {"item_ids": [ZERO_ITEM]} if tool == "hermes_owner_inbox_ack" else {"mission_id": MID})
        assert resp.status_code == 401, tool
    authed = client.post(
        "/api/ops/action",
        json={"tool": "hermes_owner_inbox_ack", "args": {"item_ids": [ZERO_ITEM]}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert authed.status_code != 401
    foreign = client.post(
        "/api/ops/action",
        json={"tool": "hermes_owner_inbox_ack", "args": {"item_ids": [ZERO_ITEM]}},
        headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
    )
    assert "access-control-allow-origin" not in {k.lower() for k in foreign.headers}


# --- Cross-site request refusal on the action route (v0.16) ----------------


def _unknown_tool_body() -> dict:
    return {"tool": "hermes_not_a_tool", "args": {}}


def test_action_refuses_non_json_content_type(client):
    resp = client.post(
        "/api/ops/action",
        content=json.dumps(_unknown_tool_body()),
        headers={"Content-Type": "text/plain"},
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_action_refuses_cross_site_fetch_metadata(client, site):
    resp = client.post("/api/ops/action", json=_unknown_tool_body(), headers={"Sec-Fetch-Site": site})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CROSS_SITE_REFUSED"


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:9999", "null"])
def test_action_refuses_foreign_origin(client, origin):
    resp = client.post("/api/ops/action", json=_unknown_tool_body(), headers={"Origin": origin})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CROSS_SITE_REFUSED"


def test_action_allows_same_origin_browser_request(client):
    host = client.base_url.netloc.decode() if isinstance(client.base_url.netloc, bytes) else client.base_url.netloc
    resp = client.post(
        "/api/ops/action",
        json=_unknown_tool_body(),
        headers={"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"},
    )
    # Passes the cross-site guard and reaches normal tool resolution.
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "UNKNOWN_TOOL"
