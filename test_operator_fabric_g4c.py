from __future__ import annotations

import base64
import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

import operator_fabric as base
import operator_fabric_g4c as fabric
import operator_fabric_router as base_router
import operator_runners as runners

TOKEN = "0123456789abcdef0123456789abcdef"


class FakeLocalBackend(runners._LocalProcessBackend):
    name = "pi_rpc"

    def executable(self):
        return "/bin/true"

    def build_plan(self, contract):
        return {"backend": self.name, "task_id": contract["task_id"]}


class FakeUnitManager:
    def __init__(self, *, available=True):
        self.usable = available
        self.units = {}
        self.stopped = []

    def available(self):
        return self.usable

    def unit_name(self, attempt_id):
        return f"unit-{attempt_id[-10:]}"

    def launch(self, unit, task_id, workspace, jobs_root):
        self.units[unit] = {"known": True, "active": True, "quiescent": False, "state": "active"}
        return {"accepted": True, "ambiguous": False, "code": "FABRIC_EXECUTION_UNIT_STARTED"}

    def inspect(self, unit):
        return dict(self.units.get(unit, {"known": False, "active": False, "quiescent": False, "state": "unknown"}))

    def stop(self, unit):
        self.stopped.append(unit)
        value = {"known": True, "active": False, "quiescent": True, "state": "inactive"}
        self.units[unit] = value
        return dict(value)

    def quiesce(self, unit):
        self.units[unit] = {"known": True, "active": False, "quiescent": True, "state": "inactive"}

    def activate(self, unit):
        self.units[unit] = {"known": True, "active": True, "quiescent": False, "state": "active"}



def policy(tmp_path: Path, *, backends=("pi_rpc",), conflict="workspace:repo"):
    mapping = fabric.WorkspaceMapping("repo", tmp_path.resolve(), "r1", conflict)
    raw = {"node": "node-a", "mapping": str(tmp_path.resolve()), "conflict": conflict}
    return fabric.FabricPeerPolicy(
        node_name="node-a",
        identity="Hermes GPT Fabric node-a",
        allowed_coordinator_principals=("coord-main",),
        allowed_profiles=("default",),
        max_authorization="high_impact",
        allowed_backends=tuple(backends),
        required_features=(),
        workspace_mappings={"repo": mapping},
        digest=fabric.sha256_json(raw),
    )



def node(*, backends=("pi_rpc",)):
    return fabric.FabricNode(
        name="node-a",
        a2a_peer_name="node-a-peer",
        expected_identity="Hermes GPT Fabric node-a",
        coordinator_principal="coord-main",
        enabled=True,
        allowed_profiles=("default",),
        max_authorization="high_impact",
        allowed_remote_backends=tuple(backends),
        logical_workspaces=("repo",),
        required_features=(),
    )



def contract(tmp_path: Path, *, auth="read_only", artifacts=None):
    authorization = {"class": auth, "approved": True}
    if auth == "high_impact":
        authorization.update({"approved_by": "owner", "approval_reference": "approval-1"})
    return {
        "schema": "hermes.work-contract/v1",
        "task_id": "task-fabric-g4c",
        "objective": "Perform bounded Fabric work.",
        "assigned_agent": "node-a",
        "assigned_profile": "default",
        "inputs": [],
        "constraints": [],
        "allowed_scope": {"workspaces": [str(tmp_path.resolve())], "profiles": ["default"]},
        "forbidden_actions": [],
        "expected_artifacts": artifacts or [],
        "tests": [],
        "review_requirements": {"required": False, "reviewer": "", "evidence": "", "approval_required": False},
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed"]},
            "artifacts_present": bool(artifacts),
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
        "authorization": authorization,
        "execution": {
            "backend": "fabric",
            "options": {
                "node": "node-a",
                "remote_backend": "pi_rpc",
                "logical_workspace": "repo",
                "remote_options": {},
                "evidence_provenance": {"run_state": ["managed_peer_structured"]},
            },
        },
    }



def make_service(tmp_path, monkeypatch, *, observed=None, unit=None, write_result=None):
    observed = observed if observed is not None else []
    unit = unit or FakeUnitManager()
    monkeypatch.setattr(runners, "get_backend", lambda _name: FakeLocalBackend())

    def read_dispatch(_contract, **_kwargs):
        return {"success": True, "changed": True, "backend": "pi_rpc"}

    def write_dispatch(_contract, **_kwargs):
        return dict(write_result or {"success": True, "changed": True, "backend": "pi_rpc"})

    return fabric.FabricPeerService(
        policy_loader=lambda: policy(tmp_path),
        tokens={"coord-main": TOKEN},
        db_path=tmp_path / "peer.db",
        dispatch_fn=read_dispatch,
        write_dispatch_fn=write_dispatch,
        observed_fn=lambda _task_id: list(observed),
        cancel_fn=lambda _backend, _task: {"success": True, "changed": True, "state": "cancelled"},
        unit_manager=unit,
        artifact_root=tmp_path / "snapshots",
        hermes_root=tmp_path,
    )



def rpc_for(service):
    def rpc(_node, request, _timeout):
        response = service.handle(request, f"Bearer {TOKEN}")
        attempt = request.get("attempt_id") or request.get("request_id")
        return f"ftask-{str(attempt)[-10:]}", response

    return rpc



def make_coordinator(tmp_path, service, *, rpc=None):
    return fabric.FabricCoordinator(
        registry_loader=lambda: {"node-a": node()},
        db_path=tmp_path / "coord.db",
        rpc=rpc or rpc_for(service),
        hermes_root=tmp_path,
    )



def envelope(service, value, *, sequence=1, retry_parent=None):
    cap = service.capabilities(policy(value_path(value)))
    env = base._build_envelope(
        value,
        node(),
        remote_backend="pi_rpc",
        logical_workspace="repo",
        remote_options={},
        evidence_policy={"run_state": ("managed_peer_structured",)},
        capability_sha=cap["snapshot_sha256"],
    )
    env["attempt_id"] = base._attempt_id(env["dispatch_id"], sequence)
    if retry_parent:
        env["retry_parent_attempt_id"] = retry_parent
    return env



def value_path(value):
    return Path(value["allowed_scope"]["workspaces"][0])



def accept(service, env):
    request = base._request(
        "accept",
        "coord-main",
        data={"envelope": env},
        dispatch_id=env["dispatch_id"],
        attempt_id=env["attempt_id"],
    )
    return service.handle(request, f"Bearer {TOKEN}")



def row_for(service, attempt_id):
    with base._connect_readonly(service.db_path) as db:
        return db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()



def claim_for(service, conflict="workspace:repo"):
    with base._connect_readonly(service.db_path) as db:
        return db.execute("SELECT * FROM write_claims WHERE conflict_domain=?", (conflict,)).fetchone()



def test_write_claim_blocks_overlapping_conflict_domain_and_is_idempotent(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)
    value = contract(tmp_path, auth="reversible_write")
    first = envelope(svc, value)
    response = accept(svc, first)
    replay = accept(svc, first)
    assert response["ok"] is True
    assert response["data"]["write_epoch"] == 1
    assert replay["code"] == "FABRIC_IDEMPOTENT_REPLAY"
    assert claim_for(svc)["attempt_id"] == first["attempt_id"]

    second = envelope(svc, value, sequence=2, retry_parent=first["attempt_id"])
    with pytest.raises(fabric.FabricError) as exc:
        accept(svc, second)
    assert exc.value.code == "FABRIC_WRITE_OWNERSHIP_BLOCKED"


def _blocked_prelaunch_accept(tmp_path, monkeypatch, *, available=True):
    entered = threading.Event()
    resume = threading.Event()
    launches: list[str] = []
    errors: list[BaseException] = []

    def availability(_self, **_kwargs):
        entered.set()
        assert resume.wait(5), "test did not release pre-launch validation"
        return {"available": available}

    monkeypatch.setattr(FakeLocalBackend, "availability", availability)
    svc = make_service(
        tmp_path,
        monkeypatch,
        write_result={"success": True, "changed": True, "backend": "pi_rpc"},
    )
    original_dispatch = svc.write_dispatch_fn

    def record_launch(contract, **kwargs):
        launches.append(kwargs["unit_id"])
        return original_dispatch(contract, **kwargs)

    svc.write_dispatch_fn = record_launch
    value = contract(tmp_path, auth="reversible_write")
    first = envelope(svc, value)

    def run_accept():
        try:
            accept(svc, first)
        except BaseException as exc:  # noqa: BLE001 - thread surfaces failures after join
            errors.append(exc)

    thread = threading.Thread(target=run_accept)
    thread.start()
    assert entered.wait(5), "accept did not reach the durable pre-launch window"
    return svc, value, first, launches, errors, resume, thread


def test_reconcile_during_prelaunch_keeps_claim_and_blocks_retry(tmp_path, monkeypatch):
    svc, value, first, launches, errors, resume, thread = _blocked_prelaunch_accept(tmp_path, monkeypatch)
    assert svc._invocation_in_flight(first["attempt_id"]) is True
    status = svc._status(first["dispatch_id"], first["attempt_id"], reconcile=True)
    assert status["state"] == "ACCEPTED"
    assert status["write_claim_state"] == "ACTIVE"

    retry = envelope(svc, value, sequence=2, retry_parent=first["attempt_id"])
    with pytest.raises(fabric.FabricError) as exc:
        accept(svc, retry)
    assert exc.value.code == "FABRIC_WRITE_OWNERSHIP_BLOCKED"
    assert launches == []

    resume.set()
    thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert len(launches) == 1


def test_restarted_service_reconciles_abandoned_accepted_and_retry_gets_next_epoch(
    tmp_path, monkeypatch
):
    svc = make_service(tmp_path, monkeypatch)
    value = contract(tmp_path, auth="reversible_write")
    first = envelope(svc, value)
    policy_loads = 0

    def crash_after_durable_accept():
        nonlocal policy_loads
        policy_loads += 1
        if policy_loads == 2:
            raise RuntimeError("simulated service crash after durable ACCEPTED")
        return policy(tmp_path)

    svc.policy_loader = crash_after_durable_accept
    with pytest.raises(RuntimeError, match="simulated service crash"):
        accept(svc, first)

    assert row_for(svc, first["attempt_id"])["state"] == "ACCEPTED"
    assert claim_for(svc)["state"] == "ACTIVE"
    assert svc._invocation_in_flight(first["attempt_id"]) is False

    restarted = make_service(tmp_path, monkeypatch)
    polled = restarted._status(first["dispatch_id"], first["attempt_id"], reconcile=False)
    assert polled["state"] == "ACCEPTED"
    assert polled["write_claim_state"] == "ACTIVE"

    reconciled = restarted._status(first["dispatch_id"], first["attempt_id"], reconcile=True)
    assert reconciled["state"] == "BLOCKED"
    assert reconciled["write_claim_state"] == "RELEASED"
    released = claim_for(restarted)
    assert released["release_proof"] == "abandoned_accepted_prelaunch_no_execution"

    retry = envelope(restarted, value, sequence=2, retry_parent=first["attempt_id"])
    response = accept(restarted, retry)
    assert response["data"]["write_epoch"] == 2
    assert claim_for(restarted)["attempt_id"] == retry["attempt_id"]
    assert claim_for(restarted)["state"] == "ACTIVE"


def test_cancel_wins_before_final_launch_check_and_releases_unstarted_claim(
    tmp_path, monkeypatch
):
    svc, value, first, launches, errors, resume, thread = _blocked_prelaunch_accept(
        tmp_path, monkeypatch
    )
    assert row_for(svc, first["attempt_id"])["state"] == "ACCEPTED"
    cancelled = svc._cancel(
        first["dispatch_id"], first["attempt_id"], "coord-main", policy(tmp_path)
    )
    assert cancelled["state"] == "CANCEL_REQUESTED"
    assert cancelled["write_claim_state"] == "ACTIVE"
    assert launches == []

    resume.set()
    thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert launches == []
    assert row_for(svc, first["attempt_id"])["state"] == "CANCELLED"
    released = claim_for(svc)
    assert released["state"] == "RELEASED"
    assert released["release_proof"] == "launch_fence_revoked_cancelled_no_execution"

    retry = envelope(svc, value, sequence=2, retry_parent=first["attempt_id"])
    response = accept(svc, retry)
    assert response["data"]["write_epoch"] == 2
    assert claim_for(svc)["attempt_id"] == retry["attempt_id"]
    assert launches == [svc.unit_manager.unit_name(retry["attempt_id"])]


def test_cancelled_prelaunch_validation_failure_releases_definitely_unstarted_claim(
    tmp_path, monkeypatch
):
    svc, value, first, launches, errors, resume, thread = _blocked_prelaunch_accept(
        tmp_path, monkeypatch, available=False
    )
    cancelled = svc._cancel(
        first["dispatch_id"], first["attempt_id"], "coord-main", policy(tmp_path)
    )
    assert cancelled["state"] == "CANCEL_REQUESTED"
    assert claim_for(svc)["state"] == "ACTIVE"

    resume.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], fabric.FabricError)
    assert errors[0].code == "FABRIC_RUNNER_UNAVAILABLE"
    assert launches == []
    assert row_for(svc, first["attempt_id"])["state"] == "CANCELLED"
    assert claim_for(svc)["state"] == "RELEASED"

    monkeypatch.setattr(
        FakeLocalBackend,
        "availability",
        lambda _self, **_kwargs: {"available": True},
    )
    retry = envelope(svc, value, sequence=2, retry_parent=first["attempt_id"])
    replay = accept(svc, retry)
    assert replay["data"]["write_epoch"] == 2


def test_cancel_lost_prelaunch_update_returns_concurrent_terminal_state(tmp_path, monkeypatch):
    unit = FakeUnitManager()
    cancel_calls = []
    svc = make_service(tmp_path, monkeypatch, unit=unit)
    svc.cancel_fn = lambda backend, task: cancel_calls.append((backend, task))
    value = contract(tmp_path, auth="reversible_write")
    env = envelope(svc, value)
    accept(svc, env)
    with base._connect(svc.db_path) as db:
        db.execute(
            "UPDATE attempts SET state='ACCEPTED' WHERE attempt_id=?",
            (env["attempt_id"],),
        )

    original_row = svc._row
    first_read = True

    def stale_prelaunch_row(dispatch_id, attempt_id):
        nonlocal first_read
        row = original_row(dispatch_id, attempt_id)
        if first_read:
            first_read = False
            with base._connect(svc.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state='FAILED' WHERE attempt_id=?",
                    (attempt_id,),
                )
        return row

    svc._row = stale_prelaunch_row
    cancelled = svc._cancel(env["dispatch_id"], env["attempt_id"], "coord-main", policy(tmp_path))

    assert cancelled["state"] == "FAILED"
    assert cancelled["idempotent"] is True
    assert row_for(svc, env["attempt_id"])["state"] == "FAILED"
    assert unit.stopped == []
    assert cancel_calls == []


def test_launch_wins_first_and_cancel_returns_while_launch_call_is_in_flight(
    tmp_path, monkeypatch
):
    launch_entered = threading.Event()
    launch_resume = threading.Event()
    cancel_finished = threading.Event()
    launches: list[str] = []
    accept_errors: list[BaseException] = []
    cancel_results: list[dict] = []
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, unit=unit)

    def delayed_launch(_contract, **kwargs):
        launches.append(kwargs["unit_id"])
        launch_entered.set()
        assert launch_resume.wait(10), "test did not release delayed writer launch"
        unit.activate(kwargs["unit_id"])
        return {"success": True, "changed": True, "backend": "pi_rpc"}

    svc.write_dispatch_fn = delayed_launch
    value = contract(tmp_path, auth="reversible_write")
    first = envelope(svc, value)

    def run_accept():
        try:
            accept(svc, first)
        except BaseException as exc:  # noqa: BLE001 - thread surfaces failures after join
            accept_errors.append(exc)

    accept_thread = threading.Thread(target=run_accept)
    accept_thread.start()
    assert launch_entered.wait(5), "accept did not reach delayed writer launch"

    def run_cancel():
        cancel_results.append(
            svc._cancel(first["dispatch_id"], first["attempt_id"], "coord-main", policy(tmp_path))
        )
        cancel_finished.set()

    cancel_thread = threading.Thread(target=run_cancel)
    cancel_thread.start()
    assert cancel_finished.wait(2), "cancellation blocked behind the external launch"
    assert claim_for(svc)["state"] == "ACTIVE"
    assert launches == [unit.unit_name(first["attempt_id"])]
    assert cancel_results[0]["state"] == "CANCEL_REQUESTED"
    assert row_for(svc, first["attempt_id"])["state"] == "CANCEL_REQUESTED"
    assert unit.stopped == []
    repeated = svc._cancel(
        first["dispatch_id"], first["attempt_id"], "coord-main", policy(tmp_path)
    )
    assert repeated["state"] == "CANCEL_REQUESTED"
    assert repeated["changed"] is False
    status = svc._status(first["dispatch_id"], first["attempt_id"], reconcile=True)
    assert status["state"] == "CANCEL_REQUESTED"
    assert status["write_claim_state"] == "ACTIVE"
    assert unit.stopped == []

    launch_resume.set()
    accept_thread.join(5)
    cancel_thread.join(5)
    assert not accept_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert accept_errors == []
    assert cancel_results[0]["state"] in {"CANCEL_REQUESTED", "CANCELLED"}
    assert row_for(svc, first["attempt_id"])["state"] == "CANCELLED"
    assert claim_for(svc)["state"] == "RELEASED"
    assert unit.stopped == [unit.unit_name(first["attempt_id"])]


def test_slow_launch_does_not_hold_peer_lock_for_unrelated_conflict_domain(
    tmp_path, monkeypatch
):
    launch_entered = threading.Event()
    launch_resume = threading.Event()
    unrelated_finished = threading.Event()
    errors: list[BaseException] = []
    svc = make_service(tmp_path, monkeypatch)

    def delayed_launch(_contract, **_kwargs):
        launch_entered.set()
        assert launch_resume.wait(5), "test did not release delayed writer launch"
        return {"success": True, "changed": True, "backend": "pi_rpc"}

    svc.write_dispatch_fn = delayed_launch
    first = envelope(svc, contract(tmp_path, auth="reversible_write"))
    unrelated = envelope(svc, contract(tmp_path), sequence=2)

    def run_accept():
        try:
            accept(svc, first)
        except BaseException as exc:  # noqa: BLE001 - thread surfaces failures after join
            errors.append(exc)

    accept_thread = threading.Thread(target=run_accept)
    accept_thread.start()
    assert launch_entered.wait(5), "accept did not reach delayed writer launch"

    def run_unrelated_operations():
        try:
            unrelated_accept = accept(svc, unrelated)
            assert unrelated_accept["data"]["state"] == "RUNNING"
            unrelated_cancel = svc._cancel(
                unrelated["dispatch_id"],
                unrelated["attempt_id"],
                "coord-main",
                policy(tmp_path),
            )
            assert unrelated_cancel["state"] == "CANCELLED"
            with svc._lock, base._connect(svc.db_path) as db:
                svc.claims.acquire(
                    db,
                    conflict_domain="workspace:unrelated",
                    attempt_id="attempt-unrelated",
                    unit_id="unit-unrelated",
                )
        except BaseException as exc:  # noqa: BLE001 - thread surfaces failures after join
            errors.append(exc)
        finally:
            unrelated_finished.set()

    unrelated_thread = threading.Thread(target=run_unrelated_operations)
    unrelated_thread.start()
    assert unrelated_finished.wait(2), "slow external launch held the peer-wide lock"
    assert claim_for(svc, "workspace:unrelated")["state"] == "ACTIVE"

    launch_resume.set()
    accept_thread.join(5)
    unrelated_thread.join(5)
    assert not accept_thread.is_alive()
    assert not unrelated_thread.is_alive()
    assert errors == []


def test_revoked_fence_with_unknown_unit_state_releases_without_execution(
    tmp_path, monkeypatch
):
    svc, _value, first, launches, errors, resume, thread = _blocked_prelaunch_accept(
        tmp_path, monkeypatch
    )
    cancelled = svc._cancel(
        first["dispatch_id"], first["attempt_id"], "coord-main", policy(tmp_path)
    )
    assert cancelled["execution_unit_state"] == "unknown"

    def unexpected_inspect(_unit_id):
        raise AssertionError("revoked launch fence must not consult execution-unit state")

    svc.unit_manager.inspect = unexpected_inspect

    resume.set()
    thread.join(5)

    assert not thread.is_alive()
    assert errors == []
    assert launches == []
    assert row_for(svc, first["attempt_id"])["state"] == "CANCELLED"
    released = claim_for(svc)
    assert released["state"] == "RELEASED"
    assert released["release_proof"] == "launch_fence_revoked_cancelled_no_execution"


def test_status_during_healthy_slow_launch_preserves_fence_and_reaches_running(
    tmp_path, monkeypatch
):
    launch_entered = threading.Event()
    launch_resume = threading.Event()
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, unit=unit)

    def delayed_launch(_contract, **kwargs):
        launch_entered.set()
        assert launch_resume.wait(10), "test did not release delayed writer launch"
        unit.activate(kwargs["unit_id"])
        return {"success": True, "changed": True, "backend": "pi_rpc"}

    svc.write_dispatch_fn = delayed_launch
    value = contract(tmp_path, auth="reversible_write")
    first = envelope(svc, value)
    responses = []
    errors = []

    def run_accept():
        try:
            responses.append(accept(svc, first))
        except BaseException as exc:  # noqa: BLE001 - thread surfaces failures after join
            errors.append(exc)

    thread = threading.Thread(target=run_accept)
    thread.start()
    assert launch_entered.wait(5), "accept did not reach delayed writer launch"

    status = svc._status(first["dispatch_id"], first["attempt_id"], reconcile=False)
    assert status["state"] == "LAUNCHING"
    assert row_for(svc, first["attempt_id"])["state"] == "LAUNCHING"
    assert claim_for(svc)["state"] == "ACTIVE"

    launch_resume.set()
    thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert responses[0]["data"]["state"] == "RUNNING"
    assert row_for(svc, first["attempt_id"])["state"] == "RUNNING"
    assert claim_for(svc)["state"] == "ACTIVE"

    retry = envelope(svc, value, sequence=2, retry_parent=first["attempt_id"])
    with pytest.raises(fabric.FabricError) as exc:
        accept(svc, retry)
    assert exc.value.code == "FABRIC_WRITE_OWNERSHIP_BLOCKED"


def test_secondary_accepts_do_not_clear_original_invocation_marker(tmp_path, monkeypatch):
    launch_entered = threading.Event()
    launch_resume = threading.Event()
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, unit=unit)

    def delayed_launch(_contract, **kwargs):
        launch_entered.set()
        assert launch_resume.wait(10), "test did not release delayed writer launch"
        unit.activate(kwargs["unit_id"])
        return {"success": True, "changed": True, "backend": "pi_rpc"}

    svc.write_dispatch_fn = delayed_launch
    value = contract(tmp_path, auth="reversible_write")
    first = envelope(svc, value)
    responses = []
    errors = []

    def run_accept():
        try:
            responses.append(accept(svc, first))
        except BaseException as exc:  # noqa: BLE001 - thread surfaces failures after join
            errors.append(exc)

    thread = threading.Thread(target=run_accept)
    thread.start()
    assert launch_entered.wait(5), "accept did not reach delayed writer launch"
    assert svc._invocation_in_flight(first["attempt_id"]) is True

    status = svc._status(first["dispatch_id"], first["attempt_id"], reconcile=True)
    assert status["state"] == "LAUNCHING"
    assert svc._invocation_in_flight(first["attempt_id"]) is True

    launch_resume.set()
    thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert responses[0]["data"]["state"] == "RUNNING"
    assert svc._invocation_in_flight(first["attempt_id"]) is False

    replay = accept(svc, first)
    assert replay["code"] == "FABRIC_IDEMPOTENT_REPLAY"

    conflicting = dict(first, objective="Conflicting canonical envelope content.")
    with pytest.raises(fabric.FabricError) as exc:
        accept(svc, conflicting)
    assert exc.value.code == "FABRIC_IDEMPOTENCY_CONFLICT"


def test_post_launch_response_exception_clears_marker_and_keeps_durable_ownership(
    tmp_path, monkeypatch
):
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, unit=unit)
    env = envelope(svc, contract(tmp_path, auth="reversible_write"))
    original_response = base._response

    def raising_response(operation, **kwargs):
        if operation == "accept":
            raise RuntimeError("deterministic response construction failure")
        return original_response(operation, **kwargs)

    monkeypatch.setattr(base, "_response", raising_response)

    with pytest.raises(RuntimeError, match="response construction failure"):
        accept(svc, env)

    assert svc._invocation_in_flight(env["attempt_id"]) is False
    assert row_for(svc, env["attempt_id"])["state"] == "RUNNING"
    assert claim_for(svc)["state"] == "ACTIVE"


def test_writer_launch_exception_clears_marker_and_preserves_uncertain_ownership(
    tmp_path, monkeypatch
):
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, unit=unit)

    def raising_launch(_contract, **_kwargs):
        raise RuntimeError("deterministic launch failure")

    svc.write_dispatch_fn = raising_launch
    value = contract(tmp_path, auth="reversible_write")
    first = envelope(svc, value)

    response = accept(svc, first)

    assert response["ok"] is False
    assert response["code"] == "FABRIC_WRITE_LAUNCH_EXCEPTION_AMBIGUOUS"
    assert response["data"]["state"] == "LOST_AMBIGUOUS"
    assert svc._invocation_in_flight(first["attempt_id"]) is False
    persisted = row_for(svc, first["attempt_id"])
    assert persisted["state"] == "LOST_AMBIGUOUS"
    assert "FABRIC_WRITE_LAUNCH_EXCEPTION_AMBIGUOUS" in persisted["dispatch_result_json"]
    assert claim_for(svc)["state"] == "ACTIVE"

    reconciled = svc._status(first["dispatch_id"], first["attempt_id"], reconcile=True)
    assert reconciled["state"] == "LOST_AMBIGUOUS"
    assert reconciled["write_claim_state"] == "ACTIVE"

    retry = envelope(svc, value, sequence=2, retry_parent=first["attempt_id"])
    with pytest.raises(fabric.FabricError) as exc:
        accept(svc, retry)
    assert exc.value.code == "FABRIC_WRITE_OWNERSHIP_BLOCKED"

    cancelled = svc._cancel(
        first["dispatch_id"], first["attempt_id"], "coord-main", policy(tmp_path)
    )
    assert cancelled["state"] == "CANCELLED"
    assert cancelled["write_claim_state"] == "RELEASED"


def test_definite_launch_failure_after_cancel_terminalizes_and_allows_retry(
    tmp_path, monkeypatch
):
    launch_entered = threading.Event()
    launch_resume = threading.Event()
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, unit=unit)

    def failed_launch(_contract, **kwargs):
        launch_entered.set()
        assert launch_resume.wait(5), "test did not release failed launch"
        unit.quiesce(kwargs["unit_id"])
        return {
            "success": False,
            "changed": False,
            "code": "FABRIC_EXECUTION_UNIT_START_FAILED",
        }

    svc.write_dispatch_fn = failed_launch
    value = contract(tmp_path, auth="reversible_write")
    first = envelope(svc, value)
    errors = []
    cancel_finished = threading.Event()
    cancel_results = []

    def run_accept():
        try:
            accept(svc, first)
        except BaseException as exc:  # noqa: BLE001 - thread surfaces failures after join
            errors.append(exc)

    thread = threading.Thread(target=run_accept)
    thread.start()
    assert launch_entered.wait(5), "accept did not reach failed launch"

    def run_cancel():
        cancel_results.append(
            svc._cancel(
                first["dispatch_id"],
                first["attempt_id"],
                "coord-main",
                policy(tmp_path),
            )
        )
        cancel_finished.set()

    cancel_thread = threading.Thread(target=run_cancel)
    cancel_thread.start()
    assert cancel_finished.wait(2), "cancellation blocked behind the failed launch callback"
    assert claim_for(svc)["state"] == "ACTIVE"
    assert cancel_results[0]["state"] == "CANCEL_REQUESTED"

    launch_resume.set()
    thread.join(5)
    cancel_thread.join(5)
    assert not thread.is_alive()
    assert not cancel_thread.is_alive()
    assert errors == []
    assert cancel_results[0]["state"] in {"CANCEL_REQUESTED", "CANCELLED", "FAILED"}
    assert row_for(svc, first["attempt_id"])["state"] in {"CANCELLED", "FAILED"}
    assert claim_for(svc)["state"] == "RELEASED"

    svc.write_dispatch_fn = lambda _contract, **_kwargs: {
        "success": True,
        "changed": True,
        "backend": "pi_rpc",
    }
    retry = envelope(svc, value, sequence=2, retry_parent=first["attempt_id"])
    response = accept(svc, retry)
    assert response["data"]["write_epoch"] == 2


def test_cancel_between_launch_finalizer_read_and_write_is_preserved(tmp_path, monkeypatch):
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, unit=unit)
    value = contract(tmp_path, auth="reversible_write")
    env = envelope(svc, value)
    original_connect = base._connect
    cancellation = []
    injected = False

    class InterleavingConnection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, parameters=()):
            nonlocal injected
            if (
                not injected
                and "dispatch_result_json" in sql
                and "'LAUNCHING','CANCEL_REQUESTED'" in sql
            ):
                injected = True
                assert row_for(svc, env["attempt_id"])["state"] == "LAUNCHING"
                cancellation.append(
                    svc._cancel(
                        env["dispatch_id"], env["attempt_id"], "coord-main", policy(tmp_path)
                    )
                )
            return self.db.execute(sql, parameters)

    @contextmanager
    def interleaving_connect(path):
        with original_connect(path) as db:
            yield InterleavingConnection(db)

    monkeypatch.setattr(base, "_connect", interleaving_connect)
    response = accept(svc, env)

    assert injected is True
    assert cancellation[0]["state"] == "CANCEL_REQUESTED"
    assert response["data"]["state"] == "CANCELLED"
    assert row_for(svc, env["attempt_id"])["state"] == "CANCELLED"
    assert claim_for(svc)["state"] == "RELEASED"
    assert unit.stopped == [unit.unit_name(env["attempt_id"])]



def test_terminal_runner_does_not_release_while_descendant_unit_is_active(tmp_path, monkeypatch):
    observed = []
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, observed=observed, unit=unit)
    env = envelope(svc, contract(tmp_path, auth="reversible_write"))
    accept(svc, env)
    row = row_for(svc, env["attempt_id"])
    unit.activate(row["execution_unit_id"])
    observed.append({"status": "completed", "outcome": "completed"})
    status = svc._status(env["dispatch_id"], env["attempt_id"], reconcile=True)
    assert status["state"] == "RUNNING"
    assert status["write_claim_state"] == "ACTIVE"



def test_quiescent_unit_with_missing_outcome_is_ambiguous_not_success(tmp_path, monkeypatch):
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, observed=[], unit=unit)
    env = envelope(svc, contract(tmp_path, auth="reversible_write"))
    accept(svc, env)
    row = row_for(svc, env["attempt_id"])
    unit.quiesce(row["execution_unit_id"])

    polled = svc._status(env["dispatch_id"], env["attempt_id"], reconcile=False)
    assert polled["state"] == "LOST_AMBIGUOUS"
    assert polled["write_claim_state"] == "ACTIVE"

    status = svc._status(env["dispatch_id"], env["attempt_id"], reconcile=True)
    assert status["state"] == "LOST_AMBIGUOUS"
    assert status["write_claim_state"] == "RELEASED"


def test_unknown_unit_with_missing_outcome_keeps_claim_during_reconcile(tmp_path, monkeypatch):
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, observed=[], unit=unit)
    env = envelope(svc, contract(tmp_path, auth="reversible_write"))
    accept(svc, env)

    status = svc._status(env["dispatch_id"], env["attempt_id"], reconcile=True)

    assert status["state"] == "LOST_AMBIGUOUS"
    assert status["write_claim_state"] == "ACTIVE"



def test_peer_restart_keeps_active_claim_when_execution_unit_is_active(tmp_path, monkeypatch):
    unit = FakeUnitManager()
    observed = []
    first = make_service(tmp_path, monkeypatch, observed=observed, unit=unit)
    env = envelope(first, contract(tmp_path, auth="reversible_write"))
    accept(first, env)
    row = row_for(first, env["attempt_id"])
    unit.activate(row["execution_unit_id"])

    restarted = make_service(tmp_path, monkeypatch, observed=observed, unit=unit)
    status = restarted._status(env["dispatch_id"], env["attempt_id"], reconcile=True)
    assert status["state"] == "RUNNING"
    assert status["write_claim_state"] == "ACTIVE"


def test_peer_restart_reconciles_uncertain_launching_and_blocks_retry(tmp_path, monkeypatch):
    first_unit = FakeUnitManager()
    value = contract(tmp_path, auth="reversible_write")
    first = make_service(tmp_path, monkeypatch, unit=first_unit)
    env = envelope(first, value)
    accept(first, env)
    original = row_for(first, env["attempt_id"])
    with base._connect(first.db_path) as db:
        db.execute(
            "UPDATE attempts SET state='LAUNCHING' WHERE attempt_id=?",
            (env["attempt_id"],),
        )

    restarted_unit = FakeUnitManager()
    inspected = []
    original_inspect = restarted_unit.inspect

    def inspect(unit_id):
        inspected.append(unit_id)
        return original_inspect(unit_id)

    restarted_unit.inspect = inspect
    restarted = make_service(tmp_path, monkeypatch, unit=restarted_unit)
    status = restarted._status(env["dispatch_id"], env["attempt_id"], reconcile=True)

    assert inspected == [original["execution_unit_id"]]
    assert status["attempt_id"] == env["attempt_id"]
    assert status["write_epoch"] == original["write_epoch"] == 1
    assert status["state"] == "LOST_AMBIGUOUS"
    assert status["write_claim_state"] == "ACTIVE"

    repeated = restarted._status(env["dispatch_id"], env["attempt_id"], reconcile=True)
    assert repeated["state"] == "LOST_AMBIGUOUS"
    assert repeated["write_claim_state"] == "ACTIVE"

    retry = envelope(restarted, value, sequence=2, retry_parent=env["attempt_id"])
    with pytest.raises(fabric.FabricError) as exc:
        accept(restarted, retry)
    assert exc.value.code == "FABRIC_WRITE_OWNERSHIP_BLOCKED"
    assert claim_for(restarted)["attempt_id"] == env["attempt_id"]
    assert claim_for(restarted)["epoch"] == 1



def test_cancel_releases_claim_only_after_exact_unit_is_quiescent(tmp_path, monkeypatch):
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, unit=unit)
    env = envelope(svc, contract(tmp_path, auth="reversible_write"))
    accept(svc, env)
    row = row_for(svc, env["attempt_id"])
    result = svc._cancel(env["dispatch_id"], env["attempt_id"], "coord-main", policy(tmp_path))
    assert unit.stopped == [row["execution_unit_id"]]
    assert result["state"] == "CANCELLED"
    assert result["write_claim_state"] == "RELEASED"


def test_cancel_ambiguity_keeps_claim_and_blocks_overlapping_writer(tmp_path, monkeypatch):
    class UncertainStopUnitManager(FakeUnitManager):
        def stop(self, unit):
            self.stopped.append(unit)
            value = {"known": False, "active": False, "quiescent": False, "state": "unknown"}
            self.units[unit] = value
            return dict(value)

    unit = UncertainStopUnitManager()
    svc = make_service(tmp_path, monkeypatch, unit=unit)
    value = contract(tmp_path, auth="reversible_write")
    first = envelope(svc, value)
    accept(svc, first)
    cancelled = svc._cancel(
        first["dispatch_id"], first["attempt_id"], "coord-main", policy(tmp_path)
    )
    assert cancelled["state"] == "CANCEL_REQUESTED"
    assert cancelled["write_claim_state"] == "ACTIVE"
    assert cancelled["execution_unit_state"] == "unknown"

    second = envelope(svc, value, sequence=2, retry_parent=first["attempt_id"])
    with pytest.raises(fabric.FabricError) as exc:
        accept(svc, second)
    assert exc.value.code == "FABRIC_WRITE_OWNERSHIP_BLOCKED"
    assert claim_for(svc)["attempt_id"] == first["attempt_id"]



def test_monotonic_epoch_prevents_stale_attempt_from_releasing_new_claim(tmp_path, monkeypatch):
    observed = []
    unit = FakeUnitManager()
    svc = make_service(tmp_path, monkeypatch, observed=observed, unit=unit)
    value = contract(tmp_path, auth="reversible_write")
    first = envelope(svc, value)
    accept(svc, first)
    first_row = row_for(svc, first["attempt_id"])
    unit.quiesce(first_row["execution_unit_id"])
    svc._status(first["dispatch_id"], first["attempt_id"], reconcile=True)

    second = envelope(svc, value, sequence=2, retry_parent=first["attempt_id"])
    response = accept(svc, second)
    assert response["data"]["write_epoch"] == 2
    assert svc.claims.release(first_row, proof="stale-release") is False
    claim = claim_for(svc)
    assert claim["attempt_id"] == second["attempt_id"]
    assert claim["state"] == "ACTIVE"
    assert claim["epoch"] == 2



def test_write_feature_is_not_advertised_without_execution_unit(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch, unit=FakeUnitManager(available=False))
    cap = svc.capabilities(policy(tmp_path))
    assert fabric.FEATURE_ARTIFACT in cap["features"]
    assert fabric.FEATURE_WRITE_OWNERSHIP not in cap["features"]
    env = envelope(svc, contract(tmp_path, auth="reversible_write"))
    with pytest.raises(fabric.FabricError) as exc:
        accept(svc, env)
    assert exc.value.code == "FABRIC_EXECUTION_UNIT_UNAVAILABLE"


@pytest.mark.parametrize(
    "name",
    ["../secret.txt", "/tmp/out.txt", "dir\\out.txt", ".env", "secrets/key.txt"],
)
def test_remote_artifact_logical_name_rejects_unsafe_paths(name):
    with pytest.raises(fabric.FabricError):
        fabric._logical_artifact_name(name)



def successful_artifact_service(tmp_path, monkeypatch, content=b"original-bytes"):
    observed = [{"status": "completed", "outcome": "completed", "started_at": "s", "ended_at": "e"}]
    svc = make_service(tmp_path, monkeypatch, observed=observed)
    source = tmp_path / "out.txt"
    source.write_bytes(content)
    env = envelope(svc, contract(tmp_path))
    accept(svc, env)
    svc._status(env["dispatch_id"], env["attempt_id"], reconcile=False)
    return svc, env, source



def manifest_request(env, specs):
    return base._request(
        "artifact_manifest",
        "coord-main",
        data={"artifacts": specs, "max_artifact_bytes": 1024 * 1024, "max_total_bytes": 1024 * 1024},
        dispatch_id=env["dispatch_id"],
        attempt_id=env["attempt_id"],
    )



def test_artifact_symlink_is_rejected(tmp_path, monkeypatch):
    svc, env, source = successful_artifact_service(tmp_path, monkeypatch)
    source.unlink()
    target = tmp_path / "real.txt"
    target.write_text("secret", encoding="utf-8")
    source.symlink_to(target)
    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(
            manifest_request(env, [{"path": "out.txt", "must_exist": True, "min_bytes": 1}]),
            f"Bearer {TOKEN}",
        )
    assert exc.value.code == "FABRIC_ARTIFACT_SYMLINK_REJECTED"



def test_artifact_snapshot_is_immutable_after_source_replacement(tmp_path, monkeypatch):
    svc, env, source = successful_artifact_service(tmp_path, monkeypatch, content=b"frozen-content")
    response = svc.handle(
        manifest_request(env, [{"path": "out.txt", "must_exist": True, "min_bytes": 1}]),
        f"Bearer {TOKEN}",
    )
    item = response["data"]["manifest"]["artifacts"][0]
    source.write_bytes(b"replacement-content")
    chunk_request = base._request(
        "artifact_chunk",
        "coord-main",
        data={"artifact_id": item["artifact_id"], "offset": 0, "max_bytes": 1024},
        dispatch_id=env["dispatch_id"],
        attempt_id=env["attempt_id"],
    )
    chunk = svc.handle(chunk_request, f"Bearer {TOKEN}")["data"]["chunk"]
    assert base64.b64decode(chunk["data_b64"]) == b"frozen-content"
    assert chunk["sha256"] == hashlib.sha256(b"frozen-content").hexdigest()
    assert "snapshot_path" not in item



def test_coordinator_rejects_wrong_lineage_manifest(tmp_path, monkeypatch):
    svc, _env, _source = successful_artifact_service(tmp_path, monkeypatch)
    value = contract(tmp_path, artifacts=[{"path": "out.txt", "must_exist": True, "min_bytes": 1}])
    coord = make_coordinator(tmp_path, svc)
    result = coord.dispatch(value, dry_run=False, confirm=True, timeout=5)
    attempt, dispatch, target = coord._attempt(result["attempt_id"])
    manifest = {
        "schema": fabric.ARTIFACT_MANIFEST_SCHEMA,
        "version": 1,
        "dispatch_id": "fabd-" + "0" * 32,
        "attempt_id": attempt["attempt_id"],
        "contract_sha256": dispatch["contract_sha256"],
        "node_name": target.name,
        "artifacts": [],
        "total_bytes": 0,
        "created_at": "now",
    }
    manifest["manifest_sha256"] = fabric.sha256_json(manifest)
    with pytest.raises(fabric.FabricError) as exc:
        coord._validate_manifest(
            manifest,
            attempt=attempt,
            dispatch=dispatch,
            node=target,
            specs=[{"path": "out.txt", "must_exist": True, "min_bytes": 1}],
        )
    assert exc.value.code == "FABRIC_ARTIFACT_LINEAGE_MISMATCH"



def test_coordinator_hash_mismatch_does_not_admit_artifact(tmp_path, monkeypatch):
    svc, env, _source = successful_artifact_service(tmp_path, monkeypatch)
    coord = make_coordinator(tmp_path, svc)
    coord._ensure_db()
    value = contract(tmp_path)
    contract_sha = base._contract_sha(value)
    now = base._now()
    with base._connect(coord.db_path) as db:
        db.execute(
            "INSERT INTO dispatches(dispatch_id,task_id,contract_sha256,node_name,evidence_policy_json,created_at) VALUES(?,?,?,?,?,?)",
            (env["dispatch_id"], value["task_id"], contract_sha, "node-a", '{"run_state":["managed_peer_structured"]}', now),
        )
        db.execute(
            "INSERT INTO attempts(attempt_id,dispatch_id,envelope_sha256,node_name,peer_name,remote_backend,coordinator_principal,capability_sha256,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (env["attempt_id"], env["dispatch_id"], "0" * 64, "node-a", "node-a-peer", "pi_rpc", "coord-main", "0" * 64, "TERMINAL_REPORTED", now, now),
        )
    attempt, _dispatch, target = coord._attempt(env["attempt_id"])
    item = {
        "artifact_id": "fart-" + "a" * 32,
        "logical_name": "out.txt",
        "size_bytes": 3,
        "sha256": hashlib.sha256(b"good").hexdigest(),
        "media_type": "text/plain",
        "active_content": False,
    }

    def bad_rpc(_node, request, _timeout):
        if request["operation"] == "artifact_chunk":
            data = base64.b64encode(b"bad").decode("ascii")
            return None, base._response(
                "artifact_chunk",
                ok=True,
                code="FABRIC_OK",
                data={
                    "chunk": {
                        "schema": fabric.ARTIFACT_CHUNK_SCHEMA,
                        "version": 1,
                        "dispatch_id": attempt["dispatch_id"],
                        "attempt_id": attempt["attempt_id"],
                        "artifact_id": item["artifact_id"],
                        "offset": 0,
                        "next_offset": 3,
                        "size_bytes": 3,
                        "sha256": item["sha256"],
                        "data_b64": data,
                        "eof": True,
                    }
                },
            )
        raise AssertionError(request["operation"])

    coord.rpc = bad_rpc
    with pytest.raises(fabric.FabricError) as exc:
        coord._pull_artifact(attempt, target, item, timeout=5)
    assert exc.value.code == "FABRIC_ARTIFACT_HASH_MISMATCH"
    with base._connect_readonly(coord.db_path) as db:
        assert db.execute("SELECT COUNT(*) AS n FROM artifact_admissions").fetchone()["n"] == 0



def test_retry_blocked_while_prior_write_claim_active(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)
    coord = make_coordinator(tmp_path, svc)
    value = contract(tmp_path, auth="reversible_write")
    result = coord.dispatch(value, dry_run=False, confirm=True, timeout=5)
    with pytest.raises(fabric.FabricError) as exc:
        coord.retry(value, result["attempt_id"], confirm=True, timeout=5)
    assert exc.value.code in {"FABRIC_WRITE_OWNERSHIP_BLOCKED", "FABRIC_RETRY_BLOCKED"}



def auto_contract(tmp_path, *, auth="read_only", artifacts=None):
    value = contract(tmp_path, auth=auth, artifacts=artifacts)
    value["assigned_agent"] = "auto"
    value["execution"] = {
        "backend": "auto",
        "options": {
            "requirements": {},
            "preferences": {"prefer_local": False},
            "logical_workspace": "repo",
            "runner_options": {},
        },
    }
    return value



def facts(now):
    return base_router.TargetFacts(
        observed_at=now,
        max_age_seconds=300,
        os_names=frozenset({"linux"}),
        runtimes=frozenset({"python"}),
        runners=frozenset({"pi_rpc"}),
        providers=frozenset(),
        models=frozenset(),
        tools=frozenset(),
        browser=False,
        vision=False,
        gpu_available=False,
        gpu_vendor="",
        gpu_memory_mb=0,
        capacity=1,
        active=0,
        cost_bucket=1,
        locality_bucket=1,
    )



def test_auto_remote_write_unlock_requires_live_g4c_features(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    now = datetime.now(timezone.utc)
    common = {
        "registry_loader": lambda: {"node-a": node()},
        "routing_policy_loader": lambda: base_router.RoutingPolicy(targets={"node-a": facts(now)}),
        "local_backends": list,
        "now": lambda: now,
    }
    locked = fabric.AutoRouter(
        remote_probe=lambda _node, _timeout: {"healthy": True, "latency_ms": 5, "features": []},
        **common,
    ).route(auto_contract(tmp_path, auth="reversible_write"))
    assert locked["selected"] is None

    unlocked = fabric.AutoRouter(
        remote_probe=lambda _node, _timeout: {
            "healthy": True,
            "latency_ms": 5,
            "features": [fabric.FEATURE_WRITE_OWNERSHIP, fabric.FEATURE_EXECUTION_UNIT, fabric.FEATURE_WRITE_EPOCH],
        },
        **common,
    ).route(auto_contract(tmp_path, auth="reversible_write"))
    assert unlocked["selected"]["node"] == "node-a"


def test_g4c_auto_route_audits_only_authoritative_post_unlock_decision(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    audits: list[dict[str, object]] = []

    def capture_audit(decision, *, success, dry_run):
        audits.append(
            {
                "success": success,
                "dry_run": dry_run,
                "selected": dict(decision.get("selected") or {}),
            }
        )

    monkeypatch.setattr(base_router, "_audit_route", capture_audit)
    now = datetime.now(timezone.utc)
    decision = fabric.AutoRouter(
        registry_loader=lambda: {"node-a": node()},
        routing_policy_loader=lambda: base_router.RoutingPolicy(targets={"node-a": facts(now)}),
        local_backends=list,
        remote_probe=lambda _node, _timeout: {
            "healthy": True,
            "latency_ms": 5,
            "features": [
                fabric.FEATURE_WRITE_OWNERSHIP,
                fabric.FEATURE_EXECUTION_UNIT,
                fabric.FEATURE_WRITE_EPOCH,
            ],
        },
        now=lambda: now,
    ).route(auto_contract(tmp_path, auth="reversible_write"), dry_run=False)

    assert decision["selected"]["node"] == "node-a"
    assert decision["selected"]["backend"] == "pi_rpc"
    assert audits == [
        {
            "success": True,
            "dry_run": False,
            "selected": {
                "node": "node-a",
                "backend": "pi_rpc",
                "transport_backend": "fabric",
                "remote": True,
                "healthy": True,
                "capability_fresh": True,
                "authority_ceiling": "high_impact",
                "eligible": True,
                "exclusions": [],
                "rank": decision["selected"]["rank"],
            },
        }
    ]


def test_g4c_auto_route_audits_failure_when_feature_gate_raises(tmp_path, monkeypatch):
    """A gate crash after the preliminary base route must still leave one audit
    record, must not claim the preliminary winner, and must propagate."""
    from datetime import datetime, timezone

    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    audits: list[dict[str, object]] = []

    def capture_audit(decision, *, success, dry_run):
        audits.append(
            {
                "success": success,
                "dry_run": dry_run,
                "selected": decision.get("selected"),
            }
        )

    monkeypatch.setattr(base_router, "_audit_route", capture_audit)

    class _BoomFeatures(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("g4c feature gate exploded")

    now = datetime.now(timezone.utc)
    router_obj = fabric.AutoRouter(
        registry_loader=lambda: {"node-a": node()},
        routing_policy_loader=lambda: base_router.RoutingPolicy(targets={"node-a": facts(now)}),
        local_backends=list,
        remote_probe=lambda _node, _timeout: {
            "healthy": True,
            "latency_ms": 5,
            "features": [],
        },
        now=lambda: now,
    )
    monkeypatch.setattr(router_obj, "_features", _BoomFeatures())

    with pytest.raises(RuntimeError, match="feature gate exploded"):
        router_obj.route(
            auto_contract(
                tmp_path,
                artifacts=[{"path": "out.txt", "must_exist": True, "min_bytes": 1}],
            ),
            dry_run=False,
        )

    assert audits == [{"success": False, "dry_run": False, "selected": None}]


def test_auto_remote_artifact_unlock_requires_snapshot_features(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    now = datetime.now(timezone.utc)
    router_obj = fabric.AutoRouter(
        registry_loader=lambda: {"node-a": node()},
        routing_policy_loader=lambda: base_router.RoutingPolicy(targets={"node-a": facts(now)}),
        local_backends=list,
        remote_probe=lambda _node, _timeout: {
            "healthy": True,
            "latency_ms": 5,
            "features": [fabric.FEATURE_ARTIFACT, fabric.FEATURE_ARTIFACT_SNAPSHOT],
        },
        now=lambda: now,
    )
    decision = router_obj.route(
        auto_contract(tmp_path, artifacts=[{"path": "out.txt", "must_exist": True, "min_bytes": 1}])
    )
    assert decision["selected"]["node"] == "node-a"



def test_local_auto_write_remains_fail_closed(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    now = datetime.now(timezone.utc)
    route = fabric.AutoRouter(
        registry_loader=dict,
        routing_policy_loader=lambda: base_router.RoutingPolicy(targets={"local": facts(now)}),
        local_backends=lambda: ["pi_rpc"],
        local_posture=lambda _dry: {"ready": True, "max_authorization": "high_impact"},
        now=lambda: now,
    ).route(auto_contract(tmp_path, auth="reversible_write"))
    assert route["selected"] is None
    codes = {entry["code"] for entry in route["candidates"][0]["exclusions"]}
    assert "WRITE_CONFLICT_GUARD_UNAVAILABLE" in codes
