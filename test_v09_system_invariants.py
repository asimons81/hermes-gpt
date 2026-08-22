from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_delegations as delegations
import operator_live_events as live_events
import operator_mission_runtime as missions
import operator_policy as op
import operator_runners as runners


def _enable_workspace(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(workspace))
    monkeypatch.delenv(op.OWNER_ACTIVE_ENV, raising=False)
    monkeypatch.delenv(op.OWNER_ACK_ENV, raising=False)


def _owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)


def _mission(root: Path, mission_id: str = "msn-system-invariants") -> str:
    created = json.loads(
        missions.hermes_mission_create(
            json.dumps(
                {
                    "schema": missions.MISSION_SPEC_SCHEMA,
                    "mission_id": mission_id,
                    "title": "v0.9 system invariant acceptance",
                    "objective": "Prove Mission and Delegation authority remain synchronized.",
                    "acceptance_criteria": ["delegated work stays durably parented"],
                }
            ),
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert created["success"] is True
    return mission_id


def _contract(
    workspace: Path,
    *,
    task_id: str = "system-invariant-task",
    backend: str = "pi_rpc",
) -> dict:
    return {
        "schema": "hermes.work-contract/v1",
        "task_id": task_id,
        "assigned_agent": "coder",
        "assigned_profile": "default",
        "objective": "Exercise v0.9 cross-slice invariants.",
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
            "approval_reference": "system-invariant-test",
        },
        "execution": {"backend": backend, "options": {}},
    }


def _dispatch_running(
    workspace: Path,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mission_id: str,
    delegation_id: str,
    task_id: str,
) -> dict:
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {
                "success": True,
                "changed": True,
                "backend": "pi_rpc",
                "task_id": task_id,
                "state": "running",
            }
        ),
    )
    result = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id=task_id)),
            mission_id=mission_id,
            delegation_id=delegation_id,
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    _observe_job(root, task_id, "running")
    return result


def _observe_job(root: Path, task_id: str, state: str) -> None:
    meta_path, _, _ = runners._job_paths(task_id, root)
    terminal = state in {"completed", "cancelled", "failed"}
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": "pi_rpc",
        "state": state,
        "outcome": state,
        "created_at": "2026-08-22T00:00:00+00:00",
        "started_at": "2026-08-22T00:00:01+00:00",
        "ended_at": "2026-08-22T00:00:02+00:00" if terminal else "",
        "error": "",
    })


def test_dispatch_reserves_mission_child_before_backend_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    delegation_id = "dlg-reserved-first"
    task_id = "system-reserved-first"
    backend_saw_reservation = False

    def dispatch(*args, **kwargs):
        nonlocal backend_saw_reservation
        mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
        attachment = next(item for item in mission["attachments"] if item["ref"] == delegation_id)
        backend_saw_reservation = attachment["state"] == "pending"
        assert attachment["evidence_ref"].startswith("contract:")
        return json.dumps(
            {
                "success": True,
                "changed": True,
                "backend": "pi_rpc",
                "task_id": task_id,
                "state": "running",
            }
        )

    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", dispatch)
    out = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id=task_id)),
            mission_id=mission_id,
            delegation_id=delegation_id,
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is True
    assert out["mission_linked"] is True
    assert backend_saw_reservation is True
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment = next(item for item in mission["attachments"] if item["ref"] == delegation_id)
    assert attachment["state"] == "running"


def test_terminal_mission_rejects_reservation_before_backend_is_called(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    cancelled = json.loads(
        missions.hermes_mission_transition(
            mission_id,
            "cancelled",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert cancelled["success"] is True
    called = False

    def dispatch(*args, **kwargs):
        nonlocal called
        called = True
        return json.dumps({"success": True, "changed": True, "state": "running"})

    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", dispatch)
    out = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id="system-terminal-parent")),
            mission_id=mission_id,
            delegation_id="dlg-terminal-parent",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is False
    assert out["code"] == "DELEGATION_DISPATCH_REJECTED"
    assert called is False
    reserved = json.loads(delegations.hermes_delegation_get("dlg-terminal-parent", hermes_root=root))["delegation"]
    assert reserved["state"] == "reserved"
    assert reserved["dispatch_phase"] == "reserved"


def test_public_mission_attach_cannot_forge_delegation_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    created = _dispatch_running(
        workspace,
        root,
        monkeypatch,
        mission_id=mission_id,
        delegation_id="dlg-running-child",
        task_id="system-running-child",
    )
    assert created["success"] is True

    forged = json.loads(
        missions.hermes_mission_attach(
            mission_id,
            "delegation",
            "dlg-running-child",
            state="cancelled",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert forged["success"] is False
    assert forged["code"] == "MISSION_ATTACH_REJECTED"
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment = next(item for item in mission["attachments"] if item["ref"] == "dlg-running-child")
    assert attachment["state"] == "running"


def test_mission_reconcile_reobserves_authoritative_delegation_after_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    created = _dispatch_running(
        workspace,
        root,
        monkeypatch,
        mission_id=mission_id,
        delegation_id="dlg-tamper-child",
        task_id="system-tamper-child",
    )
    assert created["success"] is True

    with missions._connect(missions._db_path(root), write=True) as db:
        db.execute(
            "UPDATE attachments SET state='cancelled',verified=0 WHERE mission_id=? AND kind='delegation' AND ref=?",
            (mission_id, "dlg-tamper-child"),
        )
        db.commit()

    reconciled = json.loads(
        missions.hermes_mission_reconcile(
            mission_id,
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert reconciled["success"] is True
    assert reconciled["status"] == "running"
    observed = next(item for item in reconciled["observed"] if item["ref"] == "dlg-tamper-child")
    assert observed["state"] == "running"
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment = next(item for item in mission["attachments"] if item["ref"] == "dlg-tamper-child")
    assert attachment["state"] == "running"


def test_dispatch_bridge_failure_is_hard_error_and_parent_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {"success": True, "changed": True, "backend": "pi_rpc", "state": "running"}
        ),
    )
    monkeypatch.setattr(missions, "record_attachment_state", lambda *args, **kwargs: False)

    out = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id="system-dispatch-sync-fail")),
            mission_id=mission_id,
            delegation_id="dlg-dispatch-sync-fail",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is False
    assert out["code"] == "DELEGATION_MISSION_SYNC_FAILED"
    assert out["changed"] is True
    assert out["submission_may_have_succeeded"] is True
    delegation = json.loads(delegations.hermes_delegation_get("dlg-dispatch-sync-fail", hermes_root=root))
    assert delegation["delegation"]["state"] == "running"
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment = next(item for item in mission["attachments"] if item["ref"] == "dlg-dispatch-sync-fail")
    assert attachment["state"] == "pending"


def test_reconcile_bridge_failure_surfaces_then_mission_can_reobserve_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    contract = _contract(workspace, task_id="system-reconcile-sync-fail")
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {"success": True, "changed": True, "backend": "pi_rpc", "state": "queued"}
        ),
    )
    created = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(contract),
            mission_id=mission_id,
            delegation_id="dlg-reconcile-sync-fail",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert created["success"] is True
    _observe_job(root, "system-reconcile-sync-fail", "completed")
    monkeypatch.setattr(missions, "record_attachment_state", lambda *args, **kwargs: False)

    out = json.loads(
        delegations.hermes_delegation_reconcile(
            "dlg-reconcile-sync-fail",
            contract_json=json.dumps(contract),
            apply=True,
            hermes_root=root,
        )
    )
    assert out["success"] is False
    assert out["code"] == "DELEGATION_MISSION_SYNC_FAILED"
    assert out["delegation"]["state"] == "succeeded"
    mission_before = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment_before = next(
        item for item in mission_before["attachments"] if item["ref"] == "dlg-reconcile-sync-fail"
    )
    assert attachment_before["state"] == "pending"

    recovered = json.loads(
        missions.hermes_mission_reconcile(
            mission_id,
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert recovered["success"] is True
    assert recovered["status"] == "awaiting_approval"
    observed = next(item for item in recovered["observed"] if item["ref"] == "dlg-reconcile-sync-fail")
    assert observed["state"] == "succeeded"
    assert observed["verified"] is True


def test_cancel_bridge_failure_surfaces_and_mission_reconcile_reads_cancelled_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    created = _dispatch_running(
        workspace,
        root,
        monkeypatch,
        mission_id=mission_id,
        delegation_id="dlg-cancel-sync-fail",
        task_id="system-cancel-sync-fail",
    )
    assert created["success"] is True
    monkeypatch.setattr(
        delegations.runners,
        "hermes_runner_cancel",
        lambda *args, **kwargs: json.dumps(
            {"success": True, "changed": True, "backend": "pi_rpc", "state": "cancelled"}
        ),
    )
    _observe_job(root, "system-cancel-sync-fail", "cancelled")
    monkeypatch.setattr(missions, "record_attachment_state", lambda *args, **kwargs: False)

    out = json.loads(
        delegations.hermes_delegation_cancel(
            "dlg-cancel-sync-fail",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert out["success"] is False
    assert out["code"] == "DELEGATION_MISSION_SYNC_FAILED"
    assert out["delegation"]["state"] == "cancelled"
    mission_before = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment_before = next(item for item in mission_before["attachments"] if item["ref"] == "dlg-cancel-sync-fail")
    assert attachment_before["state"] == "running"

    reconciled = json.loads(
        missions.hermes_mission_reconcile(
            mission_id,
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    observed = next(item for item in reconciled["observed"] if item["ref"] == "dlg-cancel-sync-fail")
    assert observed["state"] == "cancelled"
    mission_after = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment_after = next(item for item in mission_after["attachments"] if item["ref"] == "dlg-cancel-sync-fail")
    assert attachment_after["state"] == "cancelled"


def test_owner_approval_reobserves_delegation_lifecycle_not_cached_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    contract = _contract(workspace, task_id="system-approval-reobserve")
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {"success": True, "changed": True, "backend": "pi_rpc", "state": "queued"}
        ),
    )
    created = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(contract),
            mission_id=mission_id,
            delegation_id="dlg-approval-reobserve",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert created["success"] is True
    _observe_job(root, "system-approval-reobserve", "completed")
    succeeded = json.loads(
        delegations.hermes_delegation_reconcile(
            "dlg-approval-reobserve",
            contract_json=json.dumps(contract),
            apply=True,
            hermes_root=root,
        )
    )
    assert succeeded["success"] is True
    assert succeeded["delegation"]["state"] == "succeeded"
    mission_ready = json.loads(
        missions.hermes_mission_reconcile(
            mission_id,
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert mission_ready["status"] == "awaiting_approval"

    _observe_job(root, "system-approval-reobserve", "running")

    _owner(monkeypatch)
    denied = json.loads(
        missions.hermes_mission_approve(
            mission_id,
            "approval:stale-delegation",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert denied["success"] is False
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    assert mission["status"] == "awaiting_approval"
    assert not mission["approval"].get("approved")


def test_live_event_cursor_is_clamped_to_sqlite_integer_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    live_events.publish_event(
        topic="mission",
        kind="mission.test",
        subject_type="mission",
        subject_id="msn-cursor-test",
        mission_id="msn-cursor-test",
        source="test-v09-system-invariants",
        payload={"state": "running"},
        hermes_root=root,
    )
    huge = 10**100
    out = json.loads(live_events.hermes_live_events_since(cursor=huge, hermes_root=root))
    assert out["success"] is True
    assert out["cursor"] == live_events.MAX_CURSOR
    assert out["next_cursor"] == live_events.MAX_CURSOR
    assert out["events"] == []
    assert live_events._bounded_cursor(-100) == 0
    assert live_events._bounded_cursor(huge) == live_events.MAX_CURSOR
