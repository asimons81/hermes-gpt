from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import operator_fabric as fabric


class FakeBackend:
    name = "fake"

    def availability(self, *, hermes_root=None):
        return {"available": True}

    def cancel(self, task_id, *, hermes_root=None):
        return {"success": True, "changed": True, "state": "cancelled"}


def policy(
    tmp_path: Path,
    *,
    revision: str = "r1",
    max_auth: str = "reversible_write",
) -> fabric.FabricPeerPolicy:
    mapping = fabric.WorkspaceMapping("repo", tmp_path.resolve(), revision, "workspace:repo")
    raw = {
        "node": "node-a",
        "identity": "Hermes GPT Fabric node-a",
        "principals": ["coord-main"],
        "profiles": ["default"],
        "max": max_auth,
        "backends": ["fake"],
        "features": [],
        "mapping": {
            "repo": {
                "path": str(tmp_path.resolve()),
                "revision": revision,
                "conflict": "workspace:repo",
            }
        },
    }
    return fabric.FabricPeerPolicy(
        node_name="node-a",
        identity="Hermes GPT Fabric node-a",
        allowed_coordinator_principals=("coord-main",),
        allowed_profiles=("default",),
        max_authorization=max_auth,
        allowed_backends=("fake",),
        required_features=(),
        workspace_mappings={"repo": mapping},
        digest=fabric.sha256_json(raw),
    )


def node() -> fabric.FabricNode:
    return fabric.FabricNode(
        name="node-a",
        a2a_peer_name="node-a-peer",
        expected_identity="Hermes GPT Fabric node-a",
        coordinator_principal="coord-main",
        enabled=True,
        allowed_profiles=("default",),
        max_authorization="reversible_write",
        allowed_remote_backends=("fake",),
        logical_workspaces=("repo",),
        required_features=(),
    )


def contract(tmp_path: Path, *, auth_class: str = "read_only") -> dict:
    return {
        "schema": "hermes.work-contract/v1",
        "task_id": "task-fabric-1",
        "objective": "Inspect the repository and report observed completion state.",
        "assigned_agent": "node-a",
        "assigned_profile": "default",
        "inputs": [],
        "constraints": ["Do not publish anything."],
        "allowed_scope": {
            "workspaces": [str(tmp_path.resolve())],
            "profiles": ["default"],
        },
        "forbidden_actions": [],
        "expected_artifacts": [],
        "tests": [],
        "review_requirements": {
            "required": False,
            "reviewer": "",
            "evidence": "",
            "approval_required": False,
        },
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
        "authorization": {"class": auth_class, "approved": True},
        "execution": {
            "backend": "fabric",
            "options": {
                "node": "node-a",
                "remote_backend": "fake",
                "logical_workspace": "repo",
                "remote_options": {},
                "evidence_provenance": {"run_state": ["managed_peer_structured"]},
            },
        },
    }


def service(
    tmp_path: Path,
    monkeypatch,
    *,
    policy_loader=None,
    observed=None,
    dispatch_counter=None,
    cancel_fn=None,
):
    monkeypatch.setattr(fabric.op_runners, "get_backend", lambda _name: FakeBackend())
    observed = observed if observed is not None else []
    dispatch_counter = dispatch_counter if dispatch_counter is not None else {"count": 0}

    def dispatch_fn(_contract, **_kwargs):
        dispatch_counter["count"] += 1
        return {"success": True, "changed": True, "backend": "fake"}

    return fabric.FabricPeerService(
        policy_loader=policy_loader or (lambda: policy(tmp_path)),
        tokens={"coord-main": "0123456789abcdef0123456789abcdef"},
        db_path=tmp_path / "peer.db",
        dispatch_fn=dispatch_fn,
        observed_fn=lambda _task_id: list(observed),
        cancel_fn=cancel_fn
        or (lambda _backend, _task_id: {"success": True, "changed": True, "state": "cancelled"}),
        hermes_root=tmp_path,
    )


def rpc_for(svc: fabric.FabricPeerService):
    def rpc(_node, request, _timeout):
        response = svc.handle(
            request,
            "Bearer 0123456789abcdef0123456789abcdef",
        )
        attempt_id = request.get("attempt_id") or request.get("request_id")
        return f"ftask-{str(attempt_id)[-12:]}", response

    return rpc


def coordinator(
    tmp_path: Path,
    svc: fabric.FabricPeerService,
    *,
    rpc=None,
) -> fabric.FabricCoordinator:
    return fabric.FabricCoordinator(
        registry_loader=lambda: {"node-a": node()},
        db_path=tmp_path / "coord.db",
        rpc=rpc or rpc_for(svc),
        hermes_root=tmp_path,
    )


def capability(svc: fabric.FabricPeerService) -> dict:
    return svc.handle(
        fabric._request("capabilities", "coord-main", data={}),
        "Bearer 0123456789abcdef0123456789abcdef",
    )["data"]


def envelope_for(svc: fabric.FabricPeerService, value: dict) -> dict:
    cap = capability(svc)
    return fabric._build_envelope(
        value,
        node(),
        remote_backend="fake",
        logical_workspace="repo",
        remote_options={},
        evidence_policy={"run_state": ("managed_peer_structured",)},
        capability_sha=cap["snapshot_sha256"],
    )


def accept_request(envelope: dict) -> dict:
    return fabric._request(
        "accept",
        "coord-main",
        data={"envelope": envelope},
        dispatch_id=envelope["dispatch_id"],
        attempt_id=envelope["attempt_id"],
    )


def test_strict_json_rejects_duplicate_fields():
    with pytest.raises(fabric.FabricError) as exc:
        fabric.strict_json_loads('{"a":1,"a":2}')
    assert exc.value.code == "FABRIC_AMBIGUOUS_JSON"


def test_peer_tokens_require_unique_principal_token():
    raw = json.dumps(
        {
            "coord-a": "0123456789abcdef",
            "coord-b": "0123456789abcdef",
        }
    )
    with pytest.raises(fabric.FabricError) as exc:
        fabric.load_peer_tokens(raw)
    assert exc.value.code == "FABRIC_PRINCIPAL_CONFIG_INVALID"


def test_non_loopback_plain_http_is_rejected():
    with pytest.raises(fabric.FabricError) as exc:
        fabric._require_secure_transport("http://192.0.2.10:4780")
    assert exc.value.code == "FABRIC_TRANSPORT_INSECURE"
    fabric._require_secure_transport("http://127.0.0.1:4780")


def test_remote_options_reject_nested_credentials_and_urls(tmp_path):
    value = contract(tmp_path)
    value["execution"]["options"]["remote_options"] = {"nested": {"api_key": "nope"}}
    with pytest.raises(fabric.FabricError) as exc:
        fabric._fabric_options(value)
    assert exc.value.code == "FABRIC_CALLER_CREDENTIAL"

    value["execution"]["options"]["remote_options"] = {"nested": {"endpoint": "example"}}
    with pytest.raises(fabric.FabricError) as exc:
        fabric._fabric_options(value)
    assert exc.value.code == "FABRIC_CALLER_NETWORK_TARGET"


def test_peer_requires_authenticated_unique_principal(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    request = fabric._request("capabilities", "coord-main", data={})
    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(request, "")
    assert exc.value.code == "FABRIC_PRINCIPAL_AUTH_REQUIRED"
    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(request, "Bearer wrong-wrong-wrong-wrong")
    assert exc.value.code == "FABRIC_PRINCIPAL_AUTH_FAILED"


def test_peer_accept_is_durable_and_idempotent(tmp_path, monkeypatch):
    counter = {"count": 0}
    svc = service(tmp_path, monkeypatch, dispatch_counter=counter)
    envelope = envelope_for(svc, contract(tmp_path))
    request = accept_request(envelope)
    first = svc.handle(request, "Bearer 0123456789abcdef0123456789abcdef")
    second = svc.handle(request, "Bearer 0123456789abcdef0123456789abcdef")
    assert first["ok"] is True
    assert second["code"] == "FABRIC_IDEMPOTENT_REPLAY"
    assert counter["count"] == 1


def test_peer_rejects_conflicting_attempt_reuse(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    envelope = envelope_for(svc, contract(tmp_path))
    svc.handle(accept_request(envelope), "Bearer 0123456789abcdef0123456789abcdef")
    forged = dict(envelope)
    forged["objective"] = "Different content under same attempt identity"
    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(
            accept_request(forged),
            "Bearer 0123456789abcdef0123456789abcdef",
        )
    assert exc.value.code == "FABRIC_IDEMPOTENCY_CONFLICT"


def test_peer_rejects_authority_widening(tmp_path, monkeypatch):
    svc = service(
        tmp_path,
        monkeypatch,
        policy_loader=lambda: policy(tmp_path, max_auth="read_only"),
    )
    envelope = envelope_for(svc, contract(tmp_path, auth_class="reversible_write"))
    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(
            accept_request(envelope),
            "Bearer 0123456789abcdef0123456789abcdef",
        )
    assert exc.value.code == "FABRIC_AUTHORITY_DENIED"


def test_coordinator_rejects_profile_outside_contract_scope_on_dry_run(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    coord = coordinator(tmp_path, svc)
    value = contract(tmp_path)
    value["allowed_scope"]["profiles"] = ["qa"]

    with pytest.raises(fabric.FabricError) as exc:
        coord.dispatch(value, dry_run=True, confirm=False, timeout=10)

    assert exc.value.code == "FABRIC_AUTHORITY_DENIED"
    assert "allowed_scope.profiles" in str(exc.value)


def test_peer_rejects_envelope_profile_outside_preserved_scope(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    value = contract(tmp_path)
    value["allowed_scope"]["profiles"] = ["qa"]
    envelope = envelope_for(svc, value)

    assert envelope["allowed_profiles"] == ["qa"]
    local = svc._local_contract(envelope, policy(tmp_path).workspace_mappings["repo"])
    assert local["allowed_scope"]["profiles"] == ["qa"]

    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(accept_request(envelope), "Bearer 0123456789abcdef0123456789abcdef")
    assert exc.value.code == "FABRIC_AUTHORITY_DENIED"


def test_remote_forbidden_actions_are_preserved_and_admitted(tmp_path, monkeypatch):
    observed: list[dict[str, str]] = []
    counter = {"count": 0}
    svc = service(tmp_path, monkeypatch, dispatch_counter=counter, observed=observed)
    value = contract(tmp_path)
    value["forbidden_actions"] = [
        {"action": "public_publish", "reason": "must remain private", "class": "HIGH"}
    ]
    envelope = envelope_for(svc, value)

    assert envelope["forbidden_actions"] == value["forbidden_actions"]
    local = svc._local_contract(envelope, policy(tmp_path).workspace_mappings["repo"])
    assert local["forbidden_actions"] == value["forbidden_actions"]

    coord = coordinator(tmp_path, svc)
    result = coord.dispatch(value, dry_run=False, confirm=True, timeout=10)
    assert result["success"] is True
    assert counter["count"] == 1
    observed.append(
        {
            "status": "completed",
            "outcome": "completed",
            "started_at": "2026-08-21T17:00:00Z",
            "ended_at": "2026-08-21T17:01:00Z",
            "error": "",
        }
    )
    coord.poll(result["attempt_id"], reconcile=True)
    admitted = coord.collect(result["attempt_id"])["evidence"]
    check = admitted["forbidden_check"]
    assert check["provenance"] == "managed_peer_audit"
    assert check["policy_sha256"] == fabric.sha256_json(value["forbidden_actions"])
    assert check["status"] == "PASS"
    assert check["signals"] == []


def test_remote_forbidden_violation_is_admitted_as_fail(tmp_path, monkeypatch):
    observed: list[dict[str, str]] = []
    svc = service(tmp_path, monkeypatch, observed=observed)
    value = contract(tmp_path)
    value["forbidden_actions"] = [
        {"action": "public_publish", "reason": "must remain private", "class": "HIGH"}
    ]
    coord = coordinator(tmp_path, svc)
    result = coord.dispatch(value, dry_run=False, confirm=True, timeout=10)
    assert result["success"] is True

    fabric.op.set_audit_log_override(tmp_path / "audit.jsonl")
    try:
        fabric.op.audit_record(
            tool="public_publish",
            level="workspace",
            apply_mode="direct",
            dry_run=False,
            success=True,
            profile="default",
            summary="public_publish attempted by remote runner",
            extra={"task_id": result["attempt_id"], "forbidden_action": "public_publish"},
        )
        observed.append(
            {
                "status": "completed",
                "outcome": "completed",
                "started_at": "2026-08-21T17:00:00Z",
                "ended_at": "2026-08-21T17:01:00Z",
                "error": "",
            }
        )
        coord.poll(result["attempt_id"], reconcile=True)
        check = coord.collect(result["attempt_id"])["evidence"]["forbidden_check"]
    finally:
        fabric.op.set_audit_log_override(None)

    assert check["status"] == "FAIL"
    assert check["signals"][0]["action"] == "public_publish"
    assert check["signals"][0]["class"] == "HIGH"


def test_coordinator_rejects_forbidden_evidence_policy_tamper(tmp_path, monkeypatch):
    observed = [
        {
            "status": "completed",
            "outcome": "completed",
            "started_at": "2026-08-21T17:00:00Z",
            "ended_at": "2026-08-21T17:01:00Z",
            "error": "",
        }
    ]
    svc = service(tmp_path, monkeypatch, observed=observed)
    value = contract(tmp_path)
    value["forbidden_actions"] = [
        {"action": "public_publish", "reason": "must remain private", "class": "HIGH"}
    ]

    def tampering_rpc(target, request, timeout):
        remote_task_id, response = rpc_for(svc)(target, request, timeout)
        if request["operation"] == "evidence":
            response = dict(response)
            response["data"] = dict(response["data"])
            evidence = dict(response["data"]["evidence"])
            check = dict(evidence["forbidden_check"])
            check["policy_sha256"] = "0" * 64
            evidence["forbidden_check"] = check
            response["data"]["evidence"] = evidence
        return remote_task_id, response

    coord = coordinator(tmp_path, svc, rpc=tampering_rpc)
    result = coord.dispatch(value, dry_run=False, confirm=True, timeout=10)
    coord.poll(result["attempt_id"], reconcile=True)
    with pytest.raises(fabric.FabricError) as exc:
        coord.collect(result["attempt_id"])
    assert exc.value.code == "FABRIC_EVIDENCE_LINEAGE_MISMATCH"



def test_prestart_policy_drift_blocks_runner(tmp_path, monkeypatch):
    calls = {"n": 0}
    first_policy = policy(tmp_path, revision="r1")
    changed_policy = policy(tmp_path, revision="r2")

    def loader():
        calls["n"] += 1
        return changed_policy if calls["n"] >= 3 else first_policy

    counter = {"count": 0}
    svc = service(
        tmp_path,
        monkeypatch,
        policy_loader=loader,
        dispatch_counter=counter,
    )
    envelope = envelope_for(svc, contract(tmp_path))
    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(
            accept_request(envelope),
            "Bearer 0123456789abcdef0123456789abcdef",
        )
    assert exc.value.code == "FABRIC_POLICY_DRIFT"
    assert counter["count"] == 0


def test_write_claim_blocks_second_write_in_same_conflict_domain(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    first = envelope_for(svc, contract(tmp_path, auth_class="reversible_write"))
    svc.handle(accept_request(first), "Bearer 0123456789abcdef0123456789abcdef")

    second_contract = contract(tmp_path, auth_class="reversible_write")
    second_contract["task_id"] = "task-fabric-2"
    second = envelope_for(svc, second_contract)
    with pytest.raises(fabric.FabricError) as exc:
        svc.handle(
            accept_request(second),
            "Bearer 0123456789abcdef0123456789abcdef",
        )
    assert exc.value.code == "FABRIC_WRITE_OWNERSHIP_BLOCKED"


def test_peer_restart_uses_durable_journal(tmp_path, monkeypatch):
    observed = []
    first_service = service(tmp_path, monkeypatch, observed=observed)
    envelope = envelope_for(first_service, contract(tmp_path))
    first_service.handle(
        accept_request(envelope),
        "Bearer 0123456789abcdef0123456789abcdef",
    )
    observed.append(
        {
            "status": "completed",
            "outcome": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:01:00Z",
        }
    )
    restarted = service(tmp_path, monkeypatch, observed=observed)
    status = restarted.handle(
        fabric._request(
            "reconcile",
            "coord-main",
            data={},
            dispatch_id=envelope["dispatch_id"],
            attempt_id=envelope["attempt_id"],
        ),
        "Bearer 0123456789abcdef0123456789abcdef",
    )
    assert status["data"]["state"] == "SUCCEEDED"


def test_coordinator_remote_happy_path_feeds_observed_run(tmp_path, monkeypatch):
    observed = []
    svc = service(tmp_path, monkeypatch, observed=observed)
    coord = coordinator(tmp_path, svc)
    result = coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
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
    runs = coord.observed_runs("task-fabric-1")
    assert len(runs) == 1
    assert runs[0]["outcome"] == "completed"
    assert runs[0]["scope"] == "fabric:node-a"
    assert runs[0]["evidence_provenance"] == "managed_peer_structured"


def test_coordinator_duplicate_dispatch_does_not_resubmit(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    calls = {"accept": 0}

    def rpc(target, request, timeout):
        if request["operation"] == "accept":
            calls["accept"] += 1
        return rpc_for(svc)(target, request, timeout)

    coord = coordinator(tmp_path, svc, rpc=rpc)
    first = coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    second = coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    assert first["success"] is True
    assert second["idempotent"] is True
    assert calls["accept"] == 1


def test_timeout_after_submit_is_ambiguous_and_never_blindly_retried(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    calls = {"accept": 0}

    def rpc(target, request, timeout):
        if request["operation"] == "capabilities":
            return rpc_for(svc)(target, request, timeout)
        if request["operation"] == "accept":
            calls["accept"] += 1
            raise fabric.FabricError(
                "FABRIC_TRANSPORT_TIMEOUT",
                "timed out",
                ambiguous=True,
            )
        return rpc_for(svc)(target, request, timeout)

    coord = coordinator(tmp_path, svc, rpc=rpc)
    first = coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    second = coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    assert first["success"] is False
    assert first["state"] == "SUBMISSION_AMBIGUOUS"
    assert first["submission_may_have_succeeded"] is True
    assert second["success"] is False
    assert second["idempotent"] is True
    assert calls["accept"] == 1


def test_wrong_lineage_and_self_certifying_evidence_are_rejected(tmp_path, monkeypatch):
    observed = [
        {
            "status": "completed",
            "outcome": "completed",
            "started_at": "s",
            "ended_at": "e",
            "error": "",
        }
    ]
    svc = service(tmp_path, monkeypatch, observed=observed)
    coord = coordinator(tmp_path, svc)
    result = coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    coord.poll(result["attempt_id"])
    attempt, _dispatch, target = coord._attempt(result["attempt_id"])
    evidence = svc.handle(
        fabric._request(
            "evidence",
            "coord-main",
            data={},
            dispatch_id=result["dispatch_id"],
            attempt_id=result["attempt_id"],
        ),
        "Bearer 0123456789abcdef0123456789abcdef",
    )["data"]["evidence"]
    attempt_map = dict(attempt)
    attempt_map["_coordinator_db"] = str(coord.db_path)

    forged = dict(evidence)
    forged["contract_sha256"] = "0" * 64
    with pytest.raises(fabric.FabricError) as exc:
        fabric._validate_evidence(
            forged,
            attempt=attempt_map,
            node=target,
            allowed_provenance=("managed_peer_structured",),
        )
    assert exc.value.code == "FABRIC_EVIDENCE_LINEAGE_MISMATCH"

    self_certifying = dict(evidence)
    self_certifying["verdict"] = "SATISFIED"
    with pytest.raises(fabric.FabricError) as exc:
        fabric._validate_evidence(
            self_certifying,
            attempt=attempt_map,
            node=target,
            allowed_provenance=("managed_peer_structured",),
        )
    assert exc.value.code == "FABRIC_SCHEMA_INVALID"

    missing_forbidden = dict(evidence)
    missing_forbidden.pop("forbidden_check", None)
    with pytest.raises(fabric.FabricError) as exc:
        fabric._validate_evidence(
            missing_forbidden,
            attempt=attempt_map,
            node=target,
            allowed_provenance=("managed_peer_structured",),
        )
    assert exc.value.code == "FABRIC_EVIDENCE_REJECTED"

    forged_forbidden = dict(evidence)
    forged_forbidden["forbidden_check"] = dict(evidence["forbidden_check"])
    forged_forbidden["forbidden_check"]["policy_sha256"] = "0" * 64
    with pytest.raises(fabric.FabricError) as exc:
        fabric._validate_evidence(
            forged_forbidden,
            attempt=attempt_map,
            node=target,
            allowed_provenance=("managed_peer_structured",),
        )
    assert exc.value.code == "FABRIC_EVIDENCE_LINEAGE_MISMATCH"


def test_worker_statement_cannot_satisfy_run_state(tmp_path, monkeypatch):
    observed = [
        {
            "status": "completed",
            "outcome": "completed",
            "started_at": "s",
            "ended_at": "e",
            "error": "",
        }
    ]
    svc = service(tmp_path, monkeypatch, observed=observed)
    coord = coordinator(tmp_path, svc)
    result = coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    coord.poll(result["attempt_id"])
    attempt, _dispatch, target = coord._attempt(result["attempt_id"])
    evidence = svc.handle(
        fabric._request(
            "evidence",
            "coord-main",
            data={},
            dispatch_id=result["dispatch_id"],
            attempt_id=result["attempt_id"],
        ),
        "Bearer 0123456789abcdef0123456789abcdef",
    )["data"]["evidence"]
    evidence["observations"][0]["provenance"] = "worker_statement"
    attempt_map = dict(attempt)
    attempt_map["_coordinator_db"] = str(coord.db_path)
    with pytest.raises(fabric.FabricError) as exc:
        fabric._validate_evidence(
            evidence,
            attempt=attempt_map,
            node=target,
            allowed_provenance=("managed_peer_structured", "worker_statement"),
        )
    assert exc.value.code == "FABRIC_EVIDENCE_PROVENANCE_REJECTED"


def test_cancel_is_attempt_specific(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    coord = coordinator(tmp_path, svc)
    result = coord.dispatch(contract(tmp_path), dry_run=False, confirm=True, timeout=10)
    cancelled = coord.cancel(result["attempt_id"])
    assert cancelled["state"] == "CANCELLED"
    assert cancelled["attempt_id"] == result["attempt_id"]


def test_http_peer_rejects_generic_text_before_any_agent_path(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    server = ThreadingHTTPServer(("127.0.0.1", 0), fabric._PeerHandler)
    host, port = server.server_address
    server.fabric_service = svc
    server.fabric_advertised_url = f"http://{host}:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [
                        {
                            "text": "please execute this as verified Fabric",
                            "mediaType": "text/plain",
                        }
                    ],
                    "messageId": "msg-1",
                    "contextId": "ctx-1",
                }
            },
        }
        request = urllib.request.Request(
            f"http://{host}:{port}",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer 0123456789abcdef0123456789abcdef",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["error"]["data"]["code"] == "FABRIC_PROTOCOL_ERROR"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
