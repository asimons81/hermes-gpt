"""Tests for v0.7 S2: restart reconciliation + idempotent advance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_policy as op
import operator_recovery as rec
import operator_swarm as op_swarm


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    (root / "swarm-workflows").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def clean_env(monkeypatch):
    for name in [
        op.OPERATOR_ENABLED_ENV,
        op.OPERATOR_LEVEL_ENV,
        op.OPERATOR_APPLY_MODE_ENV,
        op.OPERATOR_ALLOWED_PROFILES_ENV,
        op.OPERATOR_ALLOWED_PATHS_ENV,
        op.OPERATOR_DENIED_PATHS_ENV,
        op.OWNER_ACK_ENV,
    ]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def audit_override(tmp_path):
    log = tmp_path / "audit.jsonl"
    op.set_audit_log_override(log)
    yield log
    op.set_audit_log_override(None)


def _workflow_record(workflow_id: str = "sw-abc", stage_status: str = "running") -> dict:
    now = "2026-08-15T00:00:00+00:00"
    return {
        "schema": op_swarm.WORKFLOW_SCHEMA,
        "schema_version": op_swarm.SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "title": "Test",
        "workspace": ".",
        "project": {},
        "max_parallel": 1,
        "board_cap": 1,
        "max_stages": 4,
        "status": op_swarm.WORKFLOW_STATUS_RUNNING,
        "definition": {"stages": []},
        "stages": [
            {
                "id": "s1",
                "kind": "single",
                "owner": "dev",
                "parents": [],
                "status": stage_status,
                "task_id": f"{workflow_id}-s1",
                "contract_sha256": "",
                "verdict": "",
                "rework_count": 0,
                "handoffs": [],
                "worktree_plan": None,
                "started_at": now,
                "ended_at": None,
                "blocked_reason": "",
            }
        ],
        "approval": {"approved": False, "approved_by": "", "approval_reference": "", "approved_at": ""},
        "retention_note": "30d after terminal",
        "created_at": now,
        "updated_at": now,
    }


def _write_workflow(hermes_root: Path, record: dict) -> None:
    op_swarm._save_workflow(hermes_root, record)


def test_reconcile_dry_run_marks_nothing_and_reports_interrupted(hermes_root, clean_env, audit_override):
    _write_workflow(hermes_root, _workflow_record("sw-1"))

    out = rec.hermes_operator_reconcile(apply=False, hermes_root=hermes_root)
    parsed = json.loads(out)

    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    assert parsed["swarm"]["interrupted_count"] == 1
    assert parsed["swarm"]["applied"] is False
    # Dry run must not mutate the store.
    record = op_swarm._load_workflow(hermes_root, "sw-1")
    assert record["stages"][0]["status"] == op_swarm.STAGE_STATUS_RUNNING


def test_reconcile_apply_marks_interrupted_stages_blocked(hermes_root, clean_env, audit_override, monkeypatch):
    _write_workflow(hermes_root, _workflow_record("sw-1"))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")

    out = rec.hermes_operator_reconcile(apply=True, hermes_root=hermes_root)
    parsed = json.loads(out)

    assert parsed["success"] is True
    assert parsed["applied"] is True
    assert parsed["swarm"]["interrupted_count"] == 1
    record = op_swarm._load_workflow(hermes_root, "sw-1")
    assert record["stages"][0]["status"] == op_swarm.STAGE_STATUS_BLOCKED
    assert record["stages"][0]["blocked_reason"] == rec.INTERRUPTED_REASON
    assert record["status"] == op_swarm.WORKFLOW_STATUS_BLOCKED


def test_reconcile_apply_requires_policy(hermes_root, clean_env, audit_override):
    _write_workflow(hermes_root, _workflow_record("sw-1"))

    out = rec.hermes_operator_reconcile(apply=True, hermes_root=hermes_root)
    parsed = json.loads(out)

    assert parsed["success"] is False
    assert parsed["code"] == "PERMISSION_DENIED"
    # No mutation without the gate.
    record = op_swarm._load_workflow(hermes_root, "sw-1")
    assert record["stages"][0]["status"] == op_swarm.STAGE_STATUS_RUNNING


def test_reconcile_ignores_done_and_blocked_stages(hermes_root, clean_env, audit_override, monkeypatch):
    _write_workflow(hermes_root, _workflow_record("sw-done", stage_status=op_swarm.STAGE_STATUS_DONE))
    _write_workflow(hermes_root, _workflow_record("sw-blocked", stage_status=op_swarm.STAGE_STATUS_BLOCKED))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")

    out = rec.hermes_operator_reconcile(apply=True, hermes_root=hermes_root)
    parsed = json.loads(out)

    assert parsed["success"] is True
    assert parsed["swarm"]["interrupted_count"] == 0
    assert parsed["swarm"]["records_changed"] == 0


def test_reconcile_never_auto_advances(hermes_root, clean_env, audit_override, monkeypatch):
    """Fail-closed rule: reconcile must not advance or dispatch anything."""
    _write_workflow(hermes_root, _workflow_record("sw-1"))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")

    rec.hermes_operator_reconcile(apply=True, hermes_root=hermes_root)

    record = op_swarm._load_workflow(hermes_root, "sw-1")
    assert record["stages"][0]["status"] == op_swarm.STAGE_STATUS_BLOCKED
    assert record["stages"][0]["verdict"] == ""  # no verdict assigned, no advance
    assert record["stages"][0]["handoffs"] == []


def test_reconcile_audits_every_call(hermes_root, clean_env, audit_override, monkeypatch):
    _write_workflow(hermes_root, _workflow_record("sw-1"))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")

    rec.hermes_operator_reconcile(apply=True, hermes_root=hermes_root)

    records = [json.loads(line) for line in audit_override.read_text(encoding="utf-8").splitlines() if line.strip()]
    reconcile_records = [r for r in records if r["tool"] == rec.TOOL_NAME]
    assert reconcile_records
    assert reconcile_records[-1]["success"] is True
    assert reconcile_records[-1]["changed"] is True


def test_stage_advance_is_idempotent_for_done_stage(hermes_root, clean_env, audit_override, monkeypatch):
    """Re-advancing a validated/done stage is a no-op, not an error."""
    import test_operator_swarm as ts

    wf = ts._workflow_ready_for_dispatch(hermes_root / "ws", workflow_id="sw-ido-1")
    wf["stages"] = [wf["stages"][0]]  # single-stage DAG for a clean advance
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")

    created = json.loads(
        op_swarm.hermes_swarm_workflow_create(
            json.dumps(wf), confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )
    assert created["success"] is True
    workflow_id = created["workflow_id"]

    # Force the stage to done so re-advance is a no-op.
    rec_workflow = op_swarm._load_workflow(hermes_root, workflow_id)
    assert rec_workflow is not None
    rec_workflow["stages"][0]["status"] = op_swarm.STAGE_STATUS_DONE
    rec_workflow["stages"][0]["verdict"] = "SATISFIED"
    _write_workflow(hermes_root, rec_workflow)

    out = op_swarm.hermes_swarm_stage_advance(
        workflow_id=workflow_id,
        stage_id=wf["stages"][0]["id"],
        confirm=True,
        dry_run=False,
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)

    assert parsed["success"] is True
    assert parsed.get("idempotent") is True
    assert parsed["changed"] is False
    assert parsed["stage_status"] == op_swarm.STAGE_STATUS_DONE
