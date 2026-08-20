from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_contract as op_contract
import operator_fabric as fabric
import operator_runners as op_runners
from test_operator_fabric import (
    accept_request,
    capability,
    contract,
    coordinator,
    envelope_for,
    node,
    policy,
    rpc_for,
    service,
)


def test_authorized_remote_write_happy_path_returns_admissible_run(tmp_path, monkeypatch):
    observed = []
    svc = service(tmp_path, monkeypatch, observed=observed)
    coord = coordinator(tmp_path, svc)
    result = coord.dispatch(
        contract(tmp_path, auth_class="reversible_write"),
        dry_run=False,
        confirm=True,
        timeout=10,
    )
    assert result["success"] is True
    observed.append(
        {
            "status": "completed",
            "outcome": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:01:00Z",
            "error": "",
        }
    )
    status = coord.poll(result["attempt_id"], reconcile=True)
    assert status["peer_state"] == "SUCCEEDED"
    evidence = coord.collect(result["attempt_id"])
    assert evidence["state"] == "COMPLETED"
    runs = coord.observed_runs("task-fabric-1", refresh=False)
    assert runs[0]["outcome"] == "completed"
    assert runs[0]["evidence_provenance"] == "managed_peer_structured"


def test_unknown_node_and_unauthorized_profile_fail_closed(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    coord = coordinator(tmp_path, svc)
    value = contract(tmp_path)
    value["execution"]["options"]["node"] = "node-missing"
    with pytest.raises(fabric.FabricError) as exc:
        coord.dispatch(value, dry_run=False, confirm=True, timeout=10)
    assert exc.value.code == "FABRIC_NODE_NOT_ENROLLED"

    value = contract(tmp_path)
    value["assigned_profile"] = "other"
    with pytest.raises(fabric.FabricError) as exc:
        coord.dispatch(value, dry_run=False, confirm=True, timeout=10)
    assert exc.value.code == "FABRIC_AUTHORITY_DENIED"


def test_oversized_request_and_unsupported_major_are_rejected(tmp_path, monkeypatch):
    with pytest.raises(fabric.FabricError) as exc:
        fabric.strict_json_loads(b"x" * (128 * 1024 + 1))
    assert exc.value.code == "FABRIC_PAYLOAD_TOO_LARGE"

    svc = service(tmp_path, monkeypatch)
    request = fabric._request("capabilities", "coord-main", data={})
    request["version"] = 2
    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(request, "Bearer 0123456789abcdef0123456789abcdef")
    assert exc.value.code == "FABRIC_PROTOCOL_INCOMPATIBLE"


def test_missing_required_feature_is_rejected_before_runner_start(tmp_path, monkeypatch):
    counter = {"count": 0}
    svc = service(tmp_path, monkeypatch, dispatch_counter=counter)
    envelope = envelope_for(svc, contract(tmp_path))
    envelope["required_features"] = [*envelope["required_features"], "future-feature-v9"]
    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(
            accept_request(envelope),
            "Bearer 0123456789abcdef0123456789abcdef",
        )
    assert exc.value.code == "FABRIC_PROTOCOL_INCOMPATIBLE"
    assert counter["count"] == 0


def test_coordinator_rejects_capability_identity_drift(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)

    def drift_rpc(target, request, timeout):
        remote_task_id, response = rpc_for(svc)(target, request, timeout)
        if request["operation"] == "capabilities":
            response = dict(response)
            data = dict(response["data"])
            data["identity"] = "spoofed-peer"
            data["snapshot_sha256"] = fabric.sha256_json(
                {key: data[key] for key in data if key != "snapshot_sha256"}
            )
            response["data"] = data
        return remote_task_id, response

    coord = coordinator(tmp_path, svc, rpc=drift_rpc)
    with pytest.raises(fabric.FabricError) as exc:
        coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    assert exc.value.code == "FABRIC_PROTOCOL_INCOMPATIBLE"


def test_peer_unavailable_before_accept_does_not_create_ambiguous_submit(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)

    def unavailable_rpc(_target, request, _timeout):
        if request["operation"] == "capabilities":
            raise fabric.FabricError(
                "FABRIC_PEER_UNAVAILABLE",
                "peer unavailable",
                ambiguous=False,
            )
        raise AssertionError("accept must not run after failed capability probe")

    coord = coordinator(tmp_path, svc, rpc=unavailable_rpc)
    with pytest.raises(fabric.FabricError) as exc:
        coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    assert exc.value.code == "FABRIC_PEER_UNAVAILABLE"


def test_timeout_after_remote_accept_recovers_same_attempt_after_coordinator_restart(
    tmp_path,
    monkeypatch,
):
    observed = []
    svc = service(tmp_path, monkeypatch, observed=observed)
    calls = {"accept": 0}

    def timeout_after_accept(target, request, timeout):
        if request["operation"] == "accept":
            calls["accept"] += 1
            rpc_for(svc)(target, request, timeout)
            raise fabric.FabricError(
                "FABRIC_TRANSPORT_TIMEOUT",
                "response lost after peer accepted",
                ambiguous=True,
            )
        return rpc_for(svc)(target, request, timeout)

    first = coordinator(tmp_path, svc, rpc=timeout_after_accept)
    submitted = first.dispatch(
        contract(tmp_path),
        dry_run=False,
        confirm=True,
        timeout=10,
    )
    assert submitted["state"] == "SUBMISSION_AMBIGUOUS"
    assert submitted["submission_may_have_succeeded"] is True
    assert calls["accept"] == 1

    restarted = coordinator(tmp_path, svc)
    status = restarted.poll(submitted["attempt_id"], reconcile=True)
    assert status["state"] == "RUNNING"
    assert status["attempt_id"] == submitted["attempt_id"]
    assert calls["accept"] == 1

    observed.append(
        {
            "status": "completed",
            "outcome": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:01:00Z",
            "error": "",
        }
    )
    terminal = restarted.poll(submitted["attempt_id"], reconcile=True)
    assert terminal["peer_state"] == "SUCCEEDED"
    admitted = restarted.collect(submitted["attempt_id"])
    assert admitted["state"] == "COMPLETED"


def test_a2a_task_mapping_loss_does_not_erase_fabric_identity(tmp_path, monkeypatch):
    observed = []
    svc = service(tmp_path, monkeypatch, observed=observed)
    coord = coordinator(tmp_path, svc)
    result = coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    with fabric._connect(coord.db_path) as db:
        db.execute(
            "UPDATE attempts SET remote_task_id=NULL WHERE attempt_id=?",
            (result["attempt_id"],),
        )
    observed.append(
        {
            "status": "completed",
            "outcome": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:01:00Z",
            "error": "",
        }
    )
    status = coord.poll(result["attempt_id"], reconcile=True)
    assert status["peer_state"] == "SUCCEEDED"
    assert coord.collect(result["attempt_id"])["state"] == "COMPLETED"


def test_legacy_fleet_completion_bundle_is_not_fabric_evidence(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    coord = coordinator(tmp_path, svc)
    result = coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    attempt, _dispatch, target = coord._attempt(result["attempt_id"])
    attempt_map = dict(attempt)
    attempt_map["_coordinator_db"] = str(coord.db_path)
    with pytest.raises(fabric.FabricError) as exc:
        fabric._validate_evidence(
            {"completion_bundle": {"status": "completed", "task_id": "task-fabric-1"}},
            attempt=attempt_map,
            node=target,
            allowed_provenance=("managed_peer_structured",),
        )
    assert exc.value.code == "FABRIC_SCHEMA_INVALID"


def test_existing_contract_validator_consumes_only_pre_admitted_fabric_evidence(
    tmp_path,
    monkeypatch,
):
    observed = []
    svc = service(tmp_path, monkeypatch, observed=observed)
    coord = coordinator(tmp_path, svc)

    raw = contract(tmp_path)
    canonical, normalized = op_contract._canonical_contract(raw)
    result = coord.dispatch(normalized, dry_run=False, confirm=True, timeout=10)
    observed.append(
        {
            "status": "completed",
            "outcome": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:01:00Z",
            "error": "",
        }
    )
    coord.poll(result["attempt_id"], reconcile=True)
    coord.collect(result["attempt_id"])

    previous = op_runners.get_backend("fabric")
    op_runners.register_backend(
        fabric.FabricBackend(coordinator_factory=lambda **_kwargs: coord),
        replace=True,
    )
    before = {
        path.relative_to(tmp_path): path.stat().st_mtime_ns
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    try:
        payload = json.loads(
            op_contract.hermes_contract_validate(
                canonical,
                hermes_root=tmp_path,
            )
        )
    finally:
        op_runners.register_backend(previous, replace=True)
    after = {
        path.relative_to(tmp_path): path.stat().st_mtime_ns
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert payload["verdict"] == "SATISFIED"
    assert payload["satisfied"] is True
    assert any(run.get("scope") == "fabric:node-a" for run in payload["evidence"]["run"])
    # Contract validation may append its existing operator audit record, but it
    # must not mutate either Fabric SQLite journal while merely observing proof.
    for relative, mtime in before.items():
        if relative.name in {"coord.db", "peer.db", "coord.db-wal", "peer.db-wal"}:
            assert after.get(relative) == mtime


def test_peer_policy_identity_and_node_binding_cannot_be_self_widened(tmp_path, monkeypatch):
    alternate = policy(tmp_path)
    envelope = fabric._build_envelope(
        contract(tmp_path),
        node(),
        remote_backend="fake",
        logical_workspace="repo",
        remote_options={},
        evidence_policy={"run_state": ("managed_peer_structured",)},
        capability_sha="0" * 64,
    )
    envelope["target_node"] = "node-b"
    svc = service(tmp_path, monkeypatch, policy_loader=lambda: alternate)
    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(
            accept_request(envelope),
            "Bearer 0123456789abcdef0123456789abcdef",
        )
    assert exc.value.code == "FABRIC_NODE_IDENTITY_MISMATCH"
