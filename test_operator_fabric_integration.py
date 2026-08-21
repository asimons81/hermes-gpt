from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import operator_contract as op_contract
import operator_fabric as base
import operator_fabric_g4c as fabric
import operator_fabric_router as router
import operator_fabric_view as view
import operator_policy as op
import operator_runners as runners
import operator_swarm as swarm
from test_operator_fabric_g4c import (
    FakeUnitManager,
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


class _ImmediateBackend:
    name = "pi_rpc"

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, str]] = {}

    def availability(self, *, hermes_root=None):
        return {"available": True, "enabled": True, "write_enabled": False}

    def dispatch(self, contract, *, confirm, dry_run, timeout, hermes_root=None, **_kwargs):
        task_id = str(contract["task_id"])
        if not dry_run:
            self.runs[task_id] = {
                "task_id": task_id,
                "status": "completed",
                "outcome": "completed",
                "error": "",
                "started_at": "2026-08-20T15:00:00Z",
                "ended_at": "2026-08-20T15:01:00Z",
                "scope": "runner:pi_rpc",
            }
        return {
            "success": True,
            "ok": True,
            "changed": not dry_run,
            "dry_run": dry_run,
            "backend": self.name,
            "task_id": task_id,
        }

    def observed_runs(self, task_id, *, hermes_root=None):
        value = self.runs.get(task_id)
        return [dict(value)] if value else []

    def cancel(self, task_id, *, hermes_root=None):
        return {
            "success": True,
            "changed": False,
            "backend": self.name,
            "task_id": task_id,
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
    try:
        previous = runners.get_backend("fabric")
    except LookupError:
        previous = None
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
        if previous is not None:
            runners.register_backend(previous, replace=True)
        else:
            with runners._REGISTRY_LOCK:
                runners._BACKENDS.pop("fabric", None)


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


def test_auto_local_dispatch_composes_while_remote_candidate_is_available(tmp_path, monkeypatch):
    real_get_backend = runners.get_backend
    svc = make_service(tmp_path, monkeypatch)
    monkeypatch.setattr(runners, "get_backend", real_get_backend)
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

    route = fabric.AutoRouter(
        registry_loader=lambda: {"node-a": node()},
        routing_policy_loader=lambda: router.RoutingPolicy(
            targets={"local": facts(now), "node-a": facts(now)}
        ),
        local_backends=lambda: ["pi_rpc"],
        local_posture=lambda _dry: {"ready": True, "max_authorization": "high_impact"},
        remote_probe=probe,
        now=lambda: now,
        hermes_root=tmp_path,
    )
    captured: dict[str, dict] = {}

    def downstream(placed, *, confirm, dry_run, timeout, hermes_root=None, **_kwargs):
        captured["contract"] = placed
        return {
            "success": True,
            "ok": True,
            "changed": not dry_run,
            "dry_run": dry_run,
            "backend": "pi_rpc",
            "task_id": placed["task_id"],
        }

    backend = router.AutoBackend(
        router_factory=lambda **_kwargs: route,
        dispatch_fn=downstream,
    )
    value = auto_contract(tmp_path)
    value["execution"]["options"]["preferences"]["prefer_local"] = True

    result = backend.dispatch(
        value,
        confirm=True,
        dry_run=False,
        timeout=10,
        hermes_root=tmp_path,
    )

    assert result["success"] is True
    assert result["selected_node"] == "local"
    assert result["selected_backend"] == "pi_rpc"
    assert result["placement"]["selected"]["remote"] is False
    assert captured["contract"]["execution"]["backend"] == "pi_rpc"
    assert captured["contract"]["authorization"] == value["authorization"]
    remote = next(
        item
        for item in result["placement"]["candidates"]
        if item["node"] == "node-a" and item["backend"] == "pi_rpc"
    )
    assert remote["eligible"] is True


def test_auto_read_only_runner_options_cannot_widen_remote_authority(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "must-not-change.txt"
    marker.write_text("original", encoding="utf-8")
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text("#!/bin/sh\nprintf mutated > must-not-change.txt\n", encoding="utf-8")
    fake_pi.chmod(0o755)

    monkeypatch.setenv("HERMES_GPT_PI_EXE", str(fake_pi))
    monkeypatch.setenv("HERMES_GPT_OPERATOR_ENABLED", "1")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_LEVEL", "workspace")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_APPLY_MODE", "direct")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_ALLOWED_PATHS", str(workspace))
    monkeypatch.setenv("HERMES_GPT_OPERATOR_ALLOWED_PROFILES", "default")
    launched = False

    def must_not_launch(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("read-only Fabric work reached process launch")

    monkeypatch.setattr(runners, "_popen_process_group", must_not_launch)

    def must_not_confine(*_args, **_kwargs):
        raise AssertionError("writable Pi request reached filesystem confinement")

    monkeypatch.setattr(runners.confinement, "confinement_available", must_not_confine)
    monkeypatch.setattr(runners.confinement, "wrap_argv", must_not_confine)

    svc = fabric.FabricPeerService(
        policy_loader=lambda: policy(workspace),
        tokens={"coord-main": "0123456789abcdef0123456789abcdef"},
        db_path=tmp_path / "peer.db",
        observed_fn=lambda _task_id: [],
        artifact_root=tmp_path / "snapshots",
        hermes_root=tmp_path,
    )
    coord = make_coordinator(tmp_path, svc)
    backend, captured = _auto_backend(workspace, svc, coord, monkeypatch)

    value = auto_contract(workspace, auth="read_only")
    value["execution"]["options"]["runner_options"] = {
        "sandbox": "workspace-write",
        "tools": "read,write",
    }
    result = backend.dispatch(
        value,
        confirm=True,
        dry_run=False,
        timeout=10,
        hermes_root=tmp_path,
    )

    placed = captured["contract"]
    assert result["success"] is False
    assert result["code"] == "RUNNER_DISPATCH_ERROR"
    assert placed["authorization"]["class"] == "read_only"
    assert placed["execution"]["options"]["remote_options"]["sandbox"] == "workspace-write"
    assert launched is False
    assert marker.read_text(encoding="utf-8") == "original"

    with base._connect_readonly(svc.db_path) as db:
        peer_row = db.execute("SELECT * FROM attempts").fetchone()
    assert peer_row is not None
    dispatch_result = json.loads(peer_row["dispatch_result_json"])
    assert "read-only authorization" in dispatch_result["safe_message"]
    assert peer_row["write_epoch"] is None
    assert claim_for(svc) is None


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


def test_restart_loss_observations_preserve_uncertain_writer_ownership(tmp_path, monkeypatch):
    observed: list[dict[str, str]] = []
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, observed=observed, unit=unit)
    first_coord = make_coordinator(tmp_path, svc)
    value = contract(tmp_path, auth="reversible_write")
    first = first_coord.dispatch(value, dry_run=False, confirm=True, timeout=10)
    first_peer_row = row_for(svc, first["attempt_id"])
    unit.activate(first_peer_row["execution_unit_id"])

    with base._connect(first_coord.db_path) as db:
        db.execute(
            "UPDATE attempts SET write_epoch=999 WHERE attempt_id=?",
            (first["attempt_id"],),
        )

    restarted = make_coordinator(tmp_path, svc)
    active = restarted.poll(first["attempt_id"], reconcile=True)
    assert active["write_epoch"] == 1
    assert active["write_claim_state"] == "ACTIVE"
    assert active["execution_unit_state"] == "active"

    unit.quiesce(first_peer_row["execution_unit_id"])
    lost = restarted.poll(first["attempt_id"], reconcile=True)
    assert lost["peer_state"] == "LOST_AMBIGUOUS"
    assert lost["state"] == "BLOCKED"
    assert lost["write_claim_state"] == "RELEASED"
    assert lost["execution_unit_state"] == "inactive"

    second = restarted.retry(value, first["attempt_id"], confirm=True, timeout=10)
    superseded = restarted.poll(first["attempt_id"], reconcile=True)
    assert superseded["write_claim_state"] == "SUPERSEDED"
    assert second["write_epoch"] == 2

    second_peer_row = row_for(svc, second["attempt_id"])
    unit.units[second_peer_row["execution_unit_id"]] = {
        "known": False,
        "active": False,
        "quiescent": False,
        "state": "unknown",
    }
    unknown = restarted.poll(second["attempt_id"], reconcile=True)
    assert unknown["peer_state"] == "LOST_AMBIGUOUS"
    assert unknown["write_claim_state"] == "ACTIVE"
    assert unknown["execution_unit_state"] == "unknown"
    with pytest.raises(base.FabricError) as exc:
        restarted.retry(value, second["attempt_id"], confirm=True, timeout=10)
    assert exc.value.code == "FABRIC_WRITE_OWNERSHIP_BLOCKED"
    assert claim_for(svc)["attempt_id"] == second["attempt_id"]


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


def test_auto_artifact_evidence_maps_original_contract_and_rehashes_admission(tmp_path, monkeypatch):
    content = b"verified remote artifact"
    (tmp_path / "out.txt").write_bytes(content)
    observed = [_completed_observation()]
    svc = make_service(tmp_path, monkeypatch, observed=observed)
    coord = make_coordinator(tmp_path, svc)
    backend, _captured = _auto_backend(tmp_path, svc, coord, monkeypatch)
    value = auto_contract(
        tmp_path,
        artifacts=[{"path": "out.txt", "must_exist": True, "min_bytes": 1}],
    )

    result = backend.dispatch(
        value,
        confirm=True,
        dry_run=False,
        timeout=10,
        hermes_root=tmp_path,
    )
    assert result["success"] is True
    assert result["selected_node"] == "node-a"
    coord.poll(result["attempt_id"], reconcile=True)
    collected = coord.collect(result["attempt_id"], timeout=10)
    assert collected["state"] == "COMPLETED"

    admitted = coord.observed_artifacts(
        value["task_id"],
        contract_sha256=base.sha256_json(value),
    )
    assert len(admitted) == 1
    assert admitted[0]["logical_name"] == "out.txt"
    assert admitted[0]["sha256"] == hashlib.sha256(content).hexdigest()
    assert admitted[0]["provenance"] == "coordinator_verified_artifact"
    assert "admission_path" not in admitted[0]

    with base._connect_readonly(coord.db_path) as db:
        row = db.execute(
            "SELECT admission_path FROM artifact_admissions WHERE attempt_id=?",
            (result["attempt_id"],),
        ).fetchone()
    assert row is not None
    Path(row["admission_path"]).write_bytes(b"tampered")
    assert coord.observed_artifacts(
        value["task_id"],
        contract_sha256=base.sha256_json(value),
    ) == []


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


def test_mixed_local_remote_swarm_fan_out_fan_in_and_owner_approval_compose(tmp_path, monkeypatch):
    hermes_root = tmp_path / "hermes"
    workspace = tmp_path / "workspace"
    hermes_root.mkdir()
    workspace.mkdir()
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)

    real_get_backend = runners.get_backend
    remote_observed: list[dict[str, str]] = []
    svc = make_service(workspace, monkeypatch, observed=remote_observed)
    coord = make_coordinator(workspace, svc)
    monkeypatch.setattr(runners, "get_backend", real_get_backend)

    now = datetime.now(timezone.utc)

    def probe(_node, _timeout):
        snapshot = svc.capabilities(policy(workspace))
        return {
            "healthy": True,
            "latency_ms": 5.0,
            "snapshot_sha256": snapshot["snapshot_sha256"],
            "features": list(snapshot.get("features") or []),
        }

    route = fabric.AutoRouter(
        registry_loader=lambda: {"node-a": node()},
        routing_policy_loader=lambda: router.RoutingPolicy(
            targets={"local": facts(now), "node-a": facts(now)}
        ),
        local_backends=lambda: ["pi_rpc"],
        local_posture=lambda _dry: {"ready": True, "max_authorization": "high_impact"},
        remote_probe=probe,
        now=lambda: now,
        hermes_root=hermes_root,
    )
    local_backend = _ImmediateBackend()
    auto_backend = router.AutoBackend(router_factory=lambda **_kwargs: route)
    fabric_backend = base.FabricBackend(coordinator_factory=lambda **_kwargs: coord)

    previous: dict[str, object | None] = {}
    for name in ("pi_rpc", "auto", "fabric"):
        try:
            previous[name] = runners.get_backend(name)
        except LookupError:
            previous[name] = None
    runners.register_backend(local_backend, replace=True)
    runners.register_backend(auto_backend, replace=True)
    runners.register_backend(fabric_backend, replace=True)

    def stage(stage_id, kind, parents, execution, *, owner="default"):
        approval = kind == "approval"
        return {
            "id": stage_id,
            "kind": kind,
            "owner": "owner" if approval else owner,
            "parents": list(parents),
            "objective": f"G5 integration {stage_id}",
            "expected_artifacts": [],
            "tests": [],
            "review_requirements": {
                "required": False,
                "reviewer": "",
                "evidence": "",
                "approval_required": False,
            },
            "completion_criteria": {
                "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
                "artifacts_present": False,
                "tests_pass": False,
                "review_satisfied": approval,
                "no_forbidden_actions": True,
            },
            "authorization": {
                "class": "high_impact" if approval else "read_only",
                "approved": True,
                "approved_by": "owner",
                "approval_reference": "g5-integration",
            },
            **({"execution": execution} if execution is not None else {}),
        }

    local_auto = {
        "backend": "auto",
        "options": {
            "requirements": {"location": "local", "runners": ["pi_rpc"]},
            "preferences": {"prefer_local": True},
            "logical_workspace": "repo",
            "runner_options": {},
        },
    }
    remote_auto = {
        "backend": "auto",
        "options": {
            "requirements": {"location": "remote", "runners": ["pi_rpc"]},
            "preferences": {"prefer_local": False},
            "logical_workspace": "repo",
            "runner_options": {},
            "evidence_provenance": {"run_state": ["managed_peer_structured"]},
        },
    }
    explicit_local = {"backend": "pi_rpc", "options": {}}
    workflow = {
        "schema": "hermes.swarm-workflow/v1",
        "workflow_id": "sw-g5-mixed-001",
        "title": "G5 mixed Fabric integration",
        "workspace": str(workspace),
        "max_parallel": 2,
        "board_cap": 4,
        "stages": [
            stage("root", "single", [], explicit_local),
            stage("local_child", "parallel", ["root"], local_auto),
            stage("remote_child", "parallel", ["root"], remote_auto),
            stage("join", "single", ["local_child", "remote_child"], explicit_local),
            stage("human_approval", "approval", ["join"], None),
        ],
    }

    try:
        created = json.loads(
            swarm.hermes_swarm_workflow_create(
                json.dumps(workflow),
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert created["success"] is True

        root_dispatch = json.loads(
            swarm.hermes_swarm_stage_dispatch(
                workflow["workflow_id"],
                "root",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert root_dispatch["success"] is True
        assert root_dispatch["backend"] == "pi_rpc"
        root_advance = json.loads(
            swarm.hermes_swarm_stage_advance(
                workflow["workflow_id"],
                "root",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert root_advance["success"] is True

        local_dispatch = json.loads(
            swarm.hermes_swarm_stage_dispatch(
                workflow["workflow_id"],
                "local_child",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        remote_dispatch = json.loads(
            swarm.hermes_swarm_stage_dispatch(
                workflow["workflow_id"],
                "remote_child",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        if not local_dispatch["success"]:
            raise AssertionError(json.dumps(local_dispatch, indent=2))
        assert local_dispatch["requested_backend"] == "auto"
        assert local_dispatch["selected_node"] == "local"
        assert remote_dispatch["success"] is True
        assert remote_dispatch["requested_backend"] == "auto"
        assert remote_dispatch["selected_node"] == "node-a"
        assert remote_dispatch["selected_backend"] == "pi_rpc"

        blocked_join = json.loads(
            swarm.hermes_swarm_stage_dispatch(
                workflow["workflow_id"],
                "join",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert blocked_join["success"] is False
        assert blocked_join["code"] == "STAGE_NOT_READY"

        local_advance = json.loads(
            swarm.hermes_swarm_stage_advance(
                workflow["workflow_id"],
                "local_child",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert local_advance["success"] is True

        still_blocked = json.loads(
            swarm.hermes_swarm_stage_dispatch(
                workflow["workflow_id"],
                "join",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert still_blocked["success"] is False
        assert still_blocked["code"] == "STAGE_NOT_READY"

        remote_observed.append(_completed_observation())
        remote_status = coord.poll(remote_dispatch["attempt_id"], reconcile=True)
        assert remote_status["peer_state"] == "SUCCEEDED"
        remote_evidence = coord.collect(remote_dispatch["attempt_id"])
        assert remote_evidence["state"] == "COMPLETED"

        remote_advance = json.loads(
            swarm.hermes_swarm_stage_advance(
                workflow["workflow_id"],
                "remote_child",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert remote_advance["success"] is True

        join_dispatch = json.loads(
            swarm.hermes_swarm_stage_dispatch(
                workflow["workflow_id"],
                "join",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert join_dispatch["success"] is True
        join_advance = json.loads(
            swarm.hermes_swarm_stage_advance(
                workflow["workflow_id"],
                "join",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert join_advance["success"] is True
        assert join_advance["workflow_status"] == "awaiting_approval"

        approval_dispatch = json.loads(
            swarm.hermes_swarm_stage_dispatch(
                workflow["workflow_id"],
                "human_approval",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert approval_dispatch["success"] is False
        assert approval_dispatch["code"] == "APPROVAL_GATE"

        denied = json.loads(
            swarm.hermes_swarm_approve(
                workflow["workflow_id"],
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert denied["success"] is False
        assert denied["code"] == "SWARM_POLICY_DENIED"

        monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
        monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
        monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)
        approved = json.loads(
            swarm.hermes_swarm_approve(
                workflow["workflow_id"],
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
        )
        assert approved["success"] is True
        assert approved["workflow_status"] == "done"
        assert approved["approval"]["approved"] is True
    finally:
        for name, backend in previous.items():
            if backend is None:
                with runners._REGISTRY_LOCK:
                    runners._BACKENDS.pop(name, None)
            else:
                runners.register_backend(backend, replace=True)
