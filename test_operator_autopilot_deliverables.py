"""Deliverables and recovery must survive independent reconciliation."""
from pathlib import Path

import pytest

import operator_autopilot as ap
import operator_delegations as deleg
import operator_mission_plan as plan
import operator_mission_runtime as mission
from test_operator_autopilot_advance import _observe
from test_operator_autopilot_scheduler import (
    MID,
    _j,
    _mk,
    _node,
    _states,
    _tick,
    make_env,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def test_missing_or_empty_deliverable_cannot_complete_node(env):
    root, backend = env
    node = _node("report")
    node["expected_artifacts"] = ["report.md"]
    _mk(root, [node])
    _tick(root)
    contract = backend.calls[0]
    assert contract["completion_criteria"]["artifacts_present"] is True
    assert contract["expected_artifacts"] == [{"path": "report.md", "must_exist": True, "min_bytes": 1}]
    _observe(root, contract["task_id"], state="completed", artifacts={})
    assert _tick(root)["completed"] == []
    assert _states(root)["report"] != "completed"
    workspace = Path(contract["allowed_scope"]["workspaces"][0])
    (workspace / "report.md").write_text("")
    assert _tick(root)["completed"] == []
    (workspace / "report.md").write_text("Observed deliverable")
    assert _tick(root)["completed"] == ["report"]
    assert _j(mission.hermes_mission_get(MID, hermes_root=root))["status"] == "awaiting_approval"


def test_retry_contract_cannot_reuse_previous_attempt_artifacts(env, monkeypatch):
    root, backend = env
    monkeypatch.setattr(ap, "RETRY_BACKOFF_BASE_SECONDS", 0)
    _mk(root, [_node("a")])
    _tick(root)
    first = backend.calls[0]
    workspace = Path(first["allowed_scope"]["workspaces"][0])
    workspace.mkdir(parents=True)
    (workspace / "work-contract.json").write_text("old attempt")
    _observe(root, first["task_id"], state="failed", error="timeout")
    _tick(root)
    second = backend.calls[1]
    assert first["allowed_scope"]["workspaces"] != second["allowed_scope"]["workspaces"]
    _observe(root, second["task_id"], state="completed", artifacts={})
    assert _tick(root)["completed"] == []


@pytest.mark.parametrize("stage", ["before_observation", "during_backoff"])
def test_independent_reconcile_does_not_terminally_fail_recoverable_work(env, monkeypatch, stage):
    root, backend = env
    monkeypatch.setattr(ap, "RETRY_BACKOFF_BASE_SECONDS", 600)
    _mk(root, [_node("a")])
    _tick(root)
    _observe(root, backend.calls[0]["task_id"], state="failed", error="timeout")
    if stage == "during_backoff":
        _tick(root)
    for dry_run in (True, False):
        out = _j(mission.hermes_mission_reconcile(MID, confirm=True, dry_run=dry_run, hermes_root=root))
        assert out["success"] and out["status"] == "running"
        assert out["recovery_pending"]
        assert out["observed"][0]["state"] == "failed"
    _tick(root)
    recovery = ap._load_recovery(ap._read_run(MID, root))
    recovery["nodes"]["a"]["not_before"] = 0
    ap._save_recovery(MID, root, recovery)
    assert _tick(root)["dispatched"] == ["a"]
    _observe(root, backend.calls[1]["task_id"], state="completed")
    assert _tick(root)["mission_status"] == "awaiting_approval"


@pytest.mark.parametrize("limit", ["stopped", "expired", "attempts", "unknown", "forged"])
def test_recovery_deferral_fails_closed(env, limit):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    _observe(root, backend.calls[0]["task_id"], state="failed", error="worker crashed" if limit == "unknown" else "timeout")
    if limit == "stopped":
        ap._write_run(MID, root, state="stopped")
    elif limit == "expired":
        ap._write_run(MID, root, started_at="2000-01-01T00:00:00+00:00")
    elif limit == "attempts":
        ap._write_run(MID, root, max_attempts_per_node=1)
    elif limit == "forged":
        # A forged recovery entry cannot override the node's terminal failure.
        _j(plan.hermes_plan_node_transition(MID, "a", "failed", confirm=True, dry_run=False, hermes_root=root))
        recovery = ap._empty_recovery()
        listed = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"]
        recovery["pending_supersede"]["a"] = listed[0]["delegation_id"]
        ap._save_recovery(MID, root, recovery)
    out = _j(mission.hermes_mission_reconcile(MID, confirm=True, dry_run=False, hermes_root=root))
    assert out["status"] == "failed" and not out.get("recovery_pending")


@pytest.mark.parametrize("dry_run", [True, False])
def test_plan_replacement_refuses_in_flight_work_and_preserves_every_record(env, dry_run):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    old = _j(plan.hermes_plan_get(MID, hermes_root=root))
    out = _j(plan.hermes_plan_create(MID, confirm=True, dry_run=dry_run, hermes_root=root))
    assert out["code"] == "PLAN_IN_FLIGHT"
    assert _j(plan.hermes_plan_get(MID, hermes_root=root)) == old
    assert len(backend.calls) == 1
