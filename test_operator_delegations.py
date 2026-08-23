from __future__ import annotations

import hashlib
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


def _successful_mission_delegation(
    root: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mission_id: str,
    delegation_id: str,
    task_id: str,
    final_approval_required: bool,
) -> str:
    spec = {
        "schema": missions.MISSION_SPEC_SCHEMA,
        "mission_id": mission_id,
        "title": "Completion cancellation race",
        "objective": "Exercise Mission/delegation authority linearization.",
        "final_approval_required": final_approval_required,
    }
    assert json.loads(missions.hermes_mission_create(
        json.dumps(spec), confirm=True, dry_run=False, hermes_root=root,
    ))["success"]
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "running"}),
    )
    contract = _contract(workspace, task_id=task_id)
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(contract), mission_id=mission_id, delegation_id=delegation_id,
        confirm=True, dry_run=False, hermes_root=root,
    ))["success"]
    meta_path, _, _ = runners._job_paths(task_id, root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": "pi_rpc",
        "state": "completed",
        "outcome": "completed",
        "created_at": "2026-08-22T00:00:00+00:00",
        "started_at": "2026-08-22T00:00:01+00:00",
        "ended_at": "2026-08-22T00:00:02+00:00",
        "error": "",
    })
    observed = json.loads(delegations.hermes_delegation_reconcile(
        delegation_id, apply=False, hermes_root=root,
    ))
    assert observed["delegation"]["state"] == "succeeded"
    return f"contract:{observed['delegation']['contract_sha256']}"


def test_owner_approval_fails_closed_against_inflight_confirmed_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = "msn-approval-cancel-race"
    delegation_id = "dlg-approval-cancel-race"
    evidence_ref = _successful_mission_delegation(
        root, workspace, monkeypatch, mission_id=mission_id,
        delegation_id=delegation_id, task_id="approval-cancel-race-task",
        final_approval_required=True,
    )
    assert json.loads(missions.hermes_mission_reconcile(
        mission_id, confirm=True, dry_run=False, hermes_root=root,
    ))["status"] == "awaiting_approval"

    backend_entered = threading.Event()
    backend_release = threading.Event()

    def paused_cancel(*args, **kwargs):
        backend_entered.set()
        assert backend_release.wait(5)
        return json.dumps({"success": True, "changed": True, "state": "cancelled"})

    monkeypatch.setattr(delegations.runners, "hermes_runner_cancel", paused_cancel)
    cancelled: list[dict] = []
    cancel_thread = threading.Thread(target=lambda: cancelled.append(json.loads(
        delegations.hermes_delegation_cancel(
            delegation_id, confirm=True, dry_run=False, hermes_root=root,
        )
    )))
    cancel_thread.start()
    assert backend_entered.wait(5)
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)
    approval = json.loads(missions.hermes_mission_approve(
        mission_id, "approval:cancel-race", confirm=True, dry_run=False, hermes_root=root,
    ))
    assert approval["success"] is False
    current = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    assert current["status"] == "awaiting_approval"
    assert current["approval"] == {}
    assert next(a for a in current["attachments"] if a["ref"] == delegation_id)["evidence_ref"] == evidence_ref
    backend_release.set()
    cancel_thread.join(5)
    assert not cancel_thread.is_alive()
    assert cancelled[0]["delegation"]["state"] == "cancelled"


def test_no_approval_reconcile_fails_closed_when_confirmed_cancel_starts_after_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)
    mission_id = "msn-reconcile-cancel-race"
    delegation_id = "dlg-reconcile-complete-cancel-race"
    _successful_mission_delegation(
        root, workspace, monkeypatch, mission_id=mission_id,
        delegation_id=delegation_id, task_id="reconcile-complete-cancel-race-task",
        final_approval_required=False,
    )

    observed = threading.Event()
    release_reconcile = threading.Event()
    original_guard = missions._completion_guard

    def paused_guard(*args, **kwargs):
        observed.set()
        assert release_reconcile.wait(5)
        return original_guard(*args, **kwargs)

    monkeypatch.setattr(missions, "_completion_guard", paused_guard)
    reconcile_result: list[dict] = []
    reconcile_thread = threading.Thread(target=lambda: reconcile_result.append(json.loads(
        missions.hermes_mission_reconcile(
            mission_id, confirm=True, dry_run=False, hermes_root=root,
        )
    )))
    reconcile_thread.start()
    assert observed.wait(5)

    backend_entered = threading.Event()
    backend_release = threading.Event()

    def paused_cancel(*args, **kwargs):
        backend_entered.set()
        assert backend_release.wait(5)
        return json.dumps({"success": True, "changed": True, "state": "cancelled"})

    monkeypatch.setattr(delegations.runners, "hermes_runner_cancel", paused_cancel)
    cancelled: list[dict] = []
    cancel_thread = threading.Thread(target=lambda: cancelled.append(json.loads(
        delegations.hermes_delegation_cancel(
            delegation_id, confirm=True, dry_run=False, hermes_root=root,
        )
    )))
    cancel_thread.start()
    assert backend_entered.wait(5)
    release_reconcile.set()
    reconcile_thread.join(5)
    assert not reconcile_thread.is_alive()
    assert reconcile_result[0]["success"] is False
    assert json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))["status"] != "completed"
    backend_release.set()
    cancel_thread.join(5)
    assert not cancel_thread.is_alive()
    assert cancelled[0]["delegation"]["state"] == "cancelled"


@pytest.mark.parametrize("completion_path", ["approve", "transition", "reconcile"])
def test_mission_completion_rejects_concurrent_reconcile_authority_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion_path: str,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = f"msn-stale-success-{completion_path}"
    delegation_id = f"dlg-stale-success-{completion_path}"
    task_id = f"stale-success-{completion_path}-task"
    approval_required = completion_path == "approve"
    if not approval_required:
        monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
        monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
        monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)
    _successful_mission_delegation(
        root, workspace, monkeypatch, mission_id=mission_id,
        delegation_id=delegation_id, task_id=task_id,
        final_approval_required=approval_required,
    )
    if approval_required:
        assert json.loads(missions.hermes_mission_reconcile(
            mission_id, confirm=True, dry_run=False, hermes_root=root,
        ))["status"] == "awaiting_approval"
    if completion_path in {"approve", "transition"}:
        monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
        monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
        monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)

    observed = threading.Event()
    release = threading.Event()
    original_guard = missions._completion_guard

    def paused_guard(*args, **kwargs):
        observed.set()
        assert release.wait(5)
        return original_guard(*args, **kwargs)

    monkeypatch.setattr(missions, "_completion_guard", paused_guard)
    result: list[dict] = []
    if completion_path == "approve":
        call = lambda: missions.hermes_mission_approve(mission_id, "approval:stale", confirm=True, dry_run=False, hermes_root=root)
    elif completion_path == "transition":
        call = lambda: missions.hermes_mission_transition(mission_id, "completed", confirm=True, dry_run=False, hermes_root=root)
    else:
        call = lambda: missions.hermes_mission_reconcile(mission_id, confirm=True, dry_run=False, hermes_root=root)
    thread = threading.Thread(target=lambda: result.append(json.loads(call())))
    thread.start()
    assert observed.wait(5)

    meta_path, _, _ = runners._job_paths(task_id, root)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update({"state": "running", "outcome": "running", "ended_at": ""})
    runners._atomic_json(meta_path, meta)
    invalidated = json.loads(delegations.hermes_delegation_reconcile(
        delegation_id, apply=True, hermes_root=root,
    ))
    assert invalidated["delegation"]["state"] == "running"
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert result[0]["success"] is False
    assert json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))["status"] != "completed"


def test_cancel_backend_rejection_releases_provisional_latch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "running"}))
    contract = _contract(workspace, task_id="cancel-rejected-task")
    json.loads(delegations.hermes_delegation_dispatch(json.dumps(contract), delegation_id="dlg-cancel-rejected", confirm=True, dry_run=False, hermes_root=root))
    before = json.loads(delegations.hermes_delegation_get("dlg-cancel-rejected", hermes_root=root))["delegation"]
    monkeypatch.setattr(delegations.runners, "hermes_runner_cancel", lambda *a, **k: json.dumps({"success": False, "changed": False, "code": "NOT_CANCELLABLE"}))
    out = json.loads(delegations.hermes_delegation_cancel("dlg-cancel-rejected", confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is False and out["changed"] is False
    row = out["delegation"]
    assert row["cancel_requested"] is False
    assert row["cancellation_in_progress"] is False
    assert row["authority_version"] == before["authority_version"] + 2


def test_concurrent_exact_cancel_invokes_backend_once_and_latches_ambiguous_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "running"}),
    )
    delegation_id = "dlg-exact-cancel-ambiguous"
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id="exact-cancel-ambiguous-task")),
        delegation_id=delegation_id, confirm=True, dry_run=False, hermes_root=root,
    ))["success"]

    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def ambiguous_cancel(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        return json.dumps({"success": False, "code": "BACKEND_TIMEOUT"})

    monkeypatch.setattr(delegations.runners, "hermes_runner_cancel", ambiguous_cancel)
    first_result: list[dict] = []
    first = threading.Thread(target=lambda: first_result.append(json.loads(
        delegations.hermes_delegation_cancel(
            delegation_id, confirm=True, dry_run=False, hermes_root=root,
        )
    )))
    first.start()
    assert entered.wait(5)
    retry = json.loads(delegations.hermes_delegation_cancel(
        delegation_id, confirm=True, dry_run=False, hermes_root=root,
    ))
    assert retry["success"] is False
    assert retry["code"] == "DELEGATION_CANCELLATION_IN_PROGRESS"
    assert retry["idempotent_retry"] is True
    assert calls == 1
    release.set()
    first.join(5)
    assert not first.is_alive()
    assert first_result[0]["success"] is False

    latched_retry = json.loads(delegations.hermes_delegation_cancel(
        delegation_id, confirm=True, dry_run=False, hermes_root=root,
    ))
    assert latched_retry["code"] == "DELEGATION_CANCELLATION_IN_PROGRESS"
    assert calls == 1
    durable = json.loads(delegations.hermes_delegation_get(
        delegation_id, hermes_root=root,
    ))["delegation"]
    assert durable["cancel_requested"] is True
    assert durable["cancellation_in_progress"] is True


def test_parent_cancellation_racing_child_cancellation_preserves_lock_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    delegation_id = "dlg-parent-child-cancel-race"
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "running"}),
    )
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id="parent-child-cancel-race-task")),
        mission_id=mission_id, delegation_id=delegation_id,
        confirm=True, dry_run=False, hermes_root=root,
    ))["success"]

    backend_entered = threading.Event()
    backend_release = threading.Event()

    def paused_cancel(*args, **kwargs):
        backend_entered.set()
        assert backend_release.wait(5)
        return json.dumps({"success": True, "changed": True, "state": "cancelled"})

    monkeypatch.setattr(delegations.runners, "hermes_runner_cancel", paused_cancel)
    child_result: list[dict] = []
    child = threading.Thread(target=lambda: child_result.append(json.loads(
        delegations.hermes_delegation_cancel(
            delegation_id, confirm=True, dry_run=False, hermes_root=root,
        )
    )))
    child.start()
    assert backend_entered.wait(5)

    parent_blocked = json.loads(missions.hermes_mission_transition(
        mission_id, "cancelled", confirm=True, dry_run=False, hermes_root=root,
    ))
    assert parent_blocked["success"] is False
    assert child.is_alive()
    backend_release.set()
    child.join(5)
    assert not child.is_alive()
    assert child_result[0]["delegation"]["state"] == "cancelled"

    parent_cancelled = json.loads(missions.hermes_mission_transition(
        mission_id, "cancelled", confirm=True, dry_run=False, hermes_root=root,
    ))
    assert parent_cancelled["success"] is True
    assert parent_cancelled["to_status"] == "cancelled"


def test_successful_cancel_failed_state_remains_reconciling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "running"}))
    contract = _contract(workspace, task_id="cancel-failed-state-task")
    json.loads(delegations.hermes_delegation_dispatch(json.dumps(contract), delegation_id="dlg-cancel-failed-state", confirm=True, dry_run=False, hermes_root=root))
    monkeypatch.setattr(delegations.runners, "hermes_runner_cancel", lambda *a, **k: json.dumps({"success": True, "changed": False, "state": "failed"}))
    out = json.loads(delegations.hermes_delegation_cancel("dlg-cancel-failed-state", confirm=True, dry_run=False, hermes_root=root))
    assert out["delegation"]["state"] == "reconciling"
    assert out["delegation"]["terminal_at"] is None


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


def test_reconcile_cas_preserves_concurrently_confirmed_cancellation(
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
        lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "running"}),
    )
    task_id = "delegation-reconcile-cancel-race"
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id=task_id)),
        mission_id=mission_id,
        delegation_id="dlg-reconcile-cancel-race",
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))["success"]
    meta_path, _, _ = runners._job_paths(task_id, root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": "pi_rpc",
        "state": "completed",
        "outcome": "completed",
        "created_at": "2026-08-22T00:00:00+00:00",
        "started_at": "2026-08-22T00:00:01+00:00",
        "ended_at": "2026-08-22T00:00:02+00:00",
        "error": "",
    })
    entered = threading.Event()
    release = threading.Event()
    original_validate = delegations.contract_mod._validate_manifest_impl

    def paused_validate(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(delegations.contract_mod, "_validate_manifest_impl", paused_validate)
    result: list[dict] = []
    thread = threading.Thread(target=lambda: result.append(json.loads(
        delegations.hermes_delegation_reconcile(
            "dlg-reconcile-cancel-race", apply=True, hermes_root=root,
        )
    )))
    thread.start()
    assert entered.wait(5)
    monkeypatch.setattr(
        delegations.runners,
        "hermes_runner_cancel",
        lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "cancelled"}),
    )
    cancelled = json.loads(delegations.hermes_delegation_cancel(
        "dlg-reconcile-cancel-race", confirm=True, dry_run=False, hermes_root=root,
    ))
    assert cancelled["delegation"]["state"] == "cancelled"
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert result[0]["stale_observation"] is True
    assert result[0]["applied"] is False
    assert result[0]["evidence_ref"] == ""
    assert result[0]["delegation"]["state"] == "cancelled"
    durable = json.loads(delegations.hermes_delegation_get(
        "dlg-reconcile-cancel-race", hermes_root=root,
    ))["delegation"]
    assert durable["state"] == "cancelled"
    assert durable["dispatch_phase"] == "cancelled"
    mission = json.loads(missions.hermes_mission_reconcile(
        mission_id, confirm=True, dry_run=False, hermes_root=root,
    ))
    assert mission["success"] is True
    assert mission["status"] not in {"awaiting_approval", "completed"}
    observed_attachment = next(item for item in mission["observed"] if item["ref"] == "dlg-reconcile-cancel-race")
    assert observed_attachment["state"] == "cancelled"
    assert observed_attachment["verified"] is False
    current_mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment = next(item for item in current_mission["attachments"] if item["ref"] == "dlg-reconcile-cancel-race")
    assert attachment["state"] == "cancelled"
    assert bool(attachment["verified"]) is False



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


@pytest.mark.parametrize(
    ("observed_state", "satisfied", "expected_state", "expected_mission_state"),
    [
        pytest.param("cancelled", True, "cancelled", "cancelled", id="cancelled"),
        pytest.param("failed", True, "failed", "failed", id="failed"),
        pytest.param("completed", True, "succeeded", "succeeded", id="completed-satisfied"),
        pytest.param("completed", False, "reconciling", "blocked", id="completed-unsatisfied"),
    ],
)
def test_reconcile_ambiguous_cancel_from_fresh_authoritative_terminal_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_state: str,
    satisfied: bool,
    expected_state: str,
    expected_mission_state: str,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    task_id = f"cancel-resolution-{observed_state}-{str(satisfied).lower()}"
    delegation_id = f"dlg-cancel-resolution-{observed_state}-{str(satisfied).lower()}"
    contract = _contract(workspace, task_id=task_id)
    if not satisfied:
        contract["completion_criteria"]["run_state"]["outcome_ok"] = ["different-outcome"]
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {"success": True, "changed": True, "backend": "pi_rpc", "state": "running"}
        ),
    )
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(contract), mission_id=mission_id, delegation_id=delegation_id,
        confirm=True, dry_run=False, hermes_root=root,
    ))["success"]

    monkeypatch.setattr(
        delegations.runners,
        "hermes_runner_cancel",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True}),
    )
    ambiguous = json.loads(delegations.hermes_delegation_cancel(
        delegation_id, confirm=True, dry_run=False, hermes_root=root,
    ))
    assert ambiguous["delegation"]["cancellation_in_progress"] is True
    with delegations._connect(delegations._db_path(root), write=True) as db:
        db.execute(
            "UPDATE delegations SET cancellation_claimed_at='2026-08-22T00:00:01+00:00' "
            "WHERE delegation_id=?", (delegation_id,),
        )
        db.commit()
    meta_path, _, _ = runners._job_paths(task_id, root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": "pi_rpc",
        "state": observed_state,
        "outcome": observed_state,
        "created_at": "2026-08-22T00:00:00+00:00",
        "started_at": "2026-08-22T00:00:01+00:00",
        "ended_at": "2026-08-22T00:00:02+00:00",
        "error": "failure" if observed_state == "failed" else "",
    })

    out = json.loads(delegations.hermes_delegation_reconcile(
        delegation_id, apply=True, hermes_root=root,
    ))
    row = out["delegation"]
    assert row["state"] == expected_state
    assert row["cancellation_in_progress"] is False
    assert row["cancel_requested"] is (observed_state == "cancelled")
    assert row["dispatch_phase"] == ("cancelled" if observed_state == "cancelled" else "dispatched")
    if expected_state == "reconciling":
        assert row["terminal_at"] is None
    else:
        assert row["terminal_at"] is not None
    if observed_state == "completed":
        assert row["validation_verdict"] == ("SATISFIED" if satisfied else "NOT_SATISFIED")
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment = next(item for item in mission["attachments"] if item["ref"] == delegation_id)
    assert attachment["state"] == expected_mission_state
    if expected_state == "succeeded":
        assert bool(attachment["verified"]) is True


@pytest.mark.parametrize("observation", [None, "running", "unexpected"])
def test_reconcile_ambiguous_cancel_retains_latch_without_terminal_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observation: str | None,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    task_id = f"cancel-unresolved-{observation or 'missing'}"
    delegation_id = f"dlg-cancel-unresolved-{observation or 'missing'}"
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "state": "running"}),
    )
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id=task_id)), delegation_id=delegation_id,
        confirm=True, dry_run=False, hermes_root=root,
    ))["success"]
    with delegations._connect(delegations._db_path(root), write=True) as db:
        delegations._init(db)
        db.execute(
            "UPDATE delegations SET state='reconciling',cancel_requested=1,cancellation_in_progress=1,"
            "cancellation_claimed_at='2026-08-22T00:00:01+00:00',cancellation_observation_sha256='',"
            "cancellation_watermark_ready=1 "
            "WHERE delegation_id=?", (delegation_id,),
        )
        db.commit()
    if observation is not None:
        meta_path, _, _ = runners._job_paths(task_id, root)
        runners._atomic_json(meta_path, {
            "schema_version": runners.SCHEMA_VERSION, "task_id": task_id, "backend": "pi_rpc",
            "state": observation, "outcome": observation,
            "created_at": "2026-08-22T00:00:00+00:00", "started_at": "2026-08-22T00:00:01+00:00",
            "ended_at": "", "error": "",
        })
    out = json.loads(delegations.hermes_delegation_reconcile(
        delegation_id, apply=True, hermes_root=root,
    ))
    assert out["delegation"]["state"] == "reconciling"
    assert out["delegation"]["cancel_requested"] is True
    assert out["delegation"]["cancellation_in_progress"] is True


def test_reconcile_ambiguous_cancel_rejects_stale_precancellation_terminal_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    task_id = "cancel-stale-terminal-task"
    delegation_id = "dlg-cancel-stale-terminal"
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "state": "running"}),
    )
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id=task_id)), delegation_id=delegation_id,
        confirm=True, dry_run=False, hermes_root=root,
    ))["success"]
    monkeypatch.setattr(
        delegations.runners,
        "hermes_runner_cancel",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True}),
    )
    cancelled = json.loads(delegations.hermes_delegation_cancel(
        delegation_id, confirm=True, dry_run=False, hermes_root=root,
    ))
    assert cancelled["delegation"]["cancellation_in_progress"] is True
    with delegations._connect(delegations._db_path(root), write=True) as db:
        db.execute(
            "UPDATE delegations SET cancellation_claimed_at='2026-08-22T00:00:01+00:00' "
            "WHERE delegation_id=?", (delegation_id,),
        )
        db.commit()
    # This terminal record was absent at watermark capture and changes the
    # observation hash, but its terminal authority predates the cancel claim.
    meta_path, _, _ = runners._job_paths(task_id, root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION, "task_id": task_id, "backend": "pi_rpc",
        "state": "completed", "outcome": "completed",
        "created_at": "2026-08-21T23:59:58+00:00", "started_at": "2026-08-21T23:59:59+00:00",
        "ended_at": "2026-08-22T00:00:00+00:00", "error": "",
    })

    reconciled = json.loads(delegations.hermes_delegation_reconcile(
        delegation_id, apply=True, hermes_root=root,
    ))
    row = reconciled["delegation"]
    assert row["state"] == "reconciling"
    assert row["cancel_requested"] is True
    assert row["cancellation_in_progress"] is True
    assert row["terminal_at"] is None


@pytest.mark.parametrize("terminal_at", ["", "not-a-time", "2026-08-22T00:00:01+00:00"])
def test_reconcile_ambiguous_cancel_requires_strict_parseable_terminal_ordering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_at: str,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    task_id = f"cancel-ordering-{hashlib.sha256(terminal_at.encode()).hexdigest()[:8]}"
    delegation_id = f"dlg-{task_id}"
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "state": "running"}),
    )
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id=task_id)), delegation_id=delegation_id,
        confirm=True, dry_run=False, hermes_root=root,
    ))["success"]
    with delegations._connect(delegations._db_path(root), write=True) as db:
        delegations._init(db)
        db.execute(
            "UPDATE delegations SET state='reconciling',cancel_requested=1,cancellation_in_progress=1,"
            "cancellation_claimed_at='2026-08-22T00:00:01+00:00',"
            "cancellation_observation_sha256='different-old-payload',cancellation_watermark_ready=1 "
            "WHERE delegation_id=?", (delegation_id,),
        )
        db.commit()
    meta_path, _, _ = runners._job_paths(task_id, root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION, "task_id": task_id, "backend": "pi_rpc",
        "state": "failed", "outcome": "failed", "created_at": "2026-08-22T00:00:00+00:00",
        "started_at": "2026-08-22T00:00:00+00:00", "ended_at": terminal_at, "error": "failure",
    })
    row = json.loads(delegations.hermes_delegation_reconcile(
        delegation_id, apply=True, hermes_root=root,
    ))["delegation"]
    assert row["state"] == "reconciling"
    assert row["cancel_requested"] is True
    assert row["cancellation_in_progress"] is True


def test_reconcile_migrated_cancel_row_without_claim_ordering_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    task_id = "cancel-migrated-no-ordering"
    delegation_id = "dlg-cancel-migrated-no-ordering"
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps({"success": True, "changed": True, "state": "running"}),
    )
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id=task_id)), delegation_id=delegation_id,
        confirm=True, dry_run=False, hermes_root=root,
    ))["success"]
    with delegations._connect(delegations._db_path(root), write=True) as db:
        db.execute(
            "UPDATE delegations SET state='reconciling',cancel_requested=1,cancellation_in_progress=1,"
            "cancellation_claimed_at='',cancellation_observation_sha256='',cancellation_watermark_ready=1 "
            "WHERE delegation_id=?", (delegation_id,),
        )
        db.commit()
    meta_path, _, _ = runners._job_paths(task_id, root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION, "task_id": task_id, "backend": "pi_rpc",
        "state": "cancelled", "outcome": "cancelled", "created_at": "2026-08-22T00:00:00+00:00",
        "started_at": "2026-08-22T00:00:01+00:00", "ended_at": "2026-08-22T00:00:02+00:00",
        "error": "",
    })
    row = json.loads(delegations.hermes_delegation_reconcile(
        delegation_id, apply=True, hermes_root=root,
    ))["delegation"]
    assert row["state"] == "reconciling"
    assert row["cancel_requested"] is True
    assert row["cancellation_in_progress"] is True


@pytest.mark.parametrize("with_mission", [False, True])
@pytest.mark.parametrize(
    ("backend_payload", "expected_backend_state"),
    [
        pytest.param({}, "", id="missing"),
        pytest.param({"state": ""}, "", id="empty"),
        pytest.param({"state": None}, "", id="null"),
        pytest.param({"state": "unexpected"}, "unexpected", id="unexpected"),
    ],
)
def test_successful_cancel_without_explicit_cancelled_state_stays_reconciling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_mission: bool,
    backend_payload: dict,
    expected_backend_state: str,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root) if with_mission else ""
    suffix = expected_backend_state or "absent"
    delegation_id = f"dlg-cancel-explicit-{suffix}-{str(with_mission).lower()}"
    task_id = f"cancel-explicit-{suffix}-{str(with_mission).lower()}"
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {"success": True, "changed": True, "backend": "pi_rpc", "state": "running"}
        ),
    )
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id=task_id)),
        mission_id=mission_id,
        delegation_id=delegation_id,
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))["success"] is True
    calls = 0

    def cancel(*args, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps({"success": True, "changed": True, **backend_payload})

    monkeypatch.setattr(
        delegations.runners,
        "hermes_runner_cancel",
        cancel,
    )

    out = json.loads(delegations.hermes_delegation_cancel(
        delegation_id, confirm=True, dry_run=False, hermes_root=root,
    ))

    assert out["success"] is True
    row = out["delegation"]
    assert row["state"] == "reconciling"
    assert row["backend_state"] == expected_backend_state
    assert row["outcome"] == ""
    assert row["terminal_at"] is None
    assert row["cancel_requested"] is True
    assert row["cancellation_in_progress"] is True
    assert row["dispatch_phase"] == "dispatched"
    durable = json.loads(delegations.hermes_delegation_get(
        delegation_id, hermes_root=root,
    ))["delegation"]
    assert durable["events"][0]["event_type"] == "delegation.cancel_requested"
    assert all(event["event_type"] != "delegation.cancelled" for event in durable["events"])
    retry = json.loads(delegations.hermes_delegation_cancel(
        delegation_id, confirm=True, dry_run=False, hermes_root=root,
    ))
    assert retry["code"] == "DELEGATION_CANCELLATION_IN_PROGRESS"
    assert retry["idempotent_retry"] is True
    assert calls == 1
    if mission_id:
        mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
        attachment = next(item for item in mission["attachments"] if item["ref"] == delegation_id)
        assert attachment["state"] == "blocked"
        assert attachment["evidence_ref"] == f"delegation:{delegation_id}"


@pytest.mark.parametrize("with_mission", [False, True])
def test_concurrent_exact_cancel_coalesces_while_confirmed_cancellation_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_mission: bool,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root) if with_mission else ""
    delegation_id = f"dlg-concurrent-cancel-{str(with_mission).lower()}"
    task_id = f"delegation-concurrent-cancel-{str(with_mission).lower()}"
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {"success": True, "changed": True, "backend": "pi_rpc", "state": "running"}
        ),
    )
    dispatched = json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id=task_id)),
        mission_id=mission_id,
        delegation_id=delegation_id,
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))
    assert dispatched["success"] is True

    first_entered = threading.Event()
    second_entered = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def cancel(*args, **kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
            call = calls
        if call == 1:
            first_entered.set()
            assert second_entered.wait(5)
            return json.dumps({"success": True, "changed": True, "state": "cancelled"})
        raise AssertionError("exact retry must not invoke backend cancellation")

    monkeypatch.setattr(delegations.runners, "hermes_runner_cancel", cancel)
    results: list[dict] = []
    first = threading.Thread(target=lambda: results.append(json.loads(
        delegations.hermes_delegation_cancel(
            delegation_id, confirm=True, dry_run=False, hermes_root=root,
        )
    )))
    def exact_retry():
        results.append(json.loads(delegations.hermes_delegation_cancel(
            delegation_id, confirm=True, dry_run=False, hermes_root=root,
        )))
        second_entered.set()

    second = threading.Thread(target=exact_retry)
    first.start()
    assert first_entered.wait(5)
    second.start()
    assert second_entered.wait(5)
    first.join(5)
    assert not first.is_alive()
    assert results[0]["code"] == "DELEGATION_CANCELLATION_IN_PROGRESS"
    assert results[0]["cancellation_outcome_ambiguous"] is True
    confirmed = results[1]["delegation"]
    assert confirmed["state"] == "cancelled"
    terminal_at = confirmed["terminal_at"]
    second.join(5)
    assert not second.is_alive()

    assert calls == 1
    durable = json.loads(delegations.hermes_delegation_get(
        delegation_id, hermes_root=root,
    ))["delegation"]
    assert durable["state"] == "cancelled"
    assert durable["outcome"] == "cancelled"
    assert durable["cancel_requested"] is True
    assert durable["dispatch_phase"] == "cancelled"
    assert durable["terminal_at"] == terminal_at
    cancel_events = [
        event for event in durable["events"]
        if event["event_type"] in {"delegation.cancelled", "delegation.cancel_requested"}
    ]
    assert [event["event_type"] for event in cancel_events] == ["delegation.cancelled"]
    if mission_id:
        mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
        attachment = next(item for item in mission["attachments"] if item["ref"] == delegation_id)
        assert attachment["state"] == "cancelled"
        assert attachment["evidence_ref"] == f"delegation:{delegation_id}"


@pytest.mark.parametrize("with_mission", [False, True])
def test_concurrent_exact_cancel_preserves_delayed_confirmed_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_mission: bool,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root) if with_mission else ""
    delegation_id = f"dlg-cancel-promote-{str(with_mission).lower()}"
    task_id = f"delegation-cancel-promote-{str(with_mission).lower()}"
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(
            {"success": True, "changed": True, "backend": "pi_rpc", "state": "running"}
        ),
    )
    dispatched = json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id=task_id)),
        mission_id=mission_id,
        delegation_id=delegation_id,
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))
    assert dispatched["success"] is True

    confirmed_entered = threading.Event()
    weaker_entered = threading.Event()
    release_confirmed = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def cancel(*args, **kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
            call = calls
        if call == 1:
            confirmed_entered.set()
            assert weaker_entered.wait(5)
            assert release_confirmed.wait(5)
            return json.dumps({"success": True, "changed": True, "state": "cancelled"})
        weaker_entered.set()
        raise AssertionError("exact retry must not invoke backend cancellation")

    monkeypatch.setattr(delegations.runners, "hermes_runner_cancel", cancel)
    results: list[dict] = []
    confirmed = threading.Thread(target=lambda: results.append(json.loads(
        delegations.hermes_delegation_cancel(
            delegation_id, confirm=True, dry_run=False, hermes_root=root,
        )
    )))
    def exact_retry():
        results.append(json.loads(delegations.hermes_delegation_cancel(
            delegation_id, confirm=True, dry_run=False, hermes_root=root,
        )))
        weaker_entered.set()

    weaker = threading.Thread(target=exact_retry)
    confirmed.start()
    assert confirmed_entered.wait(5)
    weaker.start()
    assert weaker_entered.wait(5)
    weaker.join(5)
    assert not weaker.is_alive()
    assert results[0]["success"] is False
    assert results[0]["code"] == "DELEGATION_CANCELLATION_IN_PROGRESS"
    assert results[0]["delegation"]["cancellation_in_progress"] is True
    release_confirmed.set()
    confirmed.join(5)
    assert not confirmed.is_alive()

    promoted = results[1]
    assert promoted["success"] is True
    assert promoted["changed"] is True
    assert promoted["delegation"]["state"] == "cancelled"
    assert promoted["delegation"]["dispatch_phase"] == "cancelled"
    assert promoted["delegation"]["terminal_at"] is not None
    assert calls == 1
    durable = json.loads(delegations.hermes_delegation_get(
        delegation_id, hermes_root=root,
    ))["delegation"]
    assert durable["state"] == "cancelled"
    assert durable["outcome"] == "cancelled"
    assert durable["cancel_requested"] is True
    assert durable["dispatch_phase"] == "cancelled"
    cancel_events = [
        event["event_type"] for event in durable["events"]
        if event["event_type"] in {"delegation.cancelled", "delegation.cancel_requested"}
    ]
    assert cancel_events.count("delegation.cancelled") == 1
    assert cancel_events == ["delegation.cancelled"]
    if mission_id:
        mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
        attachment = next(item for item in mission["attachments"] if item["ref"] == delegation_id)
        assert attachment["state"] == "cancelled"
        assert attachment["evidence_ref"] == f"delegation:{delegation_id}"


@pytest.mark.parametrize("cancel_kind", ["reserved", "dispatched"])
@pytest.mark.parametrize("observed_state", ["running", "completed"])
@pytest.mark.parametrize("apply", [False, True])
def test_authoritative_cancellation_is_terminal_during_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_kind: str,
    observed_state: str,
    apply: bool,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root) if cancel_kind == "dispatched" else ""
    task_id = f"delegation-cancel-final-{cancel_kind}-{observed_state}-{apply}"
    delegation_id = f"dlg-cancel-final-{cancel_kind}-{observed_state}-{str(apply).lower()}"
    contract = _contract(workspace, task_id=task_id)
    dispatch_state = (
        {"success": False, "changed": False, "code": "REJECTED"}
        if cancel_kind == "reserved"
        else {"success": True, "changed": True, "backend": "pi_rpc", "state": "running"}
    )
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *args, **kwargs: json.dumps(dispatch_state),
    )
    json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(contract),
        mission_id=mission_id,
        delegation_id=delegation_id,
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))
    if cancel_kind == "dispatched":
        monkeypatch.setattr(
            delegations.runners,
            "hermes_runner_cancel",
            lambda *args, **kwargs: json.dumps(
                {"success": True, "changed": True, "backend": "pi_rpc", "state": "cancelled"}
            ),
        )
    cancelled = json.loads(delegations.hermes_delegation_cancel(
        delegation_id,
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))["delegation"]
    assert cancelled["state"] == "cancelled"
    assert cancelled["cancel_requested"] is True

    meta_path, _, _ = runners._job_paths(task_id, root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": "pi_rpc",
        "state": observed_state,
        "outcome": observed_state,
        "created_at": "2026-08-22T00:00:00+00:00",
        "started_at": "2026-08-22T00:00:01+00:00",
        "ended_at": "2026-08-22T00:00:02+00:00" if observed_state == "completed" else "",
        "error": "",
    })
    reconciled = json.loads(delegations.hermes_delegation_reconcile(
        delegation_id,
        apply=apply,
        hermes_root=root,
    ))
    assert reconciled["success"] is True
    assert reconciled["delegation"]["state"] == "cancelled"
    assert reconciled["delegation"]["cancel_requested"] is True
    assert reconciled["delegation"]["dispatch_phase"] == cancelled["dispatch_phase"]
    assert reconciled["delegation"]["terminal_at"] == cancelled["terminal_at"]
    assert reconciled["delegation"]["outcome"] == "cancelled"
    assert reconciled["delegation"]["backend_state"] == observed_state
    if observed_state == "completed":
        assert reconciled["delegation"]["validation_verdict"] == "SATISFIED"
    assert reconciled["evidence_ref"] == ""
    persisted = json.loads(delegations.hermes_delegation_get(delegation_id, hermes_root=root))["delegation"]
    assert persisted["state"] == "cancelled"
    assert persisted["cancel_requested"] is True
    assert persisted["dispatch_phase"] == cancelled["dispatch_phase"]
    assert persisted["terminal_at"] == cancelled["terminal_at"]
    assert persisted["outcome"] == "cancelled"

    if mission_id:
        mission = json.loads(missions.hermes_mission_reconcile(
            mission_id,
            confirm=True,
            dry_run=False,
            hermes_root=root,
        ))
        observed = next(item for item in mission["observed"] if item["ref"] == delegation_id)
        assert observed["state"] == "cancelled"
        assert observed["verified"] is False
        assert mission["status"] not in {"awaiting_approval", "completed"}
        monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
        monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
        monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)
        approval = json.loads(missions.hermes_mission_approve(
            mission_id,
            "approval:cancelled-child",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        ))
        assert approval["success"] is False


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
    assert retry["success"] is False
    assert retry["code"] == "DELEGATION_DISPATCH_AMBIGUOUS"
    assert retry["submission_may_have_succeeded"] is True
    assert retry["delegation"]["dispatch_phase"] == "invoking"
    assert result[0]["delegation"]["dispatch_phase"] == "dispatched"


def test_dispatch_completion_cas_preserves_inflight_confirmed_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    entered = threading.Event()
    release = threading.Event()

    def dispatch(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return json.dumps({"success": True, "changed": True, "state": "queued"})

    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", dispatch)
    monkeypatch.setattr(
        delegations.runners,
        "hermes_runner_cancel",
        lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "cancelled"}),
    )
    result: list[dict] = []
    thread = threading.Thread(target=lambda: result.append(json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(_contract(workspace, task_id="delegation-dispatch-cancel-race")),
            mission_id=mission_id,
            delegation_id="dlg-dispatch-cancel-race",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )))
    thread.start()
    assert entered.wait(5)
    cancelled = json.loads(delegations.hermes_delegation_cancel(
        "dlg-dispatch-cancel-race", confirm=True, dry_run=False, hermes_root=root,
    ))
    assert cancelled["success"] is True
    assert cancelled["delegation"]["state"] == "cancelled"
    assert cancelled["delegation"]["dispatch_phase"] == "cancelled"
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert result[0]["success"] is False
    assert result[0]["code"] == "DELEGATION_DISPATCH_CANCELLED"
    assert result[0]["delegation"]["state"] == "cancelled"
    durable = json.loads(delegations.hermes_delegation_get(
        "dlg-dispatch-cancel-race", hermes_root=root,
    ))["delegation"]
    assert durable["state"] == "cancelled"
    assert durable["dispatch_phase"] == "cancelled"
    assert durable["cancel_requested"] is True
    mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
    attachment = next(item for item in mission["attachments"] if item["ref"] == "dlg-dispatch-cancel-race")
    assert attachment["state"] == "cancelled"
    assert bool(attachment["verified"]) is False


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


_CHANGED_MISSING = object()


@pytest.mark.parametrize(
    "backend_changed",
    [_CHANGED_MISSING, None, 0, "false", []],
    ids=["missing", "null", "falsey-non-bool", "truthy-non-bool", "malformed-container"],
)
def test_ambiguous_failed_dispatch_cannot_reinvoke_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend_changed: object,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    calls = 0

    def dispatch(*args, **kwargs):
        nonlocal calls
        calls += 1
        response = {"success": False, "code": "BACKEND_FAILURE"}
        if backend_changed is not _CHANGED_MISSING:
            response["changed"] = backend_changed
        return json.dumps(response)

    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", dispatch)
    contract = json.dumps(_contract(workspace, task_id="delegation-ambiguous-retry"))
    first = json.loads(delegations.hermes_delegation_dispatch(
        contract,
        delegation_id="dlg-ambiguous-retry",
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))
    retry = json.loads(delegations.hermes_delegation_dispatch(
        contract,
        delegation_id="dlg-ambiguous-retry",
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))

    assert calls == 1
    assert first["submission_may_have_succeeded"] is True
    assert first["delegation"]["state"] == "reconciling"
    assert first["delegation"]["dispatch_phase"] == "invoking"
    assert retry["success"] is False
    assert retry["code"] == "DELEGATION_DISPATCH_AMBIGUOUS"
    assert retry["submission_may_have_succeeded"] is True
    assert retry["delegation"]["dispatch_phase"] == "invoking"


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


def test_legacy_manifest_can_be_backfilled_only_from_matching_contract_on_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    contract = _contract(workspace, task_id="delegation-legacy-manifest")
    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", lambda *a, **k: json.dumps({"success": True, "changed": True, "state": "queued"}))
    assert json.loads(delegations.hermes_delegation_dispatch(json.dumps(contract), delegation_id="dlg-legacy-manifest", confirm=True, dry_run=False, hermes_root=root))["success"]
    with delegations._connect(delegations._db_path(root), write=True) as db:
        db.execute("DELETE FROM delegation_validation_manifests WHERE delegation_id='dlg-legacy-manifest'")
        db.commit()

    missing = json.loads(delegations.hermes_delegation_reconcile("dlg-legacy-manifest", apply=False, hermes_root=root))
    assert missing["success"] is False
    supplied_preview = json.loads(delegations.hermes_delegation_reconcile("dlg-legacy-manifest", contract_json=json.dumps(contract), apply=False, hermes_root=root))
    assert supplied_preview["success"] is True
    with delegations._connect(delegations._db_path(root), write=False) as db:
        assert db.execute("SELECT 1 FROM delegation_validation_manifests WHERE delegation_id='dlg-legacy-manifest'").fetchone() is None
    backfilled = json.loads(delegations.hermes_delegation_reconcile("dlg-legacy-manifest", contract_json=json.dumps(contract), apply=True, hermes_root=root))
    assert backfilled["success"] is True
    subsequent = json.loads(delegations.hermes_delegation_reconcile("dlg-legacy-manifest", apply=False, hermes_root=root))
    assert subsequent["success"] is True


def test_reserved_cancel_requires_confirmation_without_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", lambda *a, **k: json.dumps({"success": False, "changed": False, "code": "REJECTED"}))
    contract = _contract(workspace, task_id="delegation-reserved-confirm")
    json.loads(delegations.hermes_delegation_dispatch(json.dumps(contract), delegation_id="dlg-reserved-confirm", confirm=True, dry_run=False, hermes_root=root))

    out = json.loads(delegations.hermes_delegation_cancel("dlg-reserved-confirm", confirm=False, dry_run=False, hermes_root=root))
    assert out["success"] is False and out["code"] == "CONFIRMATION_REQUIRED" and out["changed"] is False
    row = json.loads(delegations.hermes_delegation_get("dlg-reserved-confirm", hermes_root=root))["delegation"]
    assert row["state"] == "reserved" and row["cancel_requested"] is False
    assert [event["event_type"] for event in row["events"]] == ["delegation.dispatch_rejected", "delegation.invoking", "delegation.reserved"]


def test_reserved_cancel_cas_loss_does_not_forge_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", lambda *a, **k: json.dumps({"success": False, "changed": False, "code": "REJECTED"}))
    contract = _contract(workspace, task_id="delegation-cancel-race")
    json.loads(delegations.hermes_delegation_dispatch(json.dumps(contract), delegation_id="dlg-cancel-race", confirm=True, dry_run=False, hermes_root=root))
    real_connect = delegations._connect
    raced = False

    class RacingConnection:
        def __init__(self, db): self.db = db
        def __enter__(self): self.db.__enter__(); return self
        def __exit__(self, *args): return self.db.__exit__(*args)
        def __getattr__(self, name): return getattr(self.db, name)
        def execute(self, sql, parameters=()):
            nonlocal raced
            if not raced and sql.startswith("UPDATE delegations SET state='cancelled'"):
                raced = True
                self.db.execute("UPDATE delegations SET dispatch_phase='invoking' WHERE delegation_id=?", (parameters[-1],))
            return self.db.execute(sql, parameters)

    monkeypatch.setattr(delegations, "_connect", lambda path, write=False: RacingConnection(real_connect(path, write=write)) if write else real_connect(path, write=write))
    out = json.loads(delegations.hermes_delegation_cancel("dlg-cancel-race", confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is False and out["code"] == "DELEGATION_CANCEL_AMBIGUOUS" and out["changed"] is False
    assert out["delegation"]["dispatch_phase"] == "invoking"
    row = json.loads(delegations.hermes_delegation_get("dlg-cancel-race", hermes_root=root))["delegation"]
    assert row["state"] == "reserved" and row["cancel_requested"] is False
    assert "delegation.cancelled" not in [event["event_type"] for event in row["events"]]


def test_reserved_cancel_mission_sync_failure_is_hard_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root)
    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", lambda *a, **k: json.dumps({"success": False, "changed": False, "code": "REJECTED"}))
    contract = _contract(workspace, task_id="delegation-reserved-sync")
    json.loads(delegations.hermes_delegation_dispatch(json.dumps(contract), mission_id=mission_id, delegation_id="dlg-reserved-sync", confirm=True, dry_run=False, hermes_root=root))
    monkeypatch.setattr(missions, "record_attachment_state", lambda *a, **k: False)

    out = json.loads(delegations.hermes_delegation_cancel("dlg-reserved-sync", confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is False and out["code"] == "DELEGATION_MISSION_SYNC_FAILED"
    assert out["changed"] is True and out["delegation"]["state"] == "cancelled"


@pytest.mark.parametrize("with_mission", [False, True])
def test_reserved_cancel_exact_dispatch_retry_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_mission: bool,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root) if with_mission else ""
    contract = _contract(workspace, task_id=f"delegation-cancelled-retry-{with_mission}")
    delegation_id = f"dlg-cancelled-retry-{str(with_mission).lower()}"
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *a, **k: json.dumps({"success": False, "changed": False, "code": "REJECTED"}),
    )
    first = json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(contract),
        mission_id=mission_id,
        delegation_id=delegation_id,
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))
    assert first["success"] is False

    cancelled = json.loads(delegations.hermes_delegation_cancel(
        delegation_id,
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))
    assert cancelled["success"] is True
    assert cancelled["delegation"]["state"] == "cancelled"
    assert cancelled["delegation"]["dispatch_phase"] == "cancelled"
    assert cancelled["delegation"]["cancel_requested"] is True

    calls = 0

    def dispatch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps({"success": True, "changed": True, "state": "queued"})

    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", dispatch)
    retry = json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(contract),
        mission_id=mission_id,
        delegation_id=delegation_id,
        confirm=True,
        dry_run=False,
        hermes_root=root,
    ))
    assert retry["success"] is False
    assert retry["code"] == "DELEGATION_DISPATCH_CANCELLED"
    assert retry["changed"] is False
    assert retry.get("idempotent") is not True
    assert calls == 0

    row = json.loads(delegations.hermes_delegation_get(delegation_id, hermes_root=root))["delegation"]
    assert row["state"] == "cancelled"
    assert row["dispatch_phase"] == "cancelled"
    assert row["cancel_requested"] is True
    if with_mission:
        mission = json.loads(missions.hermes_mission_get(mission_id, hermes_root=root))
        attachment = next(item for item in mission["attachments"] if item["kind"] == "delegation" and item["ref"] == delegation_id)
        assert attachment["state"] == "cancelled"
        assert attachment["evidence_ref"] == f"delegation:{delegation_id}"


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
    assert "cancellation_claimed_at" in columns
    assert "cancellation_observation_sha256" in columns
    assert "cancellation_watermark_ready" in columns
    assert "delegation_validation_manifests" in tables
