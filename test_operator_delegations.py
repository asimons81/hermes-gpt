from __future__ import annotations

import json
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


def test_reconcile_uses_observed_runner_state_and_updates_mission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment = next(item for item in mission["attachments"] if item["ref"] == "dlg-reconcile")
    assert attachment["state"] == "succeeded"


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
