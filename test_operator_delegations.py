from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

import operator_delegations as delegations
import operator_mission_runtime as missions
import operator_policy as op
import operator_runners as runners


def _enable_workspace(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(workspace))


def _contract(workspace: Path, *, task_id: str = "delegation-task-001", backend: str = "pi_rpc") -> dict:
    return {
        "schema": "hermes.work-contract/v1",
        "task_id": task_id,
        "assigned_agent": "coder",
        "assigned_profile": "default",
        "objective": "UNIQUE_DELEGATION_PROMPT_MUST_NOT_PERSIST",
        "allowed_scope": {"workspaces": [str(workspace)], "profiles": ["default"]},
        "forbidden_actions": [],
        "expected_artifacts": [],
        "tests": [],
        "review_requirements": {},
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
        "inputs": [],
        "constraints": [],
        "authorization": {
            "class": "reversible_write",
            "approved": True,
            "approved_by": "owner",
            "approval_reference": "test",
        },
        "execution": {"backend": backend, "options": {}},
    }


def _mission(root: Path) -> str:
    payload = json.loads(
        missions.hermes_mission_create(
            json.dumps(
                {
                    "schema": missions.MISSION_SPEC_SCHEMA,
                    "mission_id": "msn-delegation-tests",
                    "title": "Delegation tests",
                    "objective": "Exercise unified delegation lifecycle.",
                }
            ),
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert payload["success"] is True
    return "msn-delegation-tests"


def test_list_before_dispatch_is_noncreating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    out = json.loads(delegations.hermes_delegation_list(hermes_root=root))
    assert out["success"] is True
    assert out["delegations"] == []
    assert not (root / "delegations" / "delegations.db").exists()


def test_dry_run_dispatch_does_not_create_lifecycle_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {"success": True, "dry_run": True, "changed": False, "backend": "pi_rpc", "plan": {"mode": "rpc"}}
        ),
    )
    out = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace)),
            delegation_id="dlg-dry-run",
            dry_run=True,
            hermes_root=root,
        )
    )
    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["changed"] is False
    assert not (root / "delegations" / "delegations.db").exists()


def test_dispatch_persists_prompt_free_lineage_and_mission_attachment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {"success": True, "dry_run": False, "changed": True, "backend": "pi_rpc", "task_id": "delegation-task-001", "state": "queued"}
        ),
    )
    out = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace)),
            mission_id=mission_id,
            delegation_id="dlg-persisted",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is True
    assert out["delegation"]["state"] == "queued"
    assert out["mission_linked"] is True
    db_bytes = (root / "delegations" / "delegations.db").read_bytes()
    assert b"UNIQUE_DELEGATION_PROMPT_MUST_NOT_PERSIST" not in db_bytes
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    assert any(item["kind"] == "delegation" and item["ref"] == "dlg-persisted" for item in mission["attachments"])


def test_ambiguous_dispatch_is_recorded_as_reconciling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {
                "success": False,
                "changed": True,
                "backend": "fabric",
                "code": "FABRIC_PEER_UNAVAILABLE",
                "submission_may_have_succeeded": True,
            }
        ),
    )
    contract = _contract(workspace, task_id="delegation-task-ambiguous", backend="fabric")
    out = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(contract),
            delegation_id="dlg-ambiguous",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is True
    assert out["delegation"]["state"] == "reconciling"


def test_reconcile_backend_success_uses_durable_validation_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "backend": "pi_rpc", "state": "queued"}),
    )
    contract = _contract(workspace, task_id="delegation-task-reconcile")
    created = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(contract),
            mission_id=mission_id,
            delegation_id="dlg-reconcile",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert created["success"] is True
    meta_path, _, _ = runners._job_paths("delegation-task-reconcile", root)
    runners._atomic_json(
        meta_path,
        {
            "schema_version": runners.SCHEMA_VERSION,
            "task_id": "delegation-task-reconcile",
            "backend": "pi_rpc",
            "state": "completed",
            "outcome": "completed",
            "created_at": "2026-08-21T00:00:00+00:00",
            "started_at": "2026-08-21T00:00:01+00:00",
            "ended_at": "2026-08-21T00:00:02+00:00",
            "error": "",
        },
    )
    reconciled = json.loads(
        delegations.hermes_delegation_reconcile("dlg-reconcile", apply=True, hermes_root=root)
    )
    assert reconciled["success"] is True
    assert reconciled["delegation"]["state"] == "succeeded"
    assert reconciled["delegation"]["validation_verdict"] == "SATISFIED"
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment = next(item for item in mission["attachments"] if item["ref"] == "dlg-reconcile")
    assert attachment["state"] == "succeeded"
    assert attachment["verified"]



def test_reconcile_satisfied_contract_promotes_verified_mission_attachment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "backend": "pi_rpc", "state": "queued"}),
    )
    contract = _contract(workspace, task_id="delegation-task-satisfied")
    assert json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(contract),
            mission_id=mission_id,
            delegation_id="dlg-satisfied",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )["success"] is True
    meta_path, _, _ = runners._job_paths("delegation-task-satisfied", root)
    runners._atomic_json(
        meta_path,
        {
            "schema_version": runners.SCHEMA_VERSION,
            "task_id": "delegation-task-satisfied",
            "backend": "pi_rpc",
            "state": "completed",
            "outcome": "completed",
            "created_at": "2026-08-21T00:00:00+00:00",
            "started_at": "2026-08-21T00:00:01+00:00",
            "ended_at": "2026-08-21T00:00:02+00:00",
            "error": "",
        },
    )
    reconciled = json.loads(
        delegations.hermes_delegation_reconcile(
            "dlg-satisfied",
            contract_json=json.dumps(contract),
            apply=True,
            hermes_root=root,
        )
    )
    assert reconciled["success"] is True
    assert reconciled["delegation"]["state"] == "succeeded"
    assert reconciled["delegation"]["validation_verdict"] == "SATISFIED"
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment = next(item for item in mission["attachments"] if item["ref"] == "dlg-satisfied")
    assert attachment["state"] == "succeeded"
    assert bool(attachment["verified"]) is True
    assert attachment["evidence_ref"].startswith("contract:")


def test_dispatch_immediate_backend_success_does_not_self_certify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "backend": "pi_rpc", "state": "completed"}),
    )
    out = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id="delegation-task-immediate")),
            delegation_id="dlg-immediate",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is True
    assert out["delegation"]["state"] == "reconciling"


def test_dispatch_post_backend_persistence_failure_marks_submission_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    called = False
    def dispatch(*args, **kwargs):
        nonlocal called
        called = True
        return json.dumps({"success": True, "changed": True, "backend": "pi_rpc", "state": "queued"})
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        dispatch,
    )
    real_connect = delegations._connect

    def failing_connect(path, write=False):
        if write:
            raise sqlite3.OperationalError("simulated delegation store failure")
        return real_connect(path, write=write)

    monkeypatch.setattr(delegations, "_connect", failing_connect)
    out = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id="delegation-task-postdispatch")),
            delegation_id="dlg-postdispatch",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is False
    assert out["code"] == "DELEGATION_DISPATCH_REJECTED"
    assert out.get("submission_may_have_succeeded") is not True
    assert called is False


def test_dispatch_rejects_nonexistent_mission_even_when_store_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    called = False

    def dispatch(*args, **kwargs):
        nonlocal called
        called = True
        return json.dumps({"success": True})

    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", dispatch)
    out = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id="delegation-task-ghost")),
            mission_id="msn-does-not-exist",
            delegation_id="dlg-ghost",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is False
    assert out["code"] == "DELEGATION_DISPATCH_REJECTED"
    assert called is False

def test_reconcile_contract_lineage_mismatch_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "backend": "pi_rpc", "state": "queued"}),
    )
    contract = _contract(workspace, task_id="delegation-task-lineage")
    assert json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(contract),
            delegation_id="dlg-lineage",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )["success"] is True
    other = _contract(workspace, task_id="delegation-task-other")
    out = json.loads(
        delegations.hermes_delegation_reconcile(
            "dlg-lineage",
            contract_json=json.dumps(other),
            apply=False,
            hermes_root=root,
        )
    )
    assert out["success"] is False
    assert out["code"] == "DELEGATION_RECONCILE_FAILED"


def test_cancel_routes_backend_and_terminalizes_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "backend": "pi_rpc", "state": "running"}),
    )
    assert json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id="delegation-task-cancel")),
            delegation_id="dlg-cancel",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )["success"] is True
    calls: list[tuple[str, str, bool, bool]] = []

    def cancel(task_id, backend="", confirm=False, dry_run=True, hermes_root=None):
        calls.append((task_id, backend, confirm, dry_run))
        return json.dumps({"success": True, "changed": True, "backend": backend, "task_id": task_id, "state": "cancelled"})

    monkeypatch.setattr(delegations.runners, "hermes_runner_cancel", cancel)
    out = json.loads(
        delegations.hermes_delegation_cancel(
            "dlg-cancel",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is True
    assert out["delegation"]["state"] == "cancelled"
    assert calls == [("delegation-task-cancel", "pi_rpc", True, False)]


def test_cancel_backend_completed_self_report_stays_reconciling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "backend": "pi_rpc", "state": "running"}),
    )
    assert json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id="delegation-task-cancel-completed")),
            delegation_id="dlg-cancel-completed",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )["success"] is True
    monkeypatch.setattr(
        delegations.runners,
        "hermes_runner_cancel",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": False, "backend": "pi_rpc", "state": "completed"}),
    )
    out = json.loads(
        delegations.hermes_delegation_cancel(
            "dlg-cancel-completed",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is True
    row = out["delegation"]
    assert row["state"] == "reconciling"
    assert row["validation_verdict"] == ""
    assert row["terminal_at"] is None
    assert row["cancel_requested"] is True
    fetched = json.loads(delegations.hermes_delegation_get("dlg-cancel-completed", hermes_root=root))
    assert fetched["delegation"]["events"][0]["event_type"] == "delegation.cancel_requested"


def test_get_returns_bounded_event_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "backend": "pi_rpc", "state": "queued"}),
    )
    assert json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id="delegation-task-get")),
            delegation_id="dlg-get",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )["success"] is True
    out = json.loads(delegations.hermes_delegation_get("dlg-get", hermes_root=root))
    assert out["success"] is True
    assert out["delegation"]["events"][0]["event_type"] == "delegation.dispatched"


def test_concurrent_exact_retry_invokes_backend_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def dispatch(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(5)
        return json.dumps({"success": True, "changed": True, "state": "queued"})

    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", dispatch)
    args = (json.dumps(_contract(workspace, task_id="delegation-concurrent")),)
    result: list[dict] = []
    thread = threading.Thread(target=lambda: result.append(json.loads(delegations.hermes_delegation_dispatch(*args, delegation_id="dlg-concurrent", confirm=True, dry_run=False, hermes_root=root))))
    thread.start()
    assert entered.wait(5)
    retry = json.loads(delegations.hermes_delegation_dispatch(*args, delegation_id="dlg-concurrent", confirm=True, dry_run=False, hermes_root=root))
    release.set()
    thread.join(5)
    assert calls == 1
    assert retry["success"] is True and retry["idempotent"] is True
    assert retry["delegation"]["dispatch_phase"] == "invoking"
    assert result[0]["delegation"]["dispatch_phase"] == "dispatched"


def test_collision_requires_exact_delegation_and_task_lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "queued"}))
    first = _contract(workspace, task_id="delegation-collision")
    assert json.loads(delegations.hermes_delegation_dispatch(json.dumps(first), delegation_id="dlg-collision", confirm=True, dry_run=False, hermes_root=root))["success"]
    same_task_other_id = json.loads(delegations.hermes_delegation_dispatch(json.dumps(first), delegation_id="dlg-other", confirm=True, dry_run=False, hermes_root=root))
    other_task_same_id = json.loads(delegations.hermes_delegation_dispatch(json.dumps(_contract(workspace, task_id="delegation-other")), delegation_id="dlg-collision", confirm=True, dry_run=False, hermes_root=root))
    assert not same_task_other_id["success"]
    assert not other_task_same_id["success"]


def test_definite_rejection_resets_reserved_and_retry_dispatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    responses = iter((
        {"success": False, "changed": False, "code": "REJECTED"},
        {"success": True, "changed": True, "state": "queued"},
    ))
    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", lambda *a, **k: json.dumps(next(responses)))
    contract = json.dumps(_contract(workspace, task_id="delegation-retry"))
    first = json.loads(delegations.hermes_delegation_dispatch(contract, delegation_id="dlg-retry", confirm=True, dry_run=False, hermes_root=root))
    reserved = json.loads(delegations.hermes_delegation_get("dlg-retry", hermes_root=root))["delegation"]
    second = json.loads(delegations.hermes_delegation_dispatch(contract, delegation_id="dlg-retry", confirm=True, dry_run=False, hermes_root=root))
    assert not first["success"] and reserved["state"] == "reserved" and reserved["dispatch_phase"] == "reserved"
    assert second["success"] and second["delegation"]["dispatch_phase"] == "dispatched"


def test_missing_or_corrupt_manifest_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "queued"}))
    assert json.loads(delegations.hermes_delegation_dispatch(json.dumps(_contract(workspace, task_id="delegation-manifest")), delegation_id="dlg-manifest", confirm=True, dry_run=False, hermes_root=root))["success"]
    with delegations._connect(delegations._db_path(root), write=True) as db:
        db.execute("UPDATE delegation_validation_manifests SET manifest_json='{}' WHERE delegation_id='dlg-manifest'")
        db.commit()
    out = json.loads(delegations.hermes_delegation_reconcile("dlg-manifest", apply=False, hermes_root=root))
    assert not out["success"] and out["code"] == "DELEGATION_RECONCILE_FAILED"


def test_secret_like_manifest_value_rejected_before_backend_or_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    contract = _contract(workspace, task_id="delegation-secret")
    contract["authorization"]["approval_reference"] = "Bearer abcdefghijklmnopqrstuvwxyz"
    called = False
    def dispatch(*a, **k):
        nonlocal called
        called = True
        return json.dumps({"success": True})
    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", dispatch)
    out = json.loads(delegations.hermes_delegation_dispatch(json.dumps(contract), delegation_id="dlg-secret", confirm=True, dry_run=False, hermes_root=root))
    assert not out["success"] and not called
    assert not (root / "delegations" / "delegations.db").exists()


def test_in_place_database_migration_adds_dispatch_phase_and_manifest_table(tmp_path: Path):
    path = tmp_path / "delegations.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE delegations (delegation_id TEXT PRIMARY KEY, schema TEXT NOT NULL, mission_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL UNIQUE, contract_sha256 TEXT NOT NULL, backend TEXT NOT NULL, state TEXT NOT NULL, backend_state TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '', backend_ref_json TEXT NOT NULL DEFAULT '{}', validation_verdict TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, dispatched_at TEXT NOT NULL, updated_at TEXT NOT NULL, terminal_at TEXT)")
    db.close()
    with delegations._connect(path, write=True) as migrated:
        delegations._init(migrated)
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(delegations)")}
        tables = {row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "dispatch_phase" in columns
    assert "delegation_validation_manifests" in tables
