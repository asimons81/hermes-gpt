from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_delegations as delegations
import operator_mission_runtime as missions
import operator_policy as op


def _enable_workspace(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(workspace))


def _contract(workspace: Path, *, task_id: str, backend: str = "fleet") -> dict:
    return {
        "schema": "hermes.work-contract/v1",
        "task_id": task_id,
        "assigned_agent": "coder",
        "assigned_profile": "default",
        "objective": "verify stale-dispatch recovery invariants (item 252)",
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


def _fake_dispatch_ok(contract_json: str, **kwargs) -> str:
    contract = json.loads(contract_json)
    return json.dumps({"success": True, "changed": True, "state": "running",
                       "backend": contract.get("execution", {}).get("backend", "fleet"),
                       "task_id": contract["task_id"]})


def _mission(root: Path, mission_id: str) -> str:
    payload = json.loads(
        missions.hermes_mission_create(
            json.dumps(
                {
                    "schema": missions.MISSION_SPEC_SCHEMA,
                    "mission_id": mission_id,
                    "title": "Item 252 regression",
                    "objective": "Exercise stale-dispatch recovery.",
                }
            ),
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )
    assert payload["success"] is True
    return mission_id


# ---------------------------------------------------------------------------
# Item 252 defect 1: reconcile must leave a PURE reserved row (state=reserved,
# phase=reserved, zero observations) stable — flipping it back to 'reconciling'
# made the stale-invocation recovery self-erasing (live oscillation on
# dlg-c51c61e4 2026-10-11: recover -> re-freeze, twice, wedged 4h+).
# ---------------------------------------------------------------------------

def test_reconcile_keeps_pure_reserved_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root, "msn-252-pure-reserved")
    task_id = "item252-pure-reserved"
    delegation_id = f"dlg-{task_id}"

    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", _fake_dispatch_ok)
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id=task_id)), mission_id=mission_id,
        delegation_id=delegation_id, confirm=True, dry_run=False, hermes_root=root,
    ))["success"]

    # Strand the row pure-reserved (the post-#35 recovered state), zero obs.
    with delegations._connect(delegations._db_path(root), write=True) as db:
        delegations._init(db)
        db.execute(
            "UPDATE delegations SET state='reserved', dispatch_phase='reserved', "
            "backend_state='not_invoked', cancel_requested=0, cancellation_in_progress=0 "
            "WHERE delegation_id=?",
            (delegation_id,),
        )
        db.commit()

    for round_no in range(3):
        out = json.loads(delegations.hermes_delegation_reconcile(
            delegation_id, apply=True, hermes_root=root))
        row = out["delegation"]
        assert row["state"] == "reserved", (
            f"round {round_no}: pure reserved must stay reserved (got {row['state']!r})"
        )
        assert row["dispatch_phase"] == "reserved"
        assert row["backend_state"] == "not_invoked"


# ---------------------------------------------------------------------------
# Item 252 defect 2: a pending/blocked reconcile sync must not clobber the
# dispatch reservation's contract: evidence_ref — the reservation bridge
# rejects any re-drive whose evidence_ref differs, so rewriting it to
# 'delegation:<id>' poisoned the row against its own retry (live: re-drive of
# dlg-c51c61e4 refused 'Mission delegation reservation was rejected').
# ---------------------------------------------------------------------------

def test_reconcile_preserves_contract_evidence_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "hermes"
    _enable_workspace(monkeypatch, workspace)
    mission_id = _mission(root, "msn-252-evidence-ref")
    task_id = "item252-evidence-ref"
    delegation_id = f"dlg-{task_id}"

    monkeypatch.setattr(delegations.contract_mod, "hermes_contract_dispatch", _fake_dispatch_ok)
    assert json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_contract(workspace, task_id=task_id)), mission_id=mission_id,
        delegation_id=delegation_id, confirm=True, dry_run=False, hermes_root=root,
    ))["success"]

    with delegations._connect(delegations._db_path(root), write=False) as db:
        assert "contract_sha256" in dict(delegations._get_row(db, delegation_id))
    with missions._connect(missions._db_path(root), write=False) as db:
        att = db.execute(
            "SELECT evidence_ref FROM attachments WHERE mission_id=? AND kind='delegation' AND ref=?",
            (mission_id, delegation_id),
        ).fetchone()
    assert att is not None and str(att[0]).startswith("contract:"), (
        "dispatch reservation must hold a contract: evidence_ref"
    )

    # Reconcile the running row repeatedly (non-verified sync path).
    for _ in range(3):
        out = json.loads(delegations.hermes_delegation_reconcile(
            delegation_id, apply=True, hermes_root=root))
        assert out["success"]

    with missions._connect(missions._db_path(root), write=False) as db:
        att2 = db.execute(
            "SELECT evidence_ref FROM attachments WHERE mission_id=? AND kind='delegation' AND ref=?",
            (mission_id, delegation_id),
        ).fetchone()
    assert str(att2[0]).startswith("contract:"), (
        f"reconcile must not clobber the contract: reservation (got {att2[0]!r})"
    )
