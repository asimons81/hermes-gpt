"""Hermes GPT v0.8 Fabric G4-C integration layer.

G4-C extends the existing managed A2A Fabric path with durable write ownership,
whole-tree writer containment, restart/cancel/retry reconciliation, and immutable
remote artifact admission. It deliberately does not replace the G4-A peer class
globally: legacy callers keep their original behavior while the packaged Fabric
peer and registered Fabric/auto backends use this enhanced runtime explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import ssl
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import fabric_artifacts as artifacts
import fabric_write_guard as write_guard
import operator_fabric as base
import operator_fabric_router as router
import operator_runners as runners

ARTIFACT_MANIFEST_SCHEMA = artifacts.ARTIFACT_MANIFEST_SCHEMA
ARTIFACT_CHUNK_SCHEMA = artifacts.ARTIFACT_CHUNK_SCHEMA
FEATURE_ARTIFACT = artifacts.FEATURE_ARTIFACT
FEATURE_ARTIFACT_SNAPSHOT = artifacts.FEATURE_ARTIFACT_SNAPSHOT
FEATURE_WRITE_OWNERSHIP = write_guard.FEATURE_WRITE_OWNERSHIP
FEATURE_EXECUTION_UNIT = write_guard.FEATURE_EXECUTION_UNIT
FEATURE_WRITE_EPOCH = write_guard.FEATURE_WRITE_EPOCH
FEATURE_RECONCILE = "reconcile-v1"

_WRITE_CLAIM_STATES = frozenset({"NONE", "ACTIVE", "RELEASED", "SUPERSEDED", "UNKNOWN"})
_EXECUTION_UNIT_STATES = frozenset(
    {"active", "activating", "deactivating", "reloading", "inactive", "failed", "dead", "not-found", "terminal", "unknown"}
)

FabricError = base.FabricError
FabricNode = base.FabricNode
FabricPeerPolicy = base.FabricPeerPolicy
WorkspaceMapping = base.WorkspaceMapping
SystemdUserUnitManager = write_guard.SystemdUserUnitManager
canonical_json = base.canonical_json
sha256_json = base.sha256_json
strict_json_loads = base.strict_json_loads
load_node_registry = base.load_node_registry
load_peer_policy = base.load_peer_policy
load_peer_tokens = base.load_peer_tokens
_logical_artifact_name = artifacts.logical_name


def _validate_request(value: Any) -> dict[str, Any]:
    request = base._closed(
        value,
        required={
            "schema",
            "version",
            "operation",
            "coordinator_principal",
            "request_id",
            "data",
        },
        optional={"dispatch_id", "attempt_id"},
        name="Fabric request",
    )
    if request["schema"] != base.REQUEST_SCHEMA or request["version"] != base.FABRIC_VERSION:
        raise FabricError(
            "FABRIC_PROTOCOL_INCOMPATIBLE",
            "Fabric request schema/version is unsupported",
        )
    operation = base._bounded_string(request["operation"], field="operation", maximum=32)
    if operation not in {
        "capabilities",
        "accept",
        "status",
        "reconcile",
        "cancel",
        "evidence",
        "artifact_manifest",
        "artifact_chunk",
    }:
        raise FabricError("FABRIC_OPERATION_UNSUPPORTED", "Fabric operation is unsupported")
    base._bounded_string(
        request["coordinator_principal"],
        field="coordinator_principal",
        pattern=base._PRINCIPAL_RE,
    )
    base._bounded_string(request["request_id"], field="request_id", pattern=base._ID_RE)
    if "dispatch_id" in request:
        base._bounded_string(request["dispatch_id"], field="dispatch_id", pattern=base._ID_RE)
    if "attempt_id" in request:
        base._bounded_string(request["attempt_id"], field="attempt_id", pattern=base._ID_RE)
    if not isinstance(request["data"], dict):
        raise FabricError("FABRIC_SCHEMA_INVALID", "Fabric request data must be an object")
    return request


def _validate_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FabricError("FABRIC_SCHEMA_INVALID", "Fabric dispatch envelope must be an object")
    normalized = dict(value)
    retry_parent = normalized.pop("retry_parent_attempt_id", None)
    envelope = base._validate_envelope(normalized)
    if retry_parent is not None:
        envelope["retry_parent_attempt_id"] = base._bounded_string(
            retry_parent,
            field="retry_parent_attempt_id",
            pattern=base._ID_RE,
        )
    return envelope


def _run_state(run: dict[str, Any] | None) -> str:
    if not run:
        return ""
    return str(run.get("state") or run.get("status") or "").lower()


def _terminal_state(run: dict[str, Any] | None) -> str:
    state = _run_state(run)
    if state in {"completed", "succeeded", "success"}:
        return "SUCCEEDED"
    if state in {"failed", "error"}:
        return "FAILED"
    if state in {"cancelled", "canceled"}:
        return "CANCELLED"
    return ""


def _bounded_peer_observation(key: str, value: Any) -> str:
    allowed = _WRITE_CLAIM_STATES if key == "write_claim_state" else _EXECUTION_UNIT_STATES
    text = str(value or "")
    return text if text in allowed else "UNKNOWN" if key == "write_claim_state" else "unknown"


class FabricPeerService(base.FabricPeerService):
    """Managed peer with G4-C writer and artifact guarantees."""

    def __init__(
        self,
        *,
        unit_manager: Any | None = None,
        write_dispatch_fn: Callable[..., dict[str, Any]] | None = None,
        artifact_root: Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        write_guard.migrate_peer(self.db_path)
        artifacts.migrate_peer(self.db_path)
        self.claims = write_guard.WriteClaims(self.db_path)
        self.unit_manager = unit_manager or SystemdUserUnitManager()
        self.write_dispatch_fn = write_dispatch_fn or self._dispatch_contained_write
        self._invocations_in_flight: dict[str, object] = {}
        self._invocations_in_flight_lock = threading.Lock()
        snapshot_root = artifact_root or base._root(self.hermes_root) / "fabric" / "artifacts"
        self.artifact_store = artifacts.PeerArtifactStore(self.db_path, snapshot_root)

    def _invocation_in_flight(self, attempt_id: str) -> bool:
        with self._invocations_in_flight_lock:
            return attempt_id in self._invocations_in_flight

    def _owns_invocation(self, attempt_id: str, marker: object) -> bool:
        with self._invocations_in_flight_lock:
            return self._invocations_in_flight.get(attempt_id) is marker

    def capabilities(self, policy: FabricPeerPolicy) -> dict[str, Any]:
        payload = super().capabilities(policy)
        features = list(payload["features"])
        for feature in (*artifacts.ARTIFACT_FEATURES, FEATURE_RECONCILE):
            if feature not in features:
                features.append(feature)
        if self.unit_manager.available():
            for feature in write_guard.WRITE_FEATURES:
                if feature not in features:
                    features.append(feature)
        payload["features"] = features
        payload["operations"] = [
            "capabilities",
            "accept",
            "status",
            "reconcile",
            "cancel",
            "evidence",
            "artifact_manifest",
            "artifact_chunk",
        ]
        payload.pop("snapshot_sha256", None)
        payload["snapshot_sha256"] = sha256_json(payload)
        return payload

    def handle(self, request_value: dict[str, Any], authorization: str) -> dict[str, Any]:
        request = _validate_request(request_value)
        principal = self.authenticate(authorization)
        if principal != request["coordinator_principal"]:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "authenticated principal does not match request",
            )
        policy = self.policy_loader()
        if principal not in policy.allowed_coordinator_principals:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "coordinator principal is not authorized by peer policy",
            )
        operation = request["operation"]
        if operation == "capabilities":
            base._closed(request["data"], required=set(), name="capabilities data")
            return base._response(
                operation,
                ok=True,
                code="FABRIC_OK",
                data=self.capabilities(policy),
            )
        if operation == "accept":
            return self._accept(request, principal, policy)

        dispatch_id = base._bounded_string(
            request.get("dispatch_id"),
            field="dispatch_id",
            pattern=base._ID_RE,
        )
        attempt_id = base._bounded_string(
            request.get("attempt_id"),
            field="attempt_id",
            pattern=base._ID_RE,
        )
        if operation in {"status", "reconcile", "cancel", "evidence"}:
            base._closed(request["data"], required=set(), name=f"{operation} data")
        if operation == "status":
            data = self._status(dispatch_id, attempt_id, reconcile=False)
        elif operation == "reconcile":
            data = self._status(dispatch_id, attempt_id, reconcile=True)
        elif operation == "cancel":
            data = self._cancel(dispatch_id, attempt_id, principal, policy)
        elif operation == "evidence":
            data = self._evidence(dispatch_id, attempt_id, principal, policy)
        elif operation == "artifact_manifest":
            data = self._artifact_manifest(request, principal, policy)
        elif operation == "artifact_chunk":
            data = self._artifact_chunk(request, principal, policy)
        else:
            raise FabricError("FABRIC_OPERATION_UNSUPPORTED", "unsupported Fabric operation")
        return base._response(operation, ok=True, code="FABRIC_OK", data=data)

    def _write_backend_eligible(self, backend_name: str) -> None:
        if not self.unit_manager.available():
            raise FabricError(
                "FABRIC_EXECUTION_UNIT_UNAVAILABLE",
                "verified Fabric writes require a usable whole-tree execution unit",
            )
        try:
            backend = runners.get_backend(backend_name)
        except LookupError as exc:
            raise FabricError("FABRIC_RUNNER_UNAVAILABLE", "remote runner is not registered") from exc
        if not isinstance(backend, runners._LocalProcessBackend):
            raise FabricError(
                "FABRIC_EXECUTION_UNIT_UNSUPPORTED",
                "remote backend does not support verified whole-tree write containment",
            )

    def _dispatch_contained_write(
        self,
        contract: dict[str, Any],
        *,
        backend_name: str,
        unit_id: str,
        timeout: int,
    ) -> dict[str, Any]:
        backend = runners.get_backend(backend_name)
        if not isinstance(backend, runners._LocalProcessBackend):
            return {"success": False, "code": "FABRIC_EXECUTION_UNIT_UNSUPPORTED"}
        workspace = backend._policy_workspace(contract)
        if not backend.executable():
            return {"success": False, "code": "RUNNER_UNAVAILABLE"}
        backend.build_plan(contract)
        task_id = str(contract["task_id"])
        meta_path, request_path, _log_path = runners._job_paths(task_id, self.hermes_root)
        if meta_path.exists():
            return {"success": False, "code": "RUNNER_JOB_EXISTS"}
        request = {
            "backend": backend_name,
            "contract": contract,
            "timeout": max(10, min(int(timeout), 3600)),
            "hermes_root": str((self.hermes_root or Path.home() / ".hermes").expanduser()),
        }
        runners._atomic_json(request_path, request)
        runners._atomic_json(
            meta_path,
            {
                "schema_version": runners.SCHEMA_VERSION,
                "task_id": task_id,
                "backend": backend_name,
                "state": "queued",
                "outcome": "",
                "workspace": str(workspace),
                "created_at": runners._now(),
                "started_at": None,
                "ended_at": None,
                "pid": None,
                "returncode": None,
                "error": "",
            },
        )
        launch = self.unit_manager.launch(
            unit_id,
            task_id,
            workspace,
            runners._root(self.hermes_root),
            Path(runners.__file__).resolve(),
        )
        if launch.get("accepted"):
            return {
                "success": True,
                "changed": True,
                "state": "queued",
                "backend": backend_name,
                "task_id": task_id,
            }
        if launch.get("ambiguous"):
            return {
                "success": False,
                "changed": True,
                "ambiguous": True,
                "code": str(
                    launch.get("code") or "FABRIC_EXECUTION_UNIT_START_AMBIGUOUS"
                ),
            }
        try:
            request_path.unlink()
        except OSError:
            pass
        return {
            "success": False,
            "changed": False,
            "code": str(launch.get("code") or "FABRIC_EXECUTION_UNIT_START_FAILED"),
        }

    def _accept(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        invocation_marker = object()
        invocation_markers_owned: set[str] = set()
        try:
            return self._accept_impl(
                request,
                principal,
                policy,
                invocation_marker,
                invocation_markers_owned,
            )
        finally:
            if invocation_markers_owned:
                with self._invocations_in_flight_lock:
                    for attempt_id in invocation_markers_owned:
                        if self._invocations_in_flight.get(attempt_id) is invocation_marker:
                            del self._invocations_in_flight[attempt_id]

    def _accept_impl(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
        invocation_marker: object,
        invocation_markers_owned: set[str],
    ) -> dict[str, Any]:
        data = base._closed(request["data"], required={"envelope"}, name="accept data")
        envelope = _validate_envelope(data["envelope"])
        if request.get("dispatch_id") and request["dispatch_id"] != envelope["dispatch_id"]:
            raise FabricError(
                "FABRIC_IDEMPOTENCY_CONFLICT",
                "request and envelope dispatch identity differ",
            )
        if request.get("attempt_id") and request["attempt_id"] != envelope["attempt_id"]:
            raise FabricError(
                "FABRIC_IDEMPOTENCY_CONFLICT",
                "request and envelope attempt identity differ",
            )
        mapping = self._authorize_envelope(envelope, principal, policy)
        envelope_sha = sha256_json(data["envelope"])
        is_write = write_guard.is_write(envelope)
        unit_id = ""
        if is_write:
            self._write_backend_eligible(envelope["remote_backend"])
            unit_id = self.unit_manager.unit_name(envelope["attempt_id"])

        with self._lock, base._connect(self.db_path) as db:
            existing = db.execute(
                "SELECT * FROM attempts WHERE attempt_id=?",
                (envelope["attempt_id"],),
            ).fetchone()
            if existing is not None:
                if (
                    existing["dispatch_id"] != envelope["dispatch_id"]
                    or existing["envelope_sha256"] != envelope_sha
                ):
                    raise FabricError(
                        "FABRIC_IDEMPOTENCY_CONFLICT",
                        "attempt identity was reused with different canonical content",
                    )
                return base._response(
                    "accept",
                    ok=True,
                    code="FABRIC_IDEMPOTENT_REPLAY",
                    data={
                        "dispatch_id": existing["dispatch_id"],
                        "attempt_id": existing["attempt_id"],
                        "state": existing["state"],
                        "local_task_id": existing["local_task_id"],
                        "policy_sha256": existing["policy_sha256"],
                        "write_epoch": existing["write_epoch"],
                        "write_claim_state": self.claims.state(existing),
                        "execution_unit_state": _bounded_peer_observation(
                            "execution_unit_state",
                            self.unit_manager.inspect(str(existing["execution_unit_id"] or "")).get("state"),
                        ),
                    },
                )
            epoch = None
            if is_write:
                epoch = self.claims.acquire(
                    db,
                    conflict_domain=mapping.conflict_domain,
                    attempt_id=envelope["attempt_id"],
                    unit_id=unit_id,
                )
            now = base._now()
            db.execute(
                "INSERT INTO attempts"
                "(attempt_id,dispatch_id,envelope_sha256,contract_sha256,task_id,coordinator_principal,"
                "node_name,remote_backend,logical_workspace,conflict_domain,authorization_class,policy_sha256,"
                "local_task_id,state,created_at,updated_at,write_epoch,execution_unit_kind,execution_unit_id,retry_parent_attempt_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    envelope["attempt_id"],
                    envelope["dispatch_id"],
                    envelope_sha,
                    envelope["contract_sha256"],
                    envelope["task_id"],
                    principal,
                    policy.node_name,
                    envelope["remote_backend"],
                    envelope["logical_workspace"],
                    mapping.conflict_domain,
                    envelope["authorization"]["class"],
                    policy.digest,
                    envelope["attempt_id"],
                    "ACCEPTED",
                    now,
                    now,
                    epoch,
                    "systemd-user-unit" if is_write else None,
                    unit_id or None,
                    envelope.get("retry_parent_attempt_id"),
                ),
            )
            if is_write:
                # Install the process-local marker before ACCEPTED can become
                # visible outside this transaction.  Only this inserting
                # invocation owns the token and may clear it in _accept's
                # finally block.
                with self._invocations_in_flight_lock:
                    self._invocations_in_flight[envelope["attempt_id"]] = invocation_marker
                invocation_markers_owned.add(envelope["attempt_id"])

        try:
            prestart = self.policy_loader()
            prestart_mapping = self._authorize_envelope(envelope, principal, prestart)
            mapping_changed = (
                prestart_mapping.local_path != mapping.local_path
                or prestart_mapping.revision != mapping.revision
                or prestart_mapping.conflict_domain != mapping.conflict_domain
            )
            if mapping_changed:
                raise FabricError(
                    "FABRIC_POLICY_DRIFT",
                    "peer workspace policy changed before runner start",
                )
            backend = runners.get_backend(envelope["remote_backend"])
            if not bool(backend.availability(hermes_root=self.hermes_root).get("available")):
                raise FabricError(
                    "FABRIC_RUNNER_UNAVAILABLE",
                    "remote runner is unavailable at pre-start revalidation",
                )
            if is_write:
                self._write_backend_eligible(envelope["remote_backend"])
        except (FabricError, LookupError) as exc:
            with self._lock, base._connect(self.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT state FROM attempts WHERE attempt_id=?",
                    (envelope["attempt_id"],),
                ).fetchone()
                terminal_state = (
                    "CANCELLED"
                    if current is not None
                    and current["state"] == "PRELAUNCH_CANCEL_REQUESTED"
                    else "BLOCKED"
                )
                changed = db.execute(
                    "UPDATE attempts SET state=?,updated_at=?"
                    " WHERE attempt_id=?"
                    " AND state IN ('ACCEPTED','PRELAUNCH_CANCEL_REQUESTED')",
                    (terminal_state, base._now(), envelope["attempt_id"]),
                )
            row = self._row(envelope["dispatch_id"], envelope["attempt_id"])
            if is_write and changed.rowcount:
                self.claims.release(
                    row,
                    proof=(
                        "prelaunch_cancelled_no_execution"
                        if terminal_state == "CANCELLED"
                        else "prestart_blocked_no_execution"
                    ),
                )
            if isinstance(exc, FabricError):
                raise
            raise FabricError("FABRIC_RUNNER_UNAVAILABLE", "remote runner is not registered") from exc

        local_contract = self._local_contract(envelope, prestart_mapping)
        state_persisted = False
        if is_write:
            # ACCEPTED is the only prelaunch-cancellable state.  Commit launch
            # permission durably while serialized with cancellation, then use
            # one short final fence check before calling external code.  Never
            # hold the process lock or a SQLite transaction across dispatch.
            with self._lock, base._connect(self.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                launch_row = db.execute(
                    "SELECT * FROM attempts WHERE attempt_id=?",
                    (envelope["attempt_id"],),
                ).fetchone()
                claim = db.execute(
                    "SELECT * FROM write_claims WHERE conflict_domain=?",
                    (mapping.conflict_domain,),
                ).fetchone()
                owns_claim = bool(
                    launch_row is not None
                    and launch_row["state"] == "ACCEPTED"
                    and claim is not None
                    and claim["state"] == "ACTIVE"
                    and claim["attempt_id"] == envelope["attempt_id"]
                    and int(claim["epoch"] or 0) == int(launch_row["write_epoch"] or 0)
                    and self._owns_invocation(envelope["attempt_id"], invocation_marker)
                )
                if owns_claim:
                    db.execute(
                        "UPDATE attempts SET state='LAUNCHING',updated_at=?"
                        " WHERE attempt_id=? AND state='ACCEPTED'",
                        (base._now(), envelope["attempt_id"]),
                    )
            with self._lock:
                owns_invocation = self._owns_invocation(
                    envelope["attempt_id"], invocation_marker
                )
                with base._connect(self.db_path) as db:
                    launch_row = db.execute(
                        "SELECT * FROM attempts WHERE attempt_id=?",
                        (envelope["attempt_id"],),
                    ).fetchone()
                    claim = db.execute(
                        "SELECT * FROM write_claims WHERE conflict_domain=?",
                        (mapping.conflict_domain,),
                    ).fetchone()
                    owns_launch_fence = bool(
                        owns_claim
                        and launch_row is not None
                        and launch_row["state"] in {"LAUNCHING", "CANCEL_REQUESTED"}
                        and owns_invocation
                        and claim is not None
                        and claim["state"] == "ACTIVE"
                        and claim["attempt_id"] == envelope["attempt_id"]
                        and int(claim["epoch"] or 0)
                        == int(launch_row["write_epoch"] or 0)
                    )
            if owns_launch_fence:
                try:
                    result = self.write_dispatch_fn(
                        local_contract,
                        backend_name=envelope["remote_backend"],
                        unit_id=unit_id,
                        timeout=30,
                    )
                except Exception:  # noqa: BLE001 - external runner boundary fails closed
                    # Once the external launcher has been invoked, an exception
                    # cannot prove that no write execution occurred. A unit that
                    # is already quiescent may still have run and mutated state.
                    # Preserve durable ownership and force reconciliation.
                    result = {
                        "success": False,
                        "changed": False,
                        "ambiguous": True,
                        "code": "FABRIC_WRITE_LAUNCH_EXCEPTION_AMBIGUOUS",
                    }
            else:
                result = {
                    "success": False,
                    "changed": False,
                    "code": "FABRIC_LAUNCH_FENCE_REVOKED",
                }
            if not isinstance(result, dict):
                result = {"success": False, "code": "FABRIC_RUNNER_INVALID_RESULT"}
            launch_state = (
                "LOST_AMBIGUOUS"
                if result.get("ambiguous")
                else "RUNNING"
                if result.get("success")
                else "FAILED"
            )
            release_cancelled_launch = False
            with base._connect(self.db_path) as db:
                persisted_state = launch_state
                changed = db.execute(
                    "UPDATE attempts SET state=CASE WHEN state='CANCEL_REQUESTED'"
                    " THEN state ELSE ? END,dispatch_result_json=?,policy_sha256=?,updated_at=?"
                    " WHERE attempt_id=? AND state IN ('LAUNCHING','CANCEL_REQUESTED')",
                    (
                        persisted_state,
                        canonical_json(base._bounded_json(result, field="dispatch_result")),
                        prestart.digest,
                        base._now(),
                        envelope["attempt_id"],
                    ),
                )
                if changed.rowcount:
                    current = db.execute(
                        "SELECT state FROM attempts WHERE attempt_id=?",
                        (envelope["attempt_id"],),
                    ).fetchone()
                    persisted_state = str(current["state"])
                if not changed.rowcount:
                    current = db.execute(
                        "SELECT state FROM attempts WHERE attempt_id=?",
                        (envelope["attempt_id"],),
                    ).fetchone()
                    cancel_during_launch = bool(
                        current is not None
                        and current["state"] == "PRELAUNCH_CANCEL_REQUESTED"
                    )
                    if cancel_during_launch and (
                        result.get("success") or result.get("ambiguous")
                    ):
                        persisted_state = "CANCEL_REQUESTED"
                        db.execute(
                            "UPDATE attempts SET state='CANCEL_REQUESTED',"
                            "dispatch_result_json=?,policy_sha256=?,updated_at=?"
                            " WHERE attempt_id=? AND state='PRELAUNCH_CANCEL_REQUESTED'",
                            (
                                canonical_json(
                                    base._bounded_json(result, field="dispatch_result")
                                ),
                                prestart.digest,
                                base._now(),
                                envelope["attempt_id"],
                            ),
                        )
                    elif cancel_during_launch:
                        if result.get("code") == "FABRIC_LAUNCH_FENCE_REVOKED":
                            persisted_state = "CANCELLED"
                            release_cancelled_launch = True
                        else:
                            persisted_state = "LOST_AMBIGUOUS"
                        db.execute(
                            "UPDATE attempts SET state=?,dispatch_result_json=?,"
                            "policy_sha256=?,updated_at=?"
                            " WHERE attempt_id=? AND state='PRELAUNCH_CANCEL_REQUESTED'",
                            (
                                persisted_state,
                                canonical_json(
                                    base._bounded_json(result, field="dispatch_result")
                                ),
                                prestart.digest,
                                base._now(),
                                envelope["attempt_id"],
                            ),
                        )
                    elif current is not None:
                        persisted_state = str(current["state"])
            if release_cancelled_launch:
                self.claims.release(
                    self._row(envelope["dispatch_id"], envelope["attempt_id"]),
                    proof=(
                        "launch_fence_revoked_cancelled_no_execution"
                        if result.get("code") == "FABRIC_LAUNCH_FENCE_REVOKED"
                        else "cancelled_launch_failure_unit_quiescent"
                    ),
                )
            launch_state = persisted_state
            state_persisted = True
        else:
            result = self.dispatch_fn(local_contract, timeout=30)
        if not isinstance(result, dict):
            result = {"success": False, "code": "FABRIC_RUNNER_INVALID_RESULT"}
        state = (
            "LOST_AMBIGUOUS"
            if is_write and result.get("ambiguous")
            else "RUNNING"
            if result.get("success")
            else "FAILED"
        )
        if is_write and state_persisted:
            state = launch_state
        if not state_persisted:
            with base._connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,dispatch_result_json=?,policy_sha256=?,updated_at=?"
                    " WHERE attempt_id=?",
                    (
                        state,
                        canonical_json(base._bounded_json(result, field="dispatch_result")),
                        prestart.digest,
                        base._now(),
                        envelope["attempt_id"],
                    ),
                )
        row = self._row(envelope["dispatch_id"], envelope["attempt_id"])
        if is_write and state == "CANCEL_REQUESTED":
            unit_state = self.unit_manager.stop(unit_id)
            if unit_state.get("quiescent"):
                cancel_result = self.cancel_fn(row["remote_backend"], row["local_task_id"])
                terminal = _terminal_state(base._latest_run(self.observed_fn(row["local_task_id"])))
                state = terminal or "CANCELLED"
                self.claims.release(row, proof="cancel_execution_unit_quiescent")
                with base._connect(self.db_path) as db:
                    db.execute(
                        "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                        (state, base._now(), envelope["attempt_id"]),
                    )
                result = dict(result)
                result["cancel_changed"] = bool(cancel_result.get("changed", True))
        if is_write and state == "FAILED":
            if result.get("code") == "FABRIC_LAUNCH_FENCE_REVOKED":
                # The transactional ownership check proved launch was never
                # invoked.  Release only now, after the accepting call has
                # observed and persisted the revoked fence.
                self.claims.release(row, proof="launch_fence_revoked_no_execution")
            else:
                unit_state = self.unit_manager.inspect(unit_id)
                if unit_state.get("quiescent"):
                    self.claims.release(row, proof="known_start_failure_unit_quiescent")
                else:
                    state = "LOST_AMBIGUOUS"
                    with base._connect(self.db_path) as db:
                        db.execute(
                            "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                            (state, base._now(), envelope["attempt_id"]),
                        )
        return base._response(
            "accept",
            ok=bool(result.get("success")),
            code=(
                "FABRIC_ACCEPTED"
                if result.get("success")
                else str(result.get("code") or "FABRIC_RUNNER_REJECTED")
            ),
            data={
                "dispatch_id": envelope["dispatch_id"],
                "attempt_id": envelope["attempt_id"],
                "state": state,
                "local_task_id": envelope["attempt_id"],
                "policy_sha256": prestart.digest,
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state",
                    "unknown"
                    if result.get("code") == "FABRIC_LAUNCH_FENCE_REVOKED"
                    else self.unit_manager.inspect(
                        str(row["execution_unit_id"] or "")
                    ).get("state"),
                ),
            },
        )

    def _status(
        self,
        dispatch_id: str,
        attempt_id: str,
        *,
        reconcile: bool,
    ) -> dict[str, Any]:
        row = self._row(dispatch_id, attempt_id)
        if row["authorization_class"] not in write_guard.WRITE_AUTH:
            return super()._status(dispatch_id, attempt_id, reconcile=reconcile)
        if row["state"] == "ACCEPTED":
            invocation_in_flight = self._invocation_in_flight(attempt_id)
            if reconcile and not invocation_in_flight:
                proof = "abandoned_accepted_prelaunch_no_execution"
                with self._lock, base._connect(self.db_path) as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute(
                        "SELECT * FROM attempts WHERE attempt_id=? AND dispatch_id=?",
                        (attempt_id, dispatch_id),
                    ).fetchone()
                    if (
                        current is not None
                        and current["state"] == "ACCEPTED"
                        and not self._invocation_in_flight(attempt_id)
                    ):
                        # Recheck while serialized with insertion: launch is
                        # invoked only after durable LAUNCHING, so absence of
                        # this invocation marker positively proves that an
                        # ACCEPTED attempt never reached external execution.
                        db.execute(
                            "UPDATE attempts SET state='BLOCKED',updated_at=?"
                            " WHERE attempt_id=? AND state='ACCEPTED'",
                            (base._now(), attempt_id),
                        )
                        db.execute(
                            "UPDATE write_claims SET state='RELEASED',released_at=?,"
                            "release_proof=? WHERE conflict_domain=? AND attempt_id=?"
                            " AND epoch=? AND state='ACTIVE'",
                            (
                                base._now(),
                                proof,
                                current["conflict_domain"],
                                current["attempt_id"],
                                int(current["write_epoch"]),
                            ),
                        )
                row = self._row(dispatch_id, attempt_id)
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "local_task_id": row["local_task_id"],
                "policy_sha256": row["policy_sha256"],
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state",
                    self.unit_manager.inspect(str(row["execution_unit_id"] or "")).get("state"),
                ),
            }
        if row["state"] == "CANCEL_REQUESTED" and self._invocation_in_flight(attempt_id):
            unit = self.unit_manager.inspect(str(row["execution_unit_id"] or ""))
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": "CANCEL_REQUESTED",
                "local_task_id": row["local_task_id"],
                "policy_sha256": row["policy_sha256"],
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state", unit.get("state")
                ),
            }
        if row["state"] in {"LAUNCHING", "PRELAUNCH_CANCEL_REQUESTED"}:
            unit_id = str(row["execution_unit_id"] or "")
            run = base._latest_run(self.observed_fn(row["local_task_id"]))
            terminal = _terminal_state(run)
            unit = self.unit_manager.inspect(unit_id)
            invocation_in_flight = self._invocation_in_flight(attempt_id)
            if invocation_in_flight or (row["state"] == "LAUNCHING" and not reconcile):
                state = (
                    "CANCEL_REQUESTED"
                    if row["state"] == "PRELAUNCH_CANCEL_REQUESTED"
                    else "LAUNCHING"
                )
                return {
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "state": state,
                    "local_task_id": row["local_task_id"],
                    "policy_sha256": row["policy_sha256"],
                    "write_epoch": row["write_epoch"],
                    "write_claim_state": self.claims.state(row),
                    "execution_unit_state": _bounded_peer_observation(
                        "execution_unit_state", unit.get("state")
                    ),
                }
            if row["state"] == "PRELAUNCH_CANCEL_REQUESTED" and unit.get("active") and reconcile:
                unit = self.unit_manager.stop(unit_id)
            if terminal and unit.get("quiescent"):
                state = terminal
                self.claims.release(row, proof="launching_unit_terminal_and_quiescent")
            elif row["state"] == "PRELAUNCH_CANCEL_REQUESTED" and unit.get("quiescent"):
                state = "CANCELLED"
                self.claims.release(row, proof="cancelled_launch_unit_quiescent")
            elif unit.get("active"):
                state = (
                    "CANCEL_REQUESTED"
                    if row["state"] == "PRELAUNCH_CANCEL_REQUESTED"
                    else "RUNNING"
                )
            else:
                # A persisted launch fence with neither a durable terminal
                # outcome nor a provably quiescent cancellation has uncertain
                # execution history.  Keep the non-expiring claim.
                state = "LOST_AMBIGUOUS"
            with base._connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                    (state, base._now(), attempt_id),
                )
            refreshed = self._row(dispatch_id, attempt_id)
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": state,
                "local_task_id": refreshed["local_task_id"],
                "policy_sha256": refreshed["policy_sha256"],
                "write_epoch": refreshed["write_epoch"],
                "write_claim_state": self.claims.state(refreshed),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state", unit.get("state")
                ),
            }
        if row["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "local_task_id": row["local_task_id"],
                "policy_sha256": row["policy_sha256"],
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": "terminal",
            }
        run = base._latest_run(self.observed_fn(row["local_task_id"]))
        terminal = _terminal_state(run)
        unit = self.unit_manager.inspect(str(row["execution_unit_id"] or ""))
        if terminal and unit.get("quiescent"):
            state = terminal
            self.claims.release(row, proof="execution_unit_terminal_and_quiescent")
        elif terminal and unit.get("active"):
            state = "RUNNING"
        elif terminal:
            state = "LOST_AMBIGUOUS"
        elif unit.get("active"):
            state = "RUNNING"
        elif unit.get("quiescent"):
            state = "LOST_AMBIGUOUS"
            if reconcile:
                self.claims.release(
                    row,
                    proof="execution_unit_quiescent_without_terminal_observation",
                )
        elif reconcile:
            state = "LOST_AMBIGUOUS"
        else:
            state = str(row["state"])
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                (state, base._now(), attempt_id),
            )
        refreshed = self._row(dispatch_id, attempt_id)
        return {
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "state": state,
            "local_task_id": refreshed["local_task_id"],
            "policy_sha256": refreshed["policy_sha256"],
            "write_epoch": refreshed["write_epoch"],
            "write_claim_state": self.claims.state(refreshed),
            "execution_unit_state": str(unit.get("state") or "unknown"),
        }

    def _cancel(
        self,
        dispatch_id: str,
        attempt_id: str,
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        row = self._row(dispatch_id, attempt_id)
        if row["authorization_class"] not in write_guard.WRITE_AUTH:
            return super()._cancel(dispatch_id, attempt_id, principal, policy)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "cancel identity does not match accepted attempt",
            )
        if row["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "idempotent": True,
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
            }
        if row["state"] in {"ACCEPTED", "LAUNCHING"}:
            with self._lock, base._connect(self.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                        "UPDATE attempts SET state=CASE"
                        " WHEN state='ACCEPTED' THEN 'PRELAUNCH_CANCEL_REQUESTED'"
                        " ELSE 'CANCEL_REQUESTED' END,updated_at=?"
                        " WHERE attempt_id=? AND state IN ('ACCEPTED','LAUNCHING')",
                        (base._now(), attempt_id),
                    )
            if changed.rowcount:
                refreshed = self._row(dispatch_id, attempt_id)
                invocation_in_flight = self._invocation_in_flight(attempt_id)
                if (
                    refreshed["state"] == "CANCEL_REQUESTED"
                    and not invocation_in_flight
                ):
                    row = refreshed
                else:
                    return {
                        "dispatch_id": dispatch_id,
                        "attempt_id": attempt_id,
                        "state": "CANCEL_REQUESTED",
                        "changed": True,
                        "write_epoch": refreshed["write_epoch"],
                        "write_claim_state": self.claims.state(refreshed),
                        "execution_unit_state": _bounded_peer_observation(
                            "execution_unit_state",
                            self.unit_manager.inspect(
                                str(refreshed["execution_unit_id"] or "")
                            ).get("state"),
                        ),
                    }
            row = self._row(dispatch_id, attempt_id)
            if row["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
                return {
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "state": row["state"],
                    "idempotent": True,
                    "write_epoch": row["write_epoch"],
                    "write_claim_state": self.claims.state(row),
                }
        if row["state"] == "PRELAUNCH_CANCEL_REQUESTED":
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": "CANCEL_REQUESTED",
                "changed": False,
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state",
                    self.unit_manager.inspect(str(row["execution_unit_id"] or "")).get("state"),
                ),
            }
        if row["state"] == "CANCEL_REQUESTED" and self._invocation_in_flight(attempt_id):
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": "CANCEL_REQUESTED",
                "changed": False,
                "write_epoch": row["write_epoch"],
                "write_claim_state": self.claims.state(row),
                "execution_unit_state": _bounded_peer_observation(
                    "execution_unit_state",
                    self.unit_manager.inspect(str(row["execution_unit_id"] or "")).get("state"),
                ),
            }
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state='CANCEL_REQUESTED',updated_at=? WHERE attempt_id=?",
                (base._now(), attempt_id),
            )
        unit = self.unit_manager.stop(str(row["execution_unit_id"] or ""))
        if unit.get("quiescent"):
            result = self.cancel_fn(row["remote_backend"], row["local_task_id"])
            terminal = _terminal_state(base._latest_run(self.observed_fn(row["local_task_id"])))
            state = terminal or "CANCELLED"
            self.claims.release(row, proof="cancel_execution_unit_quiescent")
            changed = bool(result.get("changed", True))
        else:
            state = "CANCEL_REQUESTED"
            changed = False
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                (state, base._now(), attempt_id),
            )
        refreshed = self._row(dispatch_id, attempt_id)
        return {
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "state": state,
            "changed": changed,
            "write_epoch": refreshed["write_epoch"],
            "write_claim_state": self.claims.state(refreshed),
            "execution_unit_state": _bounded_peer_observation(
                "execution_unit_state", unit.get("state")
            ),
        }

    def _artifact_manifest(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        data = base._closed(
            request["data"],
            required={"artifacts", "max_artifact_bytes", "max_total_bytes"},
            name="artifact manifest data",
        )
        if (
            isinstance(data["max_artifact_bytes"], bool)
            or not isinstance(data["max_artifact_bytes"], int)
            or isinstance(data["max_total_bytes"], bool)
            or not isinstance(data["max_total_bytes"], int)
            or data["max_artifact_bytes"] <= 0
            or data["max_total_bytes"] <= 0
        ):
            raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact size limits are invalid")
        dispatch_id = base._bounded_string(
            request.get("dispatch_id"), field="dispatch_id", pattern=base._ID_RE
        )
        attempt_id = base._bounded_string(
            request.get("attempt_id"), field="attempt_id", pattern=base._ID_RE
        )
        row = self._row(dispatch_id, attempt_id)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "artifact identity does not match accepted attempt",
            )
        status = self._status(dispatch_id, attempt_id, reconcile=False)
        if status["state"] != "SUCCEEDED":
            raise FabricError(
                "FABRIC_ARTIFACT_NOT_READY",
                "artifacts may be finalized only after successful terminal execution",
            )
        if self.claims.state(row) == "ACTIVE":
            raise FabricError("FABRIC_ARTIFACT_NOT_READY", "write claim is still active")
        raw_specs = data["artifacts"]
        if not isinstance(raw_specs, list) or len(raw_specs) > artifacts.MAX_ARTIFACTS:
            raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact request is not bounded")
        specs: list[dict[str, Any]] = []
        for raw in raw_specs:
            spec = base._closed(
                raw,
                required={"path", "must_exist", "min_bytes"},
                name="artifact spec",
            )
            name = artifacts.logical_name(spec["path"])
            min_bytes = spec["min_bytes"]
            if isinstance(min_bytes, bool) or not isinstance(min_bytes, int) or min_bytes < 0:
                raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact min_bytes is invalid")
            specs.append(
                {
                    "path": name,
                    "must_exist": bool(spec["must_exist"]),
                    "min_bytes": min_bytes,
                }
            )
        mapping = policy.workspace_mappings.get(row["logical_workspace"])
        if mapping is None:
            raise FabricError("FABRIC_WORKSPACE_DENIED", "attempt workspace is no longer mapped")
        manifest = self.artifact_store.manifest(row, mapping, specs)
        if (
            manifest["total_bytes"] > min(data["max_total_bytes"], artifacts.MAX_TOTAL_ARTIFACT_BYTES)
            or any(
                item["size_bytes"] > min(data["max_artifact_bytes"], artifacts.MAX_ARTIFACT_BYTES)
                for item in manifest["artifacts"]
            )
        ):
            raise FabricError(
                "FABRIC_ARTIFACT_TOO_LARGE",
                "artifact exceeds coordinator-requested bound",
            )
        return {"manifest": manifest}

    def _artifact_chunk(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        data = base._closed(
            request["data"],
            required={"artifact_id", "offset", "max_bytes"},
            name="artifact chunk data",
        )
        dispatch_id = base._bounded_string(
            request.get("dispatch_id"), field="dispatch_id", pattern=base._ID_RE
        )
        attempt_id = base._bounded_string(
            request.get("attempt_id"), field="attempt_id", pattern=base._ID_RE
        )
        row = self._row(dispatch_id, attempt_id)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "artifact chunk identity mismatch",
            )
        artifact_id = base._bounded_string(
            data["artifact_id"], field="artifact_id", pattern=base._ID_RE
        )
        offset = data["offset"]
        maximum = data["max_bytes"]
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
        ):
            raise FabricError(
                "FABRIC_ARTIFACT_CHUNK_INVALID",
                "artifact chunk bounds are invalid",
            )
        return {
            "chunk": self.artifact_store.chunk(
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                artifact_id=artifact_id,
                offset=offset,
                maximum=maximum,
            )
        }


class FabricCoordinator(base.FabricCoordinator):
    """Coordinator with G4-C feature preflight, artifacts, and retry safety."""

    def _ensure_g4c(self) -> artifacts.CoordinatorArtifactStore:
        write_guard.migrate_coordinator(self.db_path)
        return artifacts.CoordinatorArtifactStore(
            self.db_path,
            base._root(self.hermes_root) / "fabric" / "admitted",
        )

    def dispatch(
        self,
        contract: dict[str, Any],
        *,
        dry_run: bool,
        confirm: bool,
        timeout: int,
    ) -> dict[str, Any]:
        store = self._ensure_g4c()
        specs = artifacts.contract_specs(contract)
        node_name, _backend, _workspace, _options, _evidence = base._fabric_options(contract)
        node = self._node(node_name)
        if not dry_run and (specs or write_guard.is_write(contract)):
            capabilities = self._capabilities(node, timeout)
            features = set(capabilities.get("features") or [])
            if specs and not artifacts.ARTIFACT_FEATURES <= features:
                raise FabricError(
                    "FABRIC_ARTIFACT_ADMISSION_UNAVAILABLE",
                    "managed peer lacks immutable bounded artifact transfer",
                )
            if write_guard.is_write(contract) and not write_guard.WRITE_FEATURES <= features:
                raise FabricError(
                    "FABRIC_EXECUTION_UNIT_UNAVAILABLE",
                    "managed peer lacks verified write ownership/containment",
                )
        result = super().dispatch(
            contract,
            dry_run=dry_run,
            confirm=confirm,
            timeout=timeout,
        )
        if not dry_run and specs and result.get("attempt_id") and result.get("dispatch_id"):
            with base._connect_readonly(self.db_path) as db:
                dispatch = db.execute(
                    "SELECT contract_sha256 FROM dispatches WHERE dispatch_id=?",
                    (result["dispatch_id"],),
                ).fetchone()
            if dispatch is not None:
                store.remember(
                    attempt_id=result["attempt_id"],
                    dispatch_id=result["dispatch_id"],
                    contract_sha256=dispatch["contract_sha256"],
                    specs=specs,
                )
        return result

    def poll(
        self,
        attempt_id: str,
        *,
        reconcile: bool = False,
        timeout: int = 15,
    ) -> dict[str, Any]:
        self._ensure_g4c()
        try:
            result = super().poll(attempt_id, reconcile=reconcile, timeout=timeout)
        except FabricError as exc:
            attempt, dispatch, node = self._attempt(attempt_id)
            state = (
                "RECONCILING"
                if exc.code in {"FABRIC_TRANSPORT_TIMEOUT", "FABRIC_PEER_UNAVAILABLE"}
                else "BLOCKED"
            )
            with base._connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,error_code=?,updated_at=? WHERE attempt_id=?",
                    (state, exc.code, base._now(), attempt_id),
                )
            return {
                "success": False,
                "backend": "fabric",
                "node": node.name,
                "dispatch_id": attempt["dispatch_id"],
                "attempt_id": attempt_id,
                "task_id": dispatch["task_id"],
                "state": state,
                "code": exc.code,
            }
        return result

    def cancel(self, attempt_id: str, *, timeout: int = 15) -> dict[str, Any]:
        self._ensure_g4c()
        try:
            return super().cancel(attempt_id, timeout=timeout)
        except FabricError as exc:
            if exc.code not in {"FABRIC_TRANSPORT_TIMEOUT", "FABRIC_PEER_UNAVAILABLE"}:
                raise
            attempt, dispatch, node = self._attempt(attempt_id)
            with base._connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state='CANCEL_AMBIGUOUS',error_code=?,updated_at=?"
                    " WHERE attempt_id=?",
                    (exc.code, base._now(), attempt_id),
                )
            return {
                "success": False,
                "changed": True,
                "backend": "fabric",
                "node": node.name,
                "attempt_id": attempt_id,
                "dispatch_id": attempt["dispatch_id"],
                "task_id": dispatch["task_id"],
                "state": "CANCEL_AMBIGUOUS",
                "code": exc.code,
                "suggested_action": "Reconcile this exact attempt before any retry.",
            }

    def _validate_manifest(
        self,
        manifest: Any,
        *,
        attempt: sqlite3.Row,
        dispatch: sqlite3.Row,
        node: FabricNode,
        specs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return artifacts.CoordinatorArtifactStore.validate_manifest(
            manifest,
            attempt=attempt,
            dispatch=dispatch,
            node=node,
            specs=specs,
        )

    def _pull_artifact(
        self,
        attempt: sqlite3.Row,
        node: FabricNode,
        item: dict[str, Any],
        *,
        timeout: int,
    ) -> dict[str, Any]:
        store = self._ensure_g4c()
        return store.pull(
            attempt=attempt,
            node=node,
            item=item,
            rpc=self.rpc,
            timeout=timeout,
        )

    def collect_artifacts(
        self,
        attempt_id: str,
        *,
        timeout: int = 15,
    ) -> list[dict[str, Any]]:
        store = self._ensure_g4c()
        specs = store.specs(attempt_id)
        if specs is None:
            return []
        attempt, dispatch, node = self._attempt(attempt_id)
        _, response = self.rpc(
            node,
            base._request(
                "artifact_manifest",
                node.coordinator_principal,
                data={
                    "artifacts": specs,
                    "max_artifact_bytes": artifacts.MAX_ARTIFACT_BYTES,
                    "max_total_bytes": artifacts.MAX_TOTAL_ARTIFACT_BYTES,
                },
                dispatch_id=attempt["dispatch_id"],
                attempt_id=attempt_id,
            ),
            timeout,
        )
        response = base._validate_response(response, operation="artifact_manifest")
        wrapper = base._closed(
            response["data"],
            required={"manifest"},
            name="artifact manifest response",
        )
        items = self._validate_manifest(
            wrapper["manifest"],
            attempt=attempt,
            dispatch=dispatch,
            node=node,
            specs=specs,
        )
        spec_map = {spec["path"]: spec for spec in specs}
        admitted: list[dict[str, Any]] = []
        for item in items:
            receipt = self._pull_artifact(attempt, node, item, timeout=timeout)
            if receipt["size_bytes"] < int(spec_map[item["logical_name"]]["min_bytes"]):
                raise FabricError(
                    "FABRIC_ARTIFACT_TOO_SMALL",
                    "artifact is below the contract minimum size",
                )
            admitted.append(receipt)
        return admitted

    def collect(self, attempt_id: str, *, timeout: int = 15) -> dict[str, Any]:
        store = self._ensure_g4c()
        has_artifacts = store.specs(attempt_id) is not None
        admitted: list[dict[str, Any]] = []
        if has_artifacts:
            try:
                admitted = self.collect_artifacts(attempt_id, timeout=timeout)
            except FabricError:
                with base._connect(self.db_path) as db:
                    db.execute(
                        "UPDATE attempts SET state='EVIDENCE_PENDING',updated_at=? WHERE attempt_id=?",
                        (base._now(), attempt_id),
                    )
                raise
        result = super().collect(attempt_id, timeout=timeout)
        if has_artifacts:
            result["artifacts"] = admitted
        return result

    def retry(
        self,
        contract: dict[str, Any],
        prior_attempt_id: str,
        *,
        confirm: bool,
        timeout: int = 15,
    ) -> dict[str, Any]:
        if not confirm:
            raise FabricError("CONFIRMATION_REQUIRED", "Fabric retry requires confirm=true")
        self._ensure_g4c()
        prior, dispatch, node = self._attempt(prior_attempt_id)
        if base._contract_sha(contract) != dispatch["contract_sha256"]:
            raise FabricError(
                "FABRIC_RETRY_LINEAGE_MISMATCH",
                "retry contract differs from original dispatch",
            )
        status = self.poll(prior_attempt_id, reconcile=True, timeout=timeout)
        if status.get("write_claim_state") == "ACTIVE":
            raise FabricError(
                "FABRIC_WRITE_OWNERSHIP_BLOCKED",
                "prior write ownership remains active",
            )
        if status.get("state") not in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "BLOCKED",
            "TERMINAL_REPORTED",
        }:
            raise FabricError(
                "FABRIC_RETRY_BLOCKED",
                "prior attempt is not safely reconciled",
            )
        node_name, backend, workspace, options, evidence = base._fabric_options(contract)
        if node_name != node.name:
            raise FabricError(
                "FABRIC_RETRY_LINEAGE_MISMATCH",
                "retry targets a different managed node",
            )
        capabilities = self._capabilities(node, timeout)
        with base._connect_readonly(self.db_path) as db:
            count = int(
                db.execute(
                    "SELECT COUNT(*) AS n FROM attempts WHERE dispatch_id=?",
                    (prior["dispatch_id"],),
                ).fetchone()["n"]
            )
        envelope = base._build_envelope(
            contract,
            node,
            remote_backend=backend,
            logical_workspace=workspace,
            remote_options=options,
            evidence_policy=evidence,
            capability_sha=capabilities["snapshot_sha256"],
        )
        attempt_id = base._attempt_id(prior["dispatch_id"], count + 1)
        envelope["attempt_id"] = attempt_id
        envelope["retry_parent_attempt_id"] = prior_attempt_id
        now = base._now()
        with base._connect(self.db_path) as db:
            db.execute(
                "INSERT INTO attempts"
                "(attempt_id,dispatch_id,envelope_sha256,node_name,peer_name,remote_backend,coordinator_principal,"
                "capability_sha256,state,created_at,updated_at,retry_parent_attempt_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    prior["dispatch_id"],
                    sha256_json(envelope),
                    node.name,
                    node.a2a_peer_name,
                    backend,
                    node.coordinator_principal,
                    capabilities["snapshot_sha256"],
                    "SUBMITTING",
                    now,
                    now,
                    prior_attempt_id,
                ),
            )
        try:
            remote_task_id, response = self.rpc(
                node,
                base._request(
                    "accept",
                    node.coordinator_principal,
                    data={"envelope": envelope},
                    dispatch_id=prior["dispatch_id"],
                    attempt_id=attempt_id,
                ),
                timeout,
            )
        except FabricError as exc:
            state = "SUBMISSION_AMBIGUOUS" if exc.ambiguous else "BLOCKED"
            with base._connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,error_code=?,updated_at=? WHERE attempt_id=?",
                    (state, exc.code, base._now(), attempt_id),
                )
            return {
                "success": False,
                "changed": bool(exc.ambiguous),
                "backend": "fabric",
                "dispatch_id": prior["dispatch_id"],
                "attempt_id": attempt_id,
                "retry_parent_attempt_id": prior_attempt_id,
                "state": state,
                "code": exc.code,
            }
        response = base._validate_response(response, operation="accept")
        data = response["data"]
        if data.get("dispatch_id") != prior["dispatch_id"] or data.get("attempt_id") != attempt_id:
            raise FabricError("FABRIC_PROTOCOL_ERROR", "peer retry accept lineage mismatch")
        state = "SUBMITTED" if response["ok"] else "BLOCKED"
        epoch_value = data.get("write_epoch")
        epoch = (
            epoch_value
            if isinstance(epoch_value, int) and not isinstance(epoch_value, bool)
            else None
        )
        claim_state = _bounded_peer_observation(
            "write_claim_state", data.get("write_claim_state")
        )
        unit_state = _bounded_peer_observation(
            "execution_unit_state", data.get("execution_unit_state")
        )
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,remote_task_id=?,peer_policy_sha256=?,write_epoch=?,"
                "write_claim_state=?,execution_unit_state=?,error_code=?,updated_at=?"
                " WHERE attempt_id=?",
                (
                    state,
                    remote_task_id,
                    data.get("policy_sha256"),
                    epoch,
                    claim_state,
                    unit_state,
                    None if response["ok"] else response["code"],
                    base._now(),
                    attempt_id,
                ),
            )
        specs = artifacts.contract_specs(contract)
        if specs:
            self._ensure_g4c().remember(
                attempt_id=attempt_id,
                dispatch_id=prior["dispatch_id"],
                contract_sha256=dispatch["contract_sha256"],
                specs=specs,
            )
        return {
            "success": bool(response["ok"]),
            "changed": bool(response["ok"]),
            "backend": "fabric",
            "node": node.name,
            "dispatch_id": prior["dispatch_id"],
            "attempt_id": attempt_id,
            "retry_parent_attempt_id": prior_attempt_id,
            "state": state,
            "write_epoch": epoch,
            "write_claim_state": claim_state,
            "execution_unit_state": unit_state,
            "code": response["code"],
        }

    def reconcile_active(self, *, timeout: int = 10) -> list[dict[str, Any]]:
        self._ensure_g4c()
        with base._connect_readonly(self.db_path) as db:
            rows = db.execute(
                "SELECT attempt_id FROM attempts "
                "WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED') "
                "ORDER BY created_at LIMIT 128"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                out.append(self.poll(row["attempt_id"], reconcile=True, timeout=timeout))
            except FabricError as exc:
                out.append(
                    {
                        "attempt_id": row["attempt_id"],
                        "success": False,
                        "state": "BLOCKED",
                        "code": exc.code,
                    }
                )
        return out

    def observed_artifacts(
        self,
        task_id: str,
        *,
        contract_sha256: str,
    ) -> list[dict[str, Any]]:
        """Return re-verified coordinator-admitted artifacts for one contract lineage.

        Auto placement changes the canonical contract before Fabric dispatch. The
        routing journal is coordinator-local evidence linking the original Work
        Contract hash to that placed hash. Only completed attempts with admitted
        run evidence are eligible, and the admission bytes are re-hashed before
        they are returned as completion evidence.
        """
        if not base._ID_RE.fullmatch(task_id or "") or not base._SHA_RE.fullmatch(contract_sha256 or ""):
            return []
        # Observation must remain strictly read-only. Ordinary Work Contract
        # validation may construct the Fabric backend even when Fabric has never
        # run; do not migrate/create a coordinator journal merely to look for
        # artifact evidence.
        if not self.db_path.is_file():
            return []
        try:
            with base._connect_readonly(self.db_path) as db:
                tables = {
                    str(row["name"])
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
        except sqlite3.Error:
            return []
        if not {"attempts", "dispatches", "artifact_admissions"} <= tables:
            return []

        allowed_shas = {contract_sha256}
        journal = router._journal_path(self.hermes_root)
        try:
            # Stream the whole journal: an older but still-valid routing
            # decision must not silently vanish merely because the journal grew
            # past a fixed tail window. Parsing stays bounded per record (each
            # line is length-capped and independently JSON-validated), and the
            # file is opened read-only for the duration of the scan.
            with journal.open("rb") as fh:
                for raw_line in fh:
                    if not raw_line or len(raw_line) > 128_000:
                        continue
                    try:
                        record = base.strict_json_loads(raw_line, maximum=128_000)
                    except FabricError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    selected = record.get("selected")
                    if (
                        record.get("schema") != router.ROUTING_DECISION_SCHEMA
                        or record.get("task_id") != task_id
                        or record.get("original_contract_sha256") != contract_sha256
                        or not isinstance(selected, dict)
                        or selected.get("remote") is not True
                        or selected.get("transport_backend") != "fabric"
                    ):
                        continue
                    placed_sha = record.get("placed_contract_sha256")
                    if isinstance(placed_sha, str) and base._SHA_RE.fullmatch(placed_sha):
                        allowed_shas.add(placed_sha)
        except OSError:
            pass

        placeholders = ",".join("?" for _ in allowed_shas)
        with base._connect_readonly(self.db_path) as db:
            rows = db.execute(
                "SELECT aa.*,d.contract_sha256,a.state,a.evidence_json,a.created_at "
                "FROM artifact_admissions aa "
                "JOIN dispatches d ON d.dispatch_id=aa.dispatch_id "
                "JOIN attempts a ON a.attempt_id=aa.attempt_id "
                f"WHERE d.task_id=? AND d.contract_sha256 IN ({placeholders}) "
                "AND a.state='COMPLETED' AND a.evidence_json IS NOT NULL "
                "ORDER BY a.created_at,aa.logical_name",
                (task_id, *sorted(allowed_shas)),
            ).fetchall()

        admission_root = (base._root(self.hermes_root) / "fabric" / "admitted").resolve()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                candidate = Path(str(row["admission_path"]))
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(admission_root) or not resolved.is_file():
                    continue
                expected_size = int(row["size_bytes"])
                before_stat = resolved.stat()
                if before_stat.st_size != expected_size:
                    continue
                first_digest = hashlib.sha256()
                with resolved.open("rb") as fh:
                    while chunk := fh.read(1024 * 1024):
                        first_digest.update(chunk)
                # Re-read independently before accepting the admission. A
                # same-size rewrite can occur within one filesystem timestamp
                # tick, so mtime/ctime identity checks alone are not a
                # deterministic stability proof.
                second_digest = hashlib.sha256()
                with resolved.open("rb") as fh:
                    while chunk := fh.read(1024 * 1024):
                        second_digest.update(chunk)
                after_stat = resolved.stat()
                if (
                    after_stat.st_size != expected_size
                    or after_stat.st_dev != before_stat.st_dev
                    or after_stat.st_ino != before_stat.st_ino
                    or after_stat.st_mtime_ns != before_stat.st_mtime_ns
                    or after_stat.st_ctime_ns != before_stat.st_ctime_ns
                ):
                    continue
                expected_digest = row["sha256"]
                if first_digest.hexdigest() != expected_digest or second_digest.hexdigest() != expected_digest:
                    continue
            except (OSError, TypeError, ValueError):
                continue
            out.append(
                {
                    "logical_name": row["logical_name"],
                    "size_bytes": expected_size,
                    "sha256": row["sha256"],
                    "media_type": row["media_type"],
                    "active_content": bool(row["active_content"]),
                    "attempt_id": row["attempt_id"],
                    "dispatch_id": row["dispatch_id"],
                    "provenance": "coordinator_verified_artifact",
                }
            )
        return out


class AutoRouter(router.AutoRouter):
    """Unlock only remote G4-B bridge exclusions proven by live G4-C features."""

    def __init__(
        self,
        *,
        remote_probe: Callable[[FabricNode, int], dict[str, Any]] | None = None,
        hermes_root: Path | None = None,
        **kwargs: Any,
    ) -> None:
        self._features: dict[str, set[str]] = {}
        if remote_probe is None:
            coordinator = FabricCoordinator(hermes_root=hermes_root)

            def selected_probe(node: FabricNode, timeout: int) -> dict[str, Any]:
                started = time.perf_counter()
                snapshot = coordinator._capabilities(node, timeout)
                features = set(snapshot.get("features") or [])
                self._features[node.name] = features
                return {
                    "healthy": True,
                    "latency_ms": (time.perf_counter() - started) * 1000.0,
                    "snapshot_sha256": snapshot.get("snapshot_sha256", ""),
                    "features": sorted(features),
                }

        else:

            def selected_probe(node: FabricNode, timeout: int) -> dict[str, Any]:
                result = remote_probe(node, timeout)
                features = result.get("features") if isinstance(result, dict) else None
                self._features[node.name] = (
                    set(features) if isinstance(features, (list, tuple, set)) else set()
                )
                return result

        super().__init__(remote_probe=selected_probe, hermes_root=hermes_root, **kwargs)

    def _audit_decision(self, decision: dict[str, Any], *, dry_run: bool) -> None:
        # The base G4-B decision is preliminary for write/artifact-capable remote
        # candidates. Defer its audit until ``route`` applies the live G4-C
        # feature gates so the durable audit trail describes the actual winner.
        return None

    def route(
        self,
        contract: dict[str, Any],
        *,
        timeout: int = 15,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        decision = super().route(contract, timeout=timeout, dry_run=dry_run)
        try:
            needs_write = write_guard.is_write(contract)
            needs_artifacts = bool(contract.get("expected_artifacts"))
            if not (needs_write or needs_artifacts):
                router._audit_route(
                    decision,
                    success=decision.get("selected") is not None,
                    dry_run=dry_run,
                )
                return decision
            for candidate in decision.get("candidates", []):
                if not candidate.get("remote"):
                    continue
                features = self._features.get(str(candidate.get("node") or ""), set())
                filtered: list[dict[str, str]] = []
                for exclusion in candidate.get("exclusions") or []:
                    code = exclusion.get("code")
                    unlock_write = (
                        code == "WRITE_CONFLICT_GUARD_UNAVAILABLE"
                        and needs_write
                        and write_guard.WRITE_FEATURES <= features
                    )
                    unlock_artifact = (
                        code == "REMOTE_ARTIFACT_ADMISSION_UNAVAILABLE"
                        and needs_artifacts
                        and artifacts.ARTIFACT_FEATURES <= features
                    )
                    if not (unlock_write or unlock_artifact):
                        filtered.append(exclusion)
                candidate["exclusions"] = filtered
                candidate["eligible"] = not filtered
                candidate["g4c_features"] = sorted(features)
            eligible = sorted(
                (item for item in decision.get("candidates", []) if item.get("eligible")),
                key=lambda item: tuple(item.get("rank") or []),
            )
            decision["selected"] = None
            if eligible:
                winner = eligible[0]
                decision["selected"] = {
                    "node": winner["node"],
                    "backend": winner["backend"],
                    "transport_backend": winner["transport_backend"],
                    "remote": winner["remote"],
                    "healthy": winner["healthy"],
                    "capability_fresh": winner["capability_fresh"],
                    "authority_ceiling": winner["authority_ceiling"],
                    "eligible": winner["eligible"],
                    "exclusions": list(winner["exclusions"]),
                    "rank": winner["rank"],
                }
        except Exception:
            # G4-C feature-gate processing raised after the preliminary base
            # route. The base audit was deferred, so emit exactly one failure
            # record now. The preliminary winner is never claimed as selected:
            # the gate evaluation that proves eligibility did not complete.
            failed = dict(decision)
            failed["selected"] = None
            router._audit_route(failed, success=False, dry_run=dry_run)
            raise
        decision["g4c_guards"] = {
            "write_required": needs_write,
            "artifact_required": needs_artifacts,
        }
        router._audit_route(
            decision,
            success=decision.get("selected") is not None,
            dry_run=dry_run,
        )
        return decision


def register_runtime() -> None:
    """Replace only runtime backend registrations; leave G4-A classes intact."""
    runners.register_backend(
        base.FabricBackend(coordinator_factory=FabricCoordinator),
        replace=True,
    )
    runners.register_backend(
        router.AutoBackend(router_factory=AutoRouter),
        replace=True,
    )


def _peer_handler_class() -> type[base._PeerHandler]:
    class G4CPeerHandler(base._PeerHandler):
        def do_POST(self) -> None:
            outer: Any = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > base._MAX_BODY:
                    raise FabricError(
                        "FABRIC_PAYLOAD_TOO_LARGE",
                        "A2A request has an invalid body size",
                    )
                outer = base._closed(
                    strict_json_loads(self.rfile.read(length)),
                    required={"jsonrpc", "id", "method", "params"},
                    name="A2A JSON-RPC request",
                )
                if outer["jsonrpc"] != "2.0" or outer["method"] not in {
                    "SendMessage",
                    "message/send",
                }:
                    raise FabricError(
                        "FABRIC_PROTOCOL_ERROR",
                        "only A2A SendMessage is accepted by Fabric peer",
                    )
                params = base._closed(
                    outer["params"],
                    required={"message"},
                    name="A2A params",
                )
                message = base._closed(
                    params["message"],
                    required={"role", "parts", "messageId", "contextId"},
                    name="A2A message",
                )
                if (
                    message["role"] != "ROLE_USER"
                    or not isinstance(message["parts"], list)
                    or len(message["parts"]) != 1
                ):
                    raise FabricError(
                        "FABRIC_PROTOCOL_ERROR",
                        "Fabric A2A message must contain one structured DataPart",
                    )
                raw_part = message["parts"][0]
                if (
                    not isinstance(raw_part, dict)
                    or "text" in raw_part
                    or "data" not in raw_part
                    or raw_part.get("mediaType") != "application/json"
                ):
                    raise FabricError(
                        "FABRIC_PROTOCOL_ERROR",
                        "Fabric accepts only structured JSON DataParts",
                    )
                part = base._closed(
                    raw_part,
                    required={"data", "mediaType"},
                    name="A2A DataPart",
                )
                request = _validate_request(part["data"])
                response = self.service.handle(
                    request,
                    self.headers.get("Authorization", ""),
                )
                context_id = request.get("dispatch_id") or message["contextId"]
                task_id = "ftask-" + hashlib.sha256(
                    f"{request['request_id']}:{request.get('attempt_id', '')}".encode()
                ).hexdigest()[:24]
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": outer["id"],
                        "result": {
                            "id": task_id,
                            "contextId": context_id,
                            "status": {
                                "state": "TASK_STATE_COMPLETED",
                                "message": {
                                    "role": "ROLE_AGENT",
                                    "parts": [
                                        {
                                            "data": response,
                                            "mediaType": "application/json",
                                        }
                                    ],
                                    "messageId": "resp-" + task_id[6:],
                                    "contextId": context_id,
                                },
                            },
                        },
                    },
                )
            except FabricError as exc:
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": outer.get("id") if isinstance(outer, dict) else None,
                        "error": {
                            "code": -32001,
                            "message": str(exc)[:300],
                            "data": {"code": exc.code},
                        },
                    },
                )
            except (OSError, ValueError, TypeError, sqlite3.Error):
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": outer.get("id") if isinstance(outer, dict) else None,
                        "error": {
                            "code": -32603,
                            "message": "internal Fabric peer error",
                            "data": {"code": "FABRIC_INTERNAL_ERROR"},
                        },
                    },
                )

    return G4CPeerHandler


def peer_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="hermes-gpt-fabric-peer",
        description="Run the deterministic Hermes GPT Fabric G4-C A2A peer endpoint.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4780)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--advertised-url", default="")
    args = parser.parse_args(argv)
    if bool(args.cert) != bool(args.key):
        raise SystemExit("Fabric TLS requires both --cert and --key.")
    loopback = args.host in {"127.0.0.1", "localhost", "::1"}
    if not loopback and not (args.cert and args.key):
        raise SystemExit("Non-loopback verified Fabric requires direct TLS (--cert and --key).")
    scheme = "https" if args.cert else "http"
    advertised = args.advertised_url or f"{scheme}://{args.host}:{args.port}"
    base._require_secure_transport(advertised)
    service = FabricPeerService()
    server = base.ThreadingHTTPServer((args.host, args.port), _peer_handler_class())
    server.fabric_service = service
    server.fabric_advertised_url = advertised
    if args.cert and args.key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = [
    "ARTIFACT_CHUNK_SCHEMA",
    "ARTIFACT_MANIFEST_SCHEMA",
    "FEATURE_ARTIFACT",
    "FEATURE_ARTIFACT_SNAPSHOT",
    "FEATURE_EXECUTION_UNIT",
    "FEATURE_RECONCILE",
    "FEATURE_WRITE_EPOCH",
    "FEATURE_WRITE_OWNERSHIP",
    "AutoRouter",
    "FabricCoordinator",
    "FabricError",
    "FabricNode",
    "FabricPeerPolicy",
    "FabricPeerService",
    "SystemdUserUnitManager",
    "WorkspaceMapping",
    "canonical_json",
    "load_node_registry",
    "load_peer_policy",
    "load_peer_tokens",
    "peer_main",
    "register_runtime",
    "sha256_json",
    "strict_json_loads",
]


if __name__ == "__main__":
    peer_main()
