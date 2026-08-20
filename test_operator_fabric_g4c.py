from __future__ import annotations

import base64
import hashlib
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
    status = svc._status(env["dispatch_id"], env["attempt_id"], reconcile=True)
    assert status["state"] == "LOST_AMBIGUOUS"
    assert status["write_claim_state"] == "RELEASED"



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
