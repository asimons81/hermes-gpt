from __future__ import annotations

import pytest

import operator_contract as op_contract
import operator_fabric as fabric
from test_operator_fabric import contract, coordinator, service


def test_fabric_rejects_assigned_profile_outside_work_contract_scope(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    coord = coordinator(tmp_path, svc)
    value = contract(tmp_path)
    value["allowed_scope"]["profiles"] = ["qa"]

    with pytest.raises(fabric.FabricError) as exc:
        coord.dispatch(value, dry_run=True, confirm=False, timeout=10)

    assert exc.value.code == "FABRIC_AUTHORITY_DENIED"
    assert "outside the Work Contract profile scope" in str(exc.value)


def test_fabric_rejects_forbidden_action_contract_before_remote_dispatch(tmp_path, monkeypatch):
    counter = {"count": 0}
    svc = service(tmp_path, monkeypatch, dispatch_counter=counter)
    coord = coordinator(tmp_path, svc)
    value = contract(tmp_path)
    value["forbidden_actions"] = [
        {"action": "public_publish", "reason": "must remain private", "class": "HIGH"}
    ]

    with pytest.raises(fabric.FabricError) as exc:
        coord.dispatch(value, dry_run=False, confirm=True, timeout=10)

    assert exc.value.code == "FABRIC_EVIDENCE_POLICY_INVALID"
    assert counter["count"] == 0


def test_historical_fabric_run_cannot_self_certify_forbidden_action_absence(
    tmp_path, monkeypatch
):
    value = contract(tmp_path)
    value["forbidden_actions"] = [
        {"action": "public_publish", "reason": "must remain private", "class": "HIGH"}
    ]
    monkeypatch.setattr(op_contract, "_observed_audit", list)

    class HistoricalFabricBackend:
        def observed_runs(self, task_id, *, hermes_root=None):
            return [
                {
                    "task_id": task_id,
                    "backend": "fabric",
                    "scope": "fabric:node-a",
                    "status": "completed",
                    "outcome": "completed",
                }
            ]

    original_get_backend = op_contract.op_runners.get_backend
    monkeypatch.setattr(
        op_contract.op_runners,
        "get_backend",
        lambda name: HistoricalFabricBackend()
        if name == "fabric"
        else original_get_backend(name),
    )

    result = op_contract._check_forbidden(value, tmp_path)

    assert result["status"] == "UNVERIFIED"
    assert "no coordinator-verifiable forbidden-action evidence" in result["detail"]
