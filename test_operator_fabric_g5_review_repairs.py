from __future__ import annotations

import json

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

    canonical, normalized = op_contract._canonical_contract(value)
    contract_sha = op_contract._contract_sha256(canonical)
    result = op_contract._check_forbidden(normalized, tmp_path, contract_sha)

    assert result["status"] == "UNVERIFIED"
    assert "no coordinator-verifiable forbidden-action evidence" in result["detail"]


def test_fabric_observer_failure_cannot_be_masked_by_other_runner_completion(
    tmp_path, monkeypatch
):
    value = contract(tmp_path)
    value["forbidden_actions"] = [
        {"action": "public_publish", "reason": "must remain private", "class": "HIGH"}
    ]
    canonical, normalized = op_contract._canonical_contract(value)
    contract_sha = op_contract._contract_sha256(canonical)

    monkeypatch.setattr(op_contract, "_observed_audit", list)
    monkeypatch.setattr(
        op_contract,
        "_observed_runs",
        lambda task_id, _hermes_root: [
            {
                "task_id": task_id,
                "status": "completed",
                "outcome": "completed",
                "scope": "runner:pi_rpc",
            }
        ],
    )

    class BrokenFabricBackend:
        def observed_runs(self, task_id, *, hermes_root=None):
            raise OSError("fabric observation unavailable")

    original_get_backend = op_contract.op_runners.get_backend
    monkeypatch.setattr(
        op_contract.op_runners,
        "get_backend",
        lambda name: BrokenFabricBackend() if name == "fabric" else original_get_backend(name),
    )

    verdict = op_contract._validate_impl(normalized, contract_sha, None, tmp_path)
    forbidden = next(check for check in verdict["checks"] if check["kind"] == "forbidden")

    assert forbidden["status"] == "UNVERIFIED"
    assert "remote Fabric execution" in forbidden["detail"]
    assert verdict["verdict"] != "SATISFIED"
    assert verdict["satisfied"] is False


def test_auto_remote_lineage_fails_closed_when_fabric_observer_is_unavailable(
    tmp_path, monkeypatch
):
    value = contract(tmp_path)
    value["assigned_agent"] = "auto"
    value["execution"] = {"backend": "auto", "options": {}}
    value["forbidden_actions"] = [
        {"action": "public_publish", "reason": "must remain private", "class": "HIGH"}
    ]
    canonical, normalized = op_contract._canonical_contract(value)
    contract_sha = op_contract._contract_sha256(canonical)

    journal = tmp_path / "fabric" / "routing-decisions.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "schema": "hermes.fabric-routing-decision/v1",
                "task_id": normalized["task_id"],
                "original_contract_sha256": contract_sha,
                "selected": {
                    "node": "node-a",
                    "backend": "fake",
                    "transport_backend": "fabric",
                    "remote": True,
                },
            }
        )
        + "\n"
    )
    monkeypatch.setattr(op_contract, "_observed_audit", list)
    monkeypatch.setattr(
        op_contract,
        "_observed_runs",
        lambda task_id, _hermes_root: [
            {
                "task_id": task_id,
                "status": "completed",
                "outcome": "completed",
                "scope": "runner:pi_rpc",
            }
        ],
    )

    class BrokenFabricBackend:
        def observed_runs(self, task_id, *, hermes_root=None):
            raise OSError("fabric observation unavailable")

    original_get_backend = op_contract.op_runners.get_backend
    monkeypatch.setattr(
        op_contract.op_runners,
        "get_backend",
        lambda name: BrokenFabricBackend() if name == "fabric" else original_get_backend(name),
    )

    verdict = op_contract._validate_impl(normalized, contract_sha, None, tmp_path)
    forbidden = next(check for check in verdict["checks"] if check["kind"] == "forbidden")

    assert forbidden["status"] == "UNVERIFIED"
    assert "remote Fabric auto placement" in forbidden["detail"]
    assert verdict["verdict"] != "SATISFIED"
    assert verdict["satisfied"] is False


def test_auto_local_lineage_can_still_pass_forbidden_check(tmp_path, monkeypatch):
    value = contract(tmp_path)
    value["assigned_agent"] = "auto"
    value["execution"] = {"backend": "auto", "options": {}}
    value["forbidden_actions"] = [
        {"action": "public_publish", "reason": "must remain private", "class": "HIGH"}
    ]
    canonical, normalized = op_contract._canonical_contract(value)
    contract_sha = op_contract._contract_sha256(canonical)

    journal = tmp_path / "fabric" / "routing-decisions.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "schema": "hermes.fabric-routing-decision/v1",
                "task_id": normalized["task_id"],
                "original_contract_sha256": contract_sha,
                "selected": {
                    "node": "local",
                    "backend": "pi_rpc",
                    "transport_backend": "pi_rpc",
                    "remote": False,
                },
            }
        )
        + "\n"
    )
    monkeypatch.setattr(op_contract, "_observed_audit", list)

    class EmptyFabricBackend:
        def observed_runs(self, task_id, *, hermes_root=None):
            return []

    original_get_backend = op_contract.op_runners.get_backend
    monkeypatch.setattr(
        op_contract.op_runners,
        "get_backend",
        lambda name: EmptyFabricBackend() if name == "fabric" else original_get_backend(name),
    )

    result = op_contract._check_forbidden(normalized, tmp_path, contract_sha)

    assert result["status"] == "PASS"
