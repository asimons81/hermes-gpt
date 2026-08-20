from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

import operator_contract as op_contract
import operator_fabric as base
import operator_fabric_g4c as fabric
import operator_fabric_router as router
import operator_fabric_view as view
import operator_runners as runners
from test_operator_fabric_g4c import (
    auto_contract,
    claim_for,
    contract,
    facts,
    make_coordinator,
    make_service,
    node,
    policy,
    row_for,
    rpc_for,
)


def _completed_observation() -> dict[str, str]:
    return {
        "status": "completed",
        "outcome": "completed",
        "started_at": "2026-08-20T15:00:00Z",
        "ended_at": "2026-08-20T15:01:00Z",
        "error": "",
    }


def _router_for_service(tmp_path, svc, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    now = datetime.now(timezone.utc)

    def probe(_node, _timeout):
        snapshot = svc.capabilities(policy(tmp_path))
        return {
            "healthy": True,
            "latency_ms": 5.0,
            "snapshot_sha256": snapshot["snapshot_sha256"],
            "features": list(snapshot.get("features") or []),
        }

    return fabric.AutoRouter(
        registry_loader=lambda: {"node-a": node()},
        routing_policy_loader=lambda: router.RoutingPolicy(targets={"node-a": facts(now)}),
        local_backends=list,
        remote_probe=probe,
        now=lambda: now,
        hermes_root=tmp_path,
    )


def _auto_backend(tmp_path, svc, coord, monkeypatch):
    route = _router_for_service(tmp_path, svc, monkeypatch)
    captured: dict[str, dict] = {}

    def downstream(placed, *, confirm, dry_run, timeout, hermes_root=None, **_kwargs):
        captured["contract"] = placed
        return coord.dispatch(
            placed,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
        )

    return (
        router.AutoBackend(
            router_factory=lambda **_kwargs: route,
            dispatch_fn=downstream,
        ),
        captured,
    )


def _bind_view(tmp_path, coord, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(base.COORDINATOR_DB_ENV, str(coord.db_path))
    monkeypatch.setattr(view.fabric, "load_node_registry", lambda **_kwargs: {"node-a": node()})


def _validate_with_coordinator(canonical: str, coord, tmp_path):
    previous = runners.get_backend("fabric")
    runners.register_backend(
        base.FabricBackend(coordinator_factory=lambda **_kwargs: coord),
        replace=True,
    )
    try:
        return json.loads(
            op_contract.hermes_contract_validate(
                canonical,
                hermes_root=tmp_path,
            )
        )
    finally:
        runners.register_backend(previous, replace=True)


def test_auto_remote_dispatch_evidence_contract_and_flight_deck_compose(tmp_path, monkeypatch):
    real_get_backend = runners.get_backend
    observed: list[dict[str, str]] = []
    svc = make_service(tmp_path, monkeypatch, observed=observed)
    coord = make_coordinator(tmp_path, svc)
    backend, captured = _auto_backend(tmp_path, svc, coord, monkeypatch)

    value = auto_contract(tmp_path)
    result = backend.dispatch(
        value,
        confirm=True,
        dry_run=False,
        timeout=10,
        hermes_root=tmp_path,
    )
    assert result["success"] is True
    assert result["selected_node"] == "node-a"
    assert result["selected_backend"] == "pi_rpc"
    placed = captured["contract"]
    assert placed["execution"]["backend"] == "fabric"
    assert placed["execution"]["options"]["node"] == "node-a"

    observed.append(_completed_observation())
    status = coord.poll(result["attempt_id"], reconcile=True)
    assert status["peer_state"] == "SUCCEEDED"
    admitted = coord.collect(result["attempt_id"])
    assert admitted["state"] == "COMPLETED"

    _bind_view(tmp_path, coord, monkeypatch)
    detail = view.attempt_detail(result["attempt_id"], hermes_root=tmp_path)
    assert detail["success"] is True
    attempt = detail["attempt"]
    assert attempt["state"] == "COMPLETED"
    assert attempt["placement_mode"] == "auto"
    assert attempt["routing"]["explanation_available"] is True
    assert attempt["routing"]["selected"]["node"] == "node-a"
    assert attempt["evidence"]["observations"][0]["provenance"] == "managed_peer_structured"
    assert attempt["authority"]["granted"] == "read_only_or_none"

    monkeypatch.setattr(runners, "get_backend", real_get_backend)
    canonical, _normalized = op_contract._canonical_contract(placed)
    verdict = _validate_with_coordinator(canonical, coord, tmp_path)
    assert verdict["verdict"] == "SATISFIED"
    assert verdict["satisfied"] is True
    assert any(item.get("scope") == "fabric:node-a" for item in verdict["evidence"]["run"])


def test_auto_read_only_runner_options_cannot_widen_remote_authority(tmp_path, monkeypatch):
    observed: list[dict[str, str]] = []
    svc = make_service(tmp_path, monkeypatch, observed=observed)
    coord = make_coordinator(tmp_path, svc)
    backend, captured = _auto_backend(tmp_path, svc, coord, monkeypatch)

    value = auto_contract(tmp_path, auth="read_only")
    value["execution"]["options"]["runner_options"] = {"sandbox": "workspace-write"}
    result = backend.dispatch(
        value,
        confirm=True,
        dry_run=False,
        timeout=10,
        hermes_root=tmp_path,
    )
    assert result["success"] is True
    placed = captured["contract"]
    assert placed["authorization"]["class"] == "read_only"
    assert placed["execution"]["options"]["remote_options"]["sandbox"] == "workspace-write"

    peer_row = row_for(svc, result["attempt_id"])
    assert peer_row["write_epoch"] is None
    assert claim_for(svc) is None

    _bind_view(tmp_path, coord, monkeypatch)
    detail = view.attempt_detail(result["attempt_id"], hermes_root=tmp_path)
    assert detail["attempt"]["authority"]["granted"] == "read_only_or_none"


def test_timeout_after_accept_restart_reconciles_same_attempt_and_view(tmp_path, monkeypatch):
    observed: list[dict[str, str]] = []
    svc = make_service(tmp_path, monkeypatch, observed=observed)
    calls = {"accept": 0}

    def timeout_after_accept(target, request, timeout):
        if request["operation"] == "accept":
            calls["accept"] += 1
            rpc_for(svc)(target, request, timeout)
            raise base.FabricError(
                "FABRIC_TRANSPORT_TIMEOUT",
                "response lost after accepted submit",
                ambiguous=True,
            )
        return rpc_for(svc)(target, request, timeout)

    first = make_coordinator(tmp_path, svc, rpc=timeout_after_accept)
    value = contract(tmp_path)
    submitted = first.dispatch(value, dry_run=False, confirm=True, timeout=10)
    assert submitted["state"] == "SUBMISSION_AMBIGUOUS"
    assert submitted["submission_may_have_succeeded"] is True
    assert calls["accept"] == 1

    _bind_view(tmp_path, first, monkeypatch)
    ambiguous = view.attempt_detail(submitted["attempt_id"], hermes_root=tmp_path)
    assert ambiguous["attempt"]["state"] == "SUBMISSION_AMBIGUOUS"
    assert ambiguous["attempt"]["blocker"] == "FABRIC_TRANSPORT_TIMEOUT"

    observed.append(
        {
            "status": "running",
            "outcome": "running",
            "started_at": "2026-08-20T15:00:00Z",
            "ended_at": "",
            "error": "",
        }
    )
    restarted = make_coordinator(tmp_path, svc)
    running = restarted.poll(submitted["attempt_id"], reconcile=True)
    assert running["state"] == "RUNNING"
    assert running["attempt_id"] == submitted["attempt_id"]
    assert calls["accept"] == 1

    _bind_view(tmp_path, restarted, monkeypatch)
    running_view = view.attempt_detail(submitted["attempt_id"], hermes_root=tmp_path)
    assert running_view["attempt"]["state"] == "RUNNING"

    observed[:] = [_completed_observation()]
    terminal = restarted.poll(submitted["attempt_id"], reconcile=True)
    assert terminal["peer_state"] == "SUCCEEDED"
    assert restarted.collect(submitted["attempt_id"])["state"] == "COMPLETED"
    completed_view = view.attempt_detail(submitted["attempt_id"], hermes_root=tmp_path)
    assert completed_view["attempt"]["state"] == "COMPLETED"


def test_write_retry_cannot_overlap_and_epoch_moves_only_after_cancel(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)
    coord = make_coordinator(tmp_path, svc)
    value = contract(tmp_path, auth="reversible_write")

    first = coord.dispatch(value, dry_run=False, confirm=True, timeout=10)
    old_row = row_for(svc, first["attempt_id"])
    assert old_row["write_epoch"] == 1
    assert claim_for(svc)["attempt_id"] == first["attempt_id"]

    with pytest.raises(base.FabricError) as exc:
        coord.retry(value, first["attempt_id"], confirm=True, timeout=10)
    assert exc.value.code == "FABRIC_WRITE_OWNERSHIP_BLOCKED"
    assert claim_for(svc)["attempt_id"] == first["attempt_id"]

    cancelled = coord.cancel(first["attempt_id"], timeout=10)
    assert cancelled["state"] == "CANCELLED"
    assert claim_for(svc)["state"] == "RELEASED"

    second = coord.retry(value, first["attempt_id"], confirm=True, timeout=10)
    assert second["success"] is True
    assert second["retry_parent_attempt_id"] == first["attempt_id"]
    assert second["write_epoch"] == 2
    active = claim_for(svc)
    assert active["attempt_id"] == second["attempt_id"]
    assert active["state"] == "ACTIVE"
    assert active["epoch"] == 2
    assert svc.claims.release(old_row, proof="stale-g5-release") is False

    _bind_view(tmp_path, coord, monkeypatch)
    old_view = view.attempt_detail(first["attempt_id"], hermes_root=tmp_path)
    new_view = view.attempt_detail(second["attempt_id"], hermes_root=tmp_path)
    assert old_view["attempt"]["state"] == "CANCELLED"
    assert new_view["attempt"]["retry_parent_attempt_id"] == first["attempt_id"]
    assert new_view["attempt"]["authority"]["write_epoch"] == 2


def test_remote_artifact_hash_is_admitted_and_active_content_stays_metadata_only(tmp_path, monkeypatch):
    content = b"<html><body>remote artifact</body></html>"
    (tmp_path / "out.html").write_bytes(content)
    observed = [_completed_observation()]
    svc = make_service(tmp_path, monkeypatch, observed=observed)
    coord = make_coordinator(tmp_path, svc)
    value = contract(
        tmp_path,
        artifacts=[{"path": "out.html", "must_exist": True, "min_bytes": 1}],
    )

    result = coord.dispatch(value, dry_run=False, confirm=True, timeout=10)
    coord.poll(result["attempt_id"], reconcile=True)
    collected = coord.collect(result["attempt_id"], timeout=10)
    assert collected["state"] == "COMPLETED"
    assert len(collected["artifacts"]) == 1
    assert collected["artifacts"][0]["sha256"] == hashlib.sha256(content).hexdigest()

    _bind_view(tmp_path, coord, monkeypatch)
    detail = view.attempt_detail(result["attempt_id"], hermes_root=tmp_path)
    artifact = detail["attempt"]["artifacts"][0]
    assert artifact["logical_name"] == "out.html"
    assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
    assert artifact["active_content"] is True
    assert artifact["render_policy"] == "isolated_metadata_only"
    assert "admission_path" not in artifact


def test_remote_completion_cannot_self_satisfy_required_human_review(tmp_path, monkeypatch):
    real_get_backend = runners.get_backend
    observed = [_completed_observation()]
    svc = make_service(tmp_path, monkeypatch, observed=observed)
    coord = make_coordinator(tmp_path, svc)
    value = contract(tmp_path)
    value["review_requirements"] = {
        "required": True,
        "reviewer": "owner",
        "evidence": "owner acceptance required",
        "approval_required": True,
    }
    value["completion_criteria"]["review_satisfied"] = True
    canonical, normalized = op_contract._canonical_contract(value)

    result = coord.dispatch(normalized, dry_run=False, confirm=True, timeout=10)
    coord.poll(result["attempt_id"], reconcile=True)
    assert coord.collect(result["attempt_id"])["state"] == "COMPLETED"

    monkeypatch.setattr(runners, "get_backend", real_get_backend)
    verdict = _validate_with_coordinator(canonical, coord, tmp_path)
    by_kind = {item["kind"]: item["status"] for item in verdict["checks"]}
    assert by_kind["run_state"] == "PASS"
    assert by_kind["review"] != "PASS"
    assert verdict["verdict"] == "NOT_SATISFIED"
    assert verdict["satisfied"] is False
