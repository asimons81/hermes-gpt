"""Hermes GPT v0.8 Fabric G4-C safety and artifact runtime.

This module extends the G4-A managed Fabric transport and G4-B router without
creating a second authority plane. It adds the pieces deliberately left locked
at the end of G4-B:

* durable, monotonic write ownership over server-defined conflict domains;
* whole-execution-unit containment and terminal proof for verified writes;
* fail-closed cancel/restart/retry reconciliation;
* immutable attempt-bound remote artifact snapshots and bounded chunk transfer;
* coordinator-side hash verification and atomic artifact admission;
* capability-aware unlocking of remote write/artifact auto placement only when
  the peer proves that these G4-C mechanisms are available.

The implementation intentionally supports verified write containment only when
a Linux user systemd manager is usable. The existing runner process groups are
not sufficient because nested runner children may create new sessions. A backend
without a trackable whole-tree execution unit remains ineligible for verified
write-capable Fabric placement.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import operator_fabric as base
import operator_fabric_router as router
import operator_policy as op
import operator_runners as runners

ARTIFACT_MANIFEST_SCHEMA = "hermes.fabric-artifact-manifest/v1"
ARTIFACT_CHUNK_SCHEMA = "hermes.fabric-artifact-chunk/v1"
FEATURE_ARTIFACT = "artifact-transfer-v1"
FEATURE_ARTIFACT_SNAPSHOT = "artifact-snapshot-v1"
FEATURE_WRITE_OWNERSHIP = "write-ownership-v1"
FEATURE_EXECUTION_UNIT = "execution-unit-v1"
FEATURE_WRITE_EPOCH = "write-epoch-v1"
FEATURE_RECONCILE = "reconcile-v1"

_WRITE_AUTH = frozenset({"reversible_write", "high_impact"})
_MAX_ARTIFACTS = 32
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_ARTIFACT_BYTES = 16 * 1024 * 1024
_MAX_CHUNK_BYTES = 48 * 1024
_MAX_LOGICAL_NAME = 512
_WINDOWS_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_SECRET_ARTIFACT_RE = re.compile(
    r"(?:^|/)(?:\.env(?:\..*)?|\.ssh(?:/|$)|id_rsa(?:\.|$)|id_ed25519(?:\.|$)|credentials?(?:\.|$)|secrets?(?:\.|$)|tokens?(?:\.|$))",
    re.IGNORECASE,
)
_ACTIVE_MEDIA = frozenset(
    {
        "text/html",
        "image/svg+xml",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
    }
)
_DB_LOCK = threading.RLock()

FabricError = base.FabricError
FabricNode = base.FabricNode
FabricPeerPolicy = base.FabricPeerPolicy
WorkspaceMapping = base.WorkspaceMapping
canonical_json = base.canonical_json
sha256_json = base.sha256_json
strict_json_loads = base.strict_json_loads
load_node_registry = base.load_node_registry
load_peer_policy = base.load_peer_policy
load_peer_tokens = base.load_peer_tokens


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(db: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _init_peer_g4c(path: Path) -> None:
    base._init_peer_db(path)
    with _DB_LOCK, base._connect(path) as db:
        _add_column(db, "attempts", "write_epoch", "INTEGER")
        _add_column(db, "attempts", "execution_unit_kind", "TEXT")
        _add_column(db, "attempts", "execution_unit_id", "TEXT")
        _add_column(db, "attempts", "retry_parent_attempt_id", "TEXT")
        _add_column(db, "write_claims", "epoch", "INTEGER NOT NULL DEFAULT 0")
        _add_column(db, "write_claims", "execution_unit_kind", "TEXT")
        _add_column(db, "write_claims", "execution_unit_id", "TEXT")
        _add_column(db, "write_claims", "release_proof", "TEXT")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
              artifact_id TEXT PRIMARY KEY,
              attempt_id TEXT NOT NULL,
              dispatch_id TEXT NOT NULL,
              contract_sha256 TEXT NOT NULL,
              logical_name TEXT NOT NULL,
              snapshot_path TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              media_type TEXT NOT NULL,
              active_content INTEGER NOT NULL,
              finalized_at TEXT NOT NULL,
              UNIQUE(attempt_id, logical_name)
            );
            CREATE INDEX IF NOT EXISTS peer_artifacts_attempt_idx ON artifacts(attempt_id);
            """
        )


def _init_coordinator_g4c(path: Path) -> None:
    base._init_coordinator_db(path)
    with _DB_LOCK, base._connect(path) as db:
        _add_column(db, "attempts", "write_epoch", "INTEGER")
        _add_column(db, "attempts", "retry_parent_attempt_id", "TEXT")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifact_requests (
              attempt_id TEXT PRIMARY KEY,
              dispatch_id TEXT NOT NULL,
              contract_sha256 TEXT NOT NULL,
              specs_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifact_admissions (
              artifact_id TEXT PRIMARY KEY,
              attempt_id TEXT NOT NULL,
              dispatch_id TEXT NOT NULL,
              logical_name TEXT NOT NULL,
              admission_path TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              media_type TEXT NOT NULL,
              active_content INTEGER NOT NULL,
              admitted_at TEXT NOT NULL,
              UNIQUE(attempt_id, logical_name)
            );
            CREATE INDEX IF NOT EXISTS coordinator_artifacts_attempt_idx
              ON artifact_admissions(attempt_id);
            """
        )


def _is_write_auth(contract_or_envelope: dict[str, Any]) -> bool:
    auth = contract_or_envelope.get("authorization") or {}
    return str(auth.get("class") or "") in _WRITE_AUTH


def _logical_artifact_name(value: Any) -> str:
    name = base._bounded_string(value, field="artifact logical name", maximum=_MAX_LOGICAL_NAME)
    if "\\" in name or name.startswith("/") or _WINDOWS_ABS_RE.match(name):
        raise FabricError(
            "FABRIC_ARTIFACT_LOGICAL_NAME_INVALID",
            "remote artifact names must be relative POSIX-style logical paths",
        )
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise FabricError(
            "FABRIC_ARTIFACT_LOGICAL_NAME_INVALID",
            "remote artifact name is not a safe relative logical path",
        )
    normalized = path.as_posix()
    if _SECRET_ARTIFACT_RE.search(normalized):
        raise FabricError(
            "FABRIC_ARTIFACT_SECRET_PATH",
            "remote artifact logical name is denied by secret-path policy",
        )
    return normalized


def _artifact_specs(contract: dict[str, Any]) -> list[dict[str, Any]]:
    raw = contract.get("expected_artifacts") or []
    if not isinstance(raw, list) or len(raw) > _MAX_ARTIFACTS:
        raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "expected_artifacts is not bounded")
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "expected artifact must be an object")
        name = _logical_artifact_name(item.get("path"))
        if name in seen:
            raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact logical names must be unique")
        min_bytes = item.get("min_bytes", 0)
        if isinstance(min_bytes, bool) or not isinstance(min_bytes, int) or min_bytes < 0:
            raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact min_bytes is invalid")
        specs.append(
            {
                "path": name,
                "must_exist": bool(item.get("must_exist", True)),
                "min_bytes": min_bytes,
            }
        )
        seen.add(name)
    return specs


def _validate_g4c_request(value: Any) -> dict[str, Any]:
    request = base._closed(
        value,
        required={"schema", "version", "operation", "coordinator_principal", "request_id", "data"},
        optional={"dispatch_id", "attempt_id"},
        name="Fabric request",
    )
    if request["schema"] != base.REQUEST_SCHEMA or request["version"] != base.FABRIC_VERSION:
        raise FabricError("FABRIC_PROTOCOL_INCOMPATIBLE", "Fabric request schema/version is unsupported")
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


def _validate_g4c_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FabricError("FABRIC_SCHEMA_INVALID", "Fabric dispatch envelope must be an object")
    raw = dict(value)
    retry_parent = raw.pop("retry_parent_attempt_id", None)
    envelope = base._validate_envelope(raw)
    if retry_parent is not None:
        envelope["retry_parent_attempt_id"] = base._bounded_string(
            retry_parent,
            field="retry_parent_attempt_id",
            pattern=base._ID_RE,
        )
    return envelope


class SystemdUserUnitManager:
    """Track a verified writer as one Linux user-systemd cgroup."""

    def __init__(self) -> None:
        self.systemd_run = shutil.which("systemd-run")
        self.systemctl = shutil.which("systemctl")
        self._cached_at = 0.0
        self._cached_available = False

    def unit_name(self, attempt_id: str) -> str:
        digest = hashlib.sha256(attempt_id.encode()).hexdigest()[:24]
        return f"hermes-fabric-{digest}.service"

    def available(self) -> bool:
        now = time.monotonic()
        if now - self._cached_at < 15:
            return self._cached_available
        self._cached_at = now
        if not sys.platform.startswith("linux") or not self.systemd_run or not self.systemctl:
            self._cached_available = False
            return False
        try:
            probe = subprocess.run(
                [self.systemctl, "--user", "show-environment"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
            self._cached_available = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            self._cached_available = False
        return self._cached_available

    def launch(self, unit: str, task_id: str, workspace: Path, jobs_root: Path) -> dict[str, Any]:
        if not self.available():
            return {"accepted": False, "ambiguous": False, "code": "FABRIC_EXECUTION_UNIT_UNAVAILABLE"}
        argv = [
            str(self.systemd_run),
            "--user",
            f"--unit={unit}",
            "--collect",
            "--quiet",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=5s",
            f"--working-directory={workspace}",
            sys.executable,
            str(Path(runners.__file__).resolve()),
            "--worker",
            task_id,
            "--root",
            str(jobs_root),
        ]
        try:
            completed = subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"accepted": False, "ambiguous": True, "code": "FABRIC_EXECUTION_UNIT_START_AMBIGUOUS"}
        except OSError:
            return {"accepted": False, "ambiguous": False, "code": "FABRIC_EXECUTION_UNIT_START_FAILED"}
        if completed.returncode != 0:
            return {"accepted": False, "ambiguous": False, "code": "FABRIC_EXECUTION_UNIT_START_FAILED"}
        return {"accepted": True, "ambiguous": False, "code": "FABRIC_EXECUTION_UNIT_STARTED"}

    def inspect(self, unit: str) -> dict[str, Any]:
        if not self.available():
            return {"known": False, "active": False, "quiescent": False, "state": "unknown"}
        try:
            completed = subprocess.run(
                [
                    str(self.systemctl),
                    "--user",
                    "show",
                    unit,
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=Result",
                    "--no-pager",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"known": False, "active": False, "quiescent": False, "state": "unknown"}
        props: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                props[key] = value
        load = props.get("LoadState", "")
        active_state = props.get("ActiveState", "")
        if completed.returncode != 0 and load != "not-found":
            return {"known": False, "active": False, "quiescent": False, "state": "unknown"}
        active = active_state in {"active", "activating", "deactivating", "reloading"}
        quiescent = load == "not-found" or active_state in {"inactive", "failed", "dead"}
        return {
            "known": True,
            "active": active,
            "quiescent": quiescent,
            "state": active_state or ("not-found" if load == "not-found" else "unknown"),
            "result": props.get("Result", ""),
        }

    def stop(self, unit: str) -> dict[str, Any]:
        if not self.available():
            return {"known": False, "active": False, "quiescent": False, "state": "unknown"}
        try:
            subprocess.run(
                [str(self.systemctl), "--user", "stop", unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"known": False, "active": False, "quiescent": False, "state": "unknown"}
        for _ in range(10):
            status = self.inspect(unit)
            if status.get("quiescent"):
                return status
            time.sleep(0.1)
        return self.inspect(unit)


class FabricPeerService(base.FabricPeerService):
    """G4-C managed peer with durable writer and artifact semantics."""

    def __init__(
        self,
        *,
        unit_manager: Any | None = None,
        write_dispatch_fn: Callable[..., dict[str, Any]] | None = None,
        artifact_root: Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        _init_peer_g4c(self.db_path)
        self.unit_manager = unit_manager or SystemdUserUnitManager()
        self.write_dispatch_fn = write_dispatch_fn or self._dispatch_contained_write
        self.artifact_root = artifact_root or base._root(self.hermes_root) / "fabric" / "artifacts"

    def capabilities(self, policy: FabricPeerPolicy) -> dict[str, Any]:
        payload = super().capabilities(policy)
        features = list(payload["features"])
        for feature in (FEATURE_ARTIFACT, FEATURE_ARTIFACT_SNAPSHOT, FEATURE_RECONCILE):
            if feature not in features:
                features.append(feature)
        if self.unit_manager.available():
            for feature in (FEATURE_WRITE_OWNERSHIP, FEATURE_EXECUTION_UNIT, FEATURE_WRITE_EPOCH):
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
        request = _validate_g4c_request(request_value)
        principal = self.authenticate(authorization)
        if principal != request["coordinator_principal"]:
            raise FabricError("FABRIC_PRINCIPAL_AUTH_FAILED", "authenticated principal does not match request")
        policy = self.policy_loader()
        if principal not in policy.allowed_coordinator_principals:
            raise FabricError("FABRIC_PRINCIPAL_AUTH_FAILED", "coordinator principal is not authorized by peer policy")
        operation = request["operation"]
        if operation == "capabilities":
            base._closed(request["data"], required=set(), name="capabilities data")
            return base._response(operation, ok=True, code="FABRIC_OK", data=self.capabilities(policy))
        if operation == "accept":
            return self._accept(request, principal, policy)

        dispatch_id = base._bounded_string(request.get("dispatch_id"), field="dispatch_id", pattern=base._ID_RE)
        attempt_id = base._bounded_string(request.get("attempt_id"), field="attempt_id", pattern=base._ID_RE)
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

    def _acquire_claim(
        self,
        db: sqlite3.Connection,
        *,
        conflict_domain: str,
        attempt_id: str,
        unit_id: str,
    ) -> int:
        claim = db.execute(
            "SELECT * FROM write_claims WHERE conflict_domain=?",
            (conflict_domain,),
        ).fetchone()
        if claim is not None and claim["state"] == "ACTIVE":
            if claim["attempt_id"] == attempt_id:
                return int(claim["epoch"] or 0)
            raise FabricError(
                "FABRIC_WRITE_OWNERSHIP_BLOCKED",
                "peer write conflict domain already has an active claim",
            )
        epoch = int(claim["epoch"] or 0) + 1 if claim is not None else 1
        now = base._now()
        db.execute(
            "INSERT OR REPLACE INTO write_claims"
            "(conflict_domain,attempt_id,state,acquired_at,released_at,epoch,execution_unit_kind,execution_unit_id,release_proof)"
            " VALUES(?,?,?,?,NULL,?,?,?,NULL)",
            (conflict_domain, attempt_id, "ACTIVE", now, epoch, "systemd-user-unit", unit_id),
        )
        return epoch

    def _release_claim(self, row: sqlite3.Row, *, proof: str) -> bool:
        epoch = row["write_epoch"]
        if epoch is None:
            return False
        with _DB_LOCK, base._connect(self.db_path) as db:
            cursor = db.execute(
                "UPDATE write_claims SET state='RELEASED',released_at=?,release_proof=?"
                " WHERE conflict_domain=? AND attempt_id=? AND epoch=? AND state='ACTIVE'",
                (base._now(), proof[:128], row["conflict_domain"], row["attempt_id"], int(epoch)),
            )
        return bool(cursor.rowcount)

    def _claim_state(self, row: sqlite3.Row) -> str:
        if row["write_epoch"] is None:
            return "NONE"
        with base._connect_readonly(self.db_path) as db:
            claim = db.execute(
                "SELECT state,attempt_id,epoch FROM write_claims WHERE conflict_domain=?",
                (row["conflict_domain"],),
            ).fetchone()
        if claim is None or claim["attempt_id"] != row["attempt_id"] or int(claim["epoch"] or 0) != int(row["write_epoch"]):
            return "SUPERSEDED"
        return str(claim["state"])

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
        executable = backend.executable()
        if not executable:
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
        meta = {
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
        }
        runners._atomic_json(meta_path, meta)
        launch = self.unit_manager.launch(unit_id, task_id, workspace, runners._root(self.hermes_root))
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
                "code": str(launch.get("code") or "FABRIC_EXECUTION_UNIT_START_AMBIGUOUS"),
            }
        try:
            request_path.unlink()
        except OSError:
            pass
        meta.update(
            {
                "state": "failed",
                "outcome": "failed",
                "ended_at": runners._now(),
                "error": "contained Fabric runner failed to start",
            }
        )
        runners._atomic_json(meta_path, meta)
        return {
            "success": False,
            "changed": False,
            "code": str(launch.get("code") or "FABRIC_EXECUTION_UNIT_START_FAILED"),
        }

    def _accept(self, request: dict[str, Any], principal: str, policy: FabricPeerPolicy) -> dict[str, Any]:
        data = base._closed(request["data"], required={"envelope"}, name="accept data")
        envelope = _validate_g4c_envelope(data["envelope"])
        if request.get("dispatch_id") and request["dispatch_id"] != envelope["dispatch_id"]:
            raise FabricError("FABRIC_IDEMPOTENCY_CONFLICT", "request and envelope dispatch identity differ")
        if request.get("attempt_id") and request["attempt_id"] != envelope["attempt_id"]:
            raise FabricError("FABRIC_IDEMPOTENCY_CONFLICT", "request and envelope attempt identity differ")
        mapping = self._authorize_envelope(envelope, principal, policy)
        envelope_sha = sha256_json(data["envelope"])
        now = base._now()
        is_write = _is_write_auth(envelope)
        unit_id = ""
        if is_write:
            self._write_backend_eligible(envelope["remote_backend"])
            unit_id = self.unit_manager.unit_name(envelope["attempt_id"])

        with self._lock, _DB_LOCK, base._connect(self.db_path) as db:
            existing = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (envelope["attempt_id"],)).fetchone()
            if existing is not None:
                if existing["dispatch_id"] != envelope["dispatch_id"] or existing["envelope_sha256"] != envelope_sha:
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
                    },
                )

            epoch: int | None = None
            if is_write:
                epoch = self._acquire_claim(
                    db,
                    conflict_domain=mapping.conflict_domain,
                    attempt_id=envelope["attempt_id"],
                    unit_id=unit_id,
                )
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

        try:
            prestart = self.policy_loader()
            prestart_mapping = self._authorize_envelope(envelope, principal, prestart)
            if (
                prestart_mapping.local_path != mapping.local_path
                or prestart_mapping.revision != mapping.revision
                or prestart_mapping.conflict_domain != mapping.conflict_domain
            ):
                raise FabricError("FABRIC_POLICY_DRIFT", "peer workspace policy changed before runner start")
            backend = runners.get_backend(envelope["remote_backend"])
            if not bool(backend.availability(hermes_root=self.hermes_root).get("available")):
                raise FabricError("FABRIC_RUNNER_UNAVAILABLE", "remote runner is unavailable at pre-start revalidation")
            if is_write:
                self._write_backend_eligible(envelope["remote_backend"])
        except (FabricError, LookupError) as exc:
            with base._connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state='BLOCKED',policy_sha256=?,updated_at=? WHERE attempt_id=?",
                    (getattr(locals().get("prestart", None), "digest", policy.digest), base._now(), envelope["attempt_id"]),
                )
            row = self._row(envelope["dispatch_id"], envelope["attempt_id"])
            if is_write:
                self._release_claim(row, proof="prestart_blocked_no_execution_unit")
            if isinstance(exc, FabricError):
                raise
            raise FabricError("FABRIC_RUNNER_UNAVAILABLE", "remote runner is not registered") from exc

        local_contract = self._local_contract(envelope, prestart_mapping)
        if is_write:
            result = self.write_dispatch_fn(
                local_contract,
                backend_name=envelope["remote_backend"],
                unit_id=unit_id,
                timeout=30,
            )
        else:
            result = self.dispatch_fn(local_contract, timeout=30)
        if not isinstance(result, dict):
            result = {"success": False, "code": "FABRIC_RUNNER_INVALID_RESULT"}
        if is_write and result.get("ambiguous"):
            state = "LOST_AMBIGUOUS"
        else:
            state = "RUNNING" if bool(result.get("success")) else "FAILED"
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,dispatch_result_json=?,policy_sha256=?,updated_at=? WHERE attempt_id=?",
                (
                    state,
                    canonical_json(base._bounded_json(result, field="dispatch_result")),
                    prestart.digest,
                    base._now(),
                    envelope["attempt_id"],
                ),
            )
        row = self._row(envelope["dispatch_id"], envelope["attempt_id"])
        if is_write and state == "FAILED":
            unit_state = self.unit_manager.inspect(unit_id)
            if unit_state.get("quiescent"):
                self._release_claim(row, proof="known_start_failure_unit_quiescent")
            else:
                state = "LOST_AMBIGUOUS"
                with base._connect(self.db_path) as db:
                    db.execute(
                        "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                        (state, base._now(), envelope["attempt_id"]),
                    )

        base._audit(
            "hermes_fabric_peer_accept",
            success=bool(result.get("success")),
            changed=bool(result.get("success") or result.get("ambiguous")),
            summary=f"Fabric attempt accepted on {policy.node_name}",
            extra={
                "dispatch_id": envelope["dispatch_id"],
                "attempt_id": envelope["attempt_id"],
                "task_id": envelope["task_id"],
                "backend": envelope["remote_backend"],
                "principal": principal,
                "policy_sha256": prestart.digest,
                "write_epoch": row["write_epoch"] or 0,
            },
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
            },
        )

    def _status(self, dispatch_id: str, attempt_id: str, *, reconcile: bool) -> dict[str, Any]:
        row = self._row(dispatch_id, attempt_id)
        if row["authorization_class"] not in _WRITE_AUTH:
            return super()._status(dispatch_id, attempt_id, reconcile=reconcile)
        if row["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "local_task_id": row["local_task_id"],
                "policy_sha256": row["policy_sha256"],
                "write_epoch": row["write_epoch"],
                "write_claim_state": self._claim_state(row),
                "execution_unit_state": "terminal",
            }

        primary = base._latest_run(self.observed_fn(row["local_task_id"]))
        unit = self.unit_manager.inspect(str(row["execution_unit_id"] or ""))
        run_state = ""
        if primary is not None:
            run_state = str(primary.get("state") or primary.get("status") or "").lower()
        mapped_terminal = (
            "SUCCEEDED"
            if run_state in {"completed", "succeeded", "success"}
            else "FAILED"
            if run_state in {"failed", "error"}
            else "CANCELLED"
            if run_state in {"cancelled", "canceled"}
            else ""
        )
        if mapped_terminal and unit.get("quiescent"):
            state = mapped_terminal
            self._release_claim(row, proof="execution_unit_terminal_and_quiescent")
        elif mapped_terminal and unit.get("active"):
            state = "RUNNING"
        elif mapped_terminal:
            state = "LOST_AMBIGUOUS"
        elif unit.get("active"):
            state = "RUNNING"
        elif unit.get("quiescent"):
            state = "LOST_AMBIGUOUS"
            if reconcile:
                self._release_claim(row, proof="execution_unit_quiescent_outcome_ambiguous")
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
            "write_claim_state": self._claim_state(refreshed),
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
        if row["authorization_class"] not in _WRITE_AUTH:
            return super()._cancel(dispatch_id, attempt_id, principal, policy)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError("FABRIC_PRINCIPAL_AUTH_FAILED", "cancel identity does not match accepted attempt")
        if row["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "idempotent": True,
                "write_epoch": row["write_epoch"],
                "write_claim_state": self._claim_state(row),
            }
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state='CANCEL_REQUESTED',updated_at=? WHERE attempt_id=?",
                (base._now(), attempt_id),
            )
        unit = self.unit_manager.stop(str(row["execution_unit_id"] or ""))
        if unit.get("quiescent"):
            result = self.cancel_fn(row["remote_backend"], row["local_task_id"])
            primary = base._latest_run(self.observed_fn(row["local_task_id"]))
            run_state = str((primary or {}).get("state") or (primary or {}).get("status") or "").lower()
            state = (
                "SUCCEEDED"
                if run_state in {"completed", "succeeded", "success"}
                else "FAILED"
                if run_state in {"failed", "error"}
                else "CANCELLED"
            )
            self._release_claim(row, proof="cancel_execution_unit_quiescent")
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
            "write_claim_state": self._claim_state(refreshed),
        }

    def _safe_source(self, mapping: WorkspaceMapping, logical_name: str) -> Path:
        name = _logical_artifact_name(logical_name)
        candidate = mapping.local_path.joinpath(*PurePosixPath(name).parts)
        current = mapping.local_path
        for part in PurePosixPath(name).parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise FabricError("FABRIC_ARTIFACT_SYMLINK_REJECTED", "artifact path contains a symlink")
            except OSError as exc:
                raise FabricError("FABRIC_ARTIFACT_UNREADABLE", "artifact path could not be inspected") from exc
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            raise
        if op.is_denied_path(resolved) or not op.path_under_allowed(resolved, [mapping.local_path]):
            raise FabricError("FABRIC_ARTIFACT_PATH_REJECTED", "artifact path is outside the approved workspace")
        try:
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise FabricError("FABRIC_ARTIFACT_UNREADABLE", "artifact could not be inspected") from exc
        if not stat.S_ISREG(mode):
            raise FabricError("FABRIC_ARTIFACT_SPECIAL_FILE_REJECTED", "artifact must be a regular file")
        return resolved

    def _snapshot_one(self, row: sqlite3.Row, mapping: WorkspaceMapping, logical_name: str) -> dict[str, Any]:
        logical_name = _logical_artifact_name(logical_name)
        with base._connect_readonly(self.db_path) as db:
            existing = db.execute(
                "SELECT * FROM artifacts WHERE attempt_id=? AND logical_name=?",
                (row["attempt_id"], logical_name),
            ).fetchone()
        if existing is not None:
            return self._manifest_item(existing)
        source = self._safe_source(mapping, logical_name)
        artifact_id = "fart-" + hashlib.sha256(
            f"{row['attempt_id']}:{logical_name}".encode()
        ).hexdigest()[:32]
        target_dir = self.artifact_root / row["attempt_id"]
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            target_dir.chmod(0o700)
        except OSError:
            pass
        target = target_dir / f"{artifact_id}.blob"
        tmp = target.with_suffix(".tmp")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(source, flags)
        digest = hashlib.sha256()
        total = 0
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise FabricError("FABRIC_ARTIFACT_SPECIAL_FILE_REJECTED", "artifact must be a regular file")
            if before.st_size > _MAX_ARTIFACT_BYTES:
                raise FabricError("FABRIC_ARTIFACT_TOO_LARGE", "artifact exceeds peer size policy")
            with os.fdopen(fd, "rb", closefd=False) as src, tmp.open("wb") as dst:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_ARTIFACT_BYTES:
                        raise FabricError("FABRIC_ARTIFACT_TOO_LARGE", "artifact exceeds peer size policy")
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            after = os.fstat(fd)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or total != after.st_size
            ):
                raise FabricError("FABRIC_ARTIFACT_UNSTABLE", "artifact changed while being finalized")
        finally:
            os.close(fd)
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(target)
        media_type = mimetypes.guess_type(logical_name)[0] or "application/octet-stream"
        finalized = base._now()
        with _DB_LOCK, base._connect(self.db_path) as db:
            db.execute(
                "INSERT INTO artifacts"
                "(artifact_id,attempt_id,dispatch_id,contract_sha256,logical_name,snapshot_path,size_bytes,sha256,media_type,active_content,finalized_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    row["attempt_id"],
                    row["dispatch_id"],
                    row["contract_sha256"],
                    logical_name,
                    str(target),
                    total,
                    digest.hexdigest(),
                    media_type,
                    1 if media_type in _ACTIVE_MEDIA else 0,
                    finalized,
                ),
            )
        with base._connect_readonly(self.db_path) as db:
            saved = db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if saved is None:
            raise FabricError("FABRIC_ARTIFACT_SNAPSHOT_FAILED", "artifact snapshot journal write failed")
        return self._manifest_item(saved)

    @staticmethod
    def _manifest_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "artifact_id": row["artifact_id"],
            "logical_name": row["logical_name"],
            "size_bytes": int(row["size_bytes"]),
            "sha256": row["sha256"],
            "media_type": row["media_type"],
            "active_content": bool(row["active_content"]),
            "finalized_at": row["finalized_at"],
        }

    def _artifact_manifest(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        data = base._closed(
            request["data"],
            required={"artifacts"},
            optional={"max_artifact_bytes", "max_total_bytes"},
            name="artifact manifest data",
        )
        dispatch_id = base._bounded_string(request.get("dispatch_id"), field="dispatch_id", pattern=base._ID_RE)
        attempt_id = base._bounded_string(request.get("attempt_id"), field="attempt_id", pattern=base._ID_RE)
        row = self._row(dispatch_id, attempt_id)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError("FABRIC_PRINCIPAL_AUTH_FAILED", "artifact identity does not match accepted attempt")
        status = self._status(dispatch_id, attempt_id, reconcile=False)
        if status["state"] != "SUCCEEDED":
            raise FabricError("FABRIC_ARTIFACT_NOT_READY", "artifacts may be finalized only after successful terminal execution")
        if self._claim_state(row) == "ACTIVE":
            raise FabricError("FABRIC_ARTIFACT_NOT_READY", "write claim is still active")
        raw_specs = data["artifacts"]
        if not isinstance(raw_specs, list) or len(raw_specs) > _MAX_ARTIFACTS:
            raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact manifest request is not bounded")
        max_one = min(int(data.get("max_artifact_bytes", _MAX_ARTIFACT_BYTES)), _MAX_ARTIFACT_BYTES)
        max_total = min(int(data.get("max_total_bytes", _MAX_TOTAL_ARTIFACT_BYTES)), _MAX_TOTAL_ARTIFACT_BYTES)
        if max_one <= 0 or max_total <= 0:
            raise FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact size limits are invalid")
        mapping = policy.workspace_mappings.get(row["logical_workspace"])
        if mapping is None:
            raise FabricError("FABRIC_WORKSPACE_DENIED", "attempt workspace is no longer mapped")
        items: list[dict[str, Any]] = []
        total = 0
        for spec in raw_specs:
            spec = base._closed(spec, required={"path", "must_exist", "min_bytes"}, name="artifact spec")
            logical_name = _logical_artifact_name(spec["path"])
            try:
                item = self._snapshot_one(row, mapping, logical_name)
            except FileNotFoundError:
                if bool(spec["must_exist"]):
                    raise FabricError("FABRIC_ARTIFACT_MISSING", "required artifact is missing")
                continue
            if item["size_bytes"] > max_one:
                raise FabricError("FABRIC_ARTIFACT_TOO_LARGE", "artifact exceeds coordinator-requested bound")
            total += int(item["size_bytes"])
            if total > max_total:
                raise FabricError("FABRIC_ARTIFACT_TOTAL_TOO_LARGE", "artifact set exceeds bounded total")
            items.append(item)
        manifest = {
            "schema": ARTIFACT_MANIFEST_SCHEMA,
            "version": 1,
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "contract_sha256": row["contract_sha256"],
            "node_name": row["node_name"],
            "artifacts": items,
            "total_bytes": total,
            "created_at": base._now(),
        }
        manifest["manifest_sha256"] = sha256_json(manifest)
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
        dispatch_id = base._bounded_string(request.get("dispatch_id"), field="dispatch_id", pattern=base._ID_RE)
        attempt_id = base._bounded_string(request.get("attempt_id"), field="attempt_id", pattern=base._ID_RE)
        artifact_id = base._bounded_string(data["artifact_id"], field="artifact_id", pattern=base._ID_RE)
        offset = data["offset"]
        maximum = data["max_bytes"]
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 1 <= maximum <= _MAX_CHUNK_BYTES
        ):
            raise FabricError("FABRIC_ARTIFACT_CHUNK_INVALID", "artifact chunk bounds are invalid")
        attempt = self._row(dispatch_id, attempt_id)
        if attempt["coordinator_principal"] != principal or attempt["node_name"] != policy.node_name:
            raise FabricError("FABRIC_PRINCIPAL_AUTH_FAILED", "artifact chunk identity mismatch")
        with base._connect_readonly(self.db_path) as db:
            artifact = db.execute(
                "SELECT * FROM artifacts WHERE artifact_id=? AND attempt_id=? AND dispatch_id=?",
                (artifact_id, attempt_id, dispatch_id),
            ).fetchone()
        if artifact is None:
            raise FabricError("FABRIC_ARTIFACT_NOT_FOUND", "artifact snapshot is not present")
        snapshot = Path(artifact["snapshot_path"])
        try:
            if snapshot.is_symlink():
                raise FabricError("FABRIC_ARTIFACT_SNAPSHOT_INVALID", "artifact snapshot may not be a symlink")
            st = snapshot.stat()
        except OSError as exc:
            raise FabricError("FABRIC_ARTIFACT_SNAPSHOT_INVALID", "artifact snapshot cannot be inspected") from exc
        if not stat.S_ISREG(st.st_mode) or st.st_size != int(artifact["size_bytes"]):
            raise FabricError("FABRIC_ARTIFACT_SNAPSHOT_INVALID", "artifact snapshot identity changed")
        if offset > st.st_size:
            raise FabricError("FABRIC_ARTIFACT_CHUNK_INVALID", "artifact chunk offset exceeds size")
        with snapshot.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read(maximum)
        next_offset = offset + len(chunk)
        return {
            "chunk": {
                "schema": ARTIFACT_CHUNK_SCHEMA,
                "version": 1,
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "artifact_id": artifact_id,
                "offset": offset,
                "next_offset": next_offset,
                "size_bytes": int(artifact["size_bytes"]),
                "sha256": artifact["sha256"],
                "data_b64": base64.b64encode(chunk).decode("ascii"),
                "eof": next_offset == int(artifact["size_bytes"]),
            }
        }


class FabricCoordinator(base.FabricCoordinator):
    """Coordinator with G4-C preflight, artifacts, retry, and ambiguity state."""

    def _ensure_db(self) -> None:
        _init_coordinator_g4c(self.db_path)

    def _store_artifact_specs(self, result: dict[str, Any], contract: dict[str, Any], specs: list[dict[str, Any]]) -> None:
        attempt_id = result.get("attempt_id")
        dispatch_id = result.get("dispatch_id")
        if not specs or not isinstance(attempt_id, str) or not isinstance(dispatch_id, str):
            return
        self._ensure_db()
        with base._connect(self.db_path) as db:
            dispatch = db.execute("SELECT contract_sha256 FROM dispatches WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            contract_sha = dispatch["contract_sha256"] if dispatch is not None else base._contract_sha(contract)
            db.execute(
                "INSERT OR REPLACE INTO artifact_requests(attempt_id,dispatch_id,contract_sha256,specs_json,created_at)"
                " VALUES(?,?,?,?,?)",
                (attempt_id, dispatch_id, contract_sha, canonical_json(specs), base._now()),
            )

    def dispatch(self, contract: dict[str, Any], *, dry_run: bool, confirm: bool, timeout: int) -> dict[str, Any]:
        specs = _artifact_specs(contract)
        node_name, _remote_backend, _workspace, _options, _evidence = base._fabric_options(contract)
        node = self._node(node_name)
        auth_class = str((contract.get("authorization") or {}).get("class") or "")
        if not dry_run and (specs or auth_class in _WRITE_AUTH):
            capabilities = self._capabilities(node, timeout)
            features = set(capabilities.get("features") or [])
            if specs and FEATURE_ARTIFACT not in features:
                raise FabricError("FABRIC_ARTIFACT_ADMISSION_UNAVAILABLE", "managed peer lacks bounded artifact transfer")
            if auth_class in _WRITE_AUTH and not {FEATURE_WRITE_OWNERSHIP, FEATURE_EXECUTION_UNIT, FEATURE_WRITE_EPOCH} <= features:
                raise FabricError("FABRIC_EXECUTION_UNIT_UNAVAILABLE", "managed peer lacks verified write containment")
        result = super().dispatch(contract, dry_run=dry_run, confirm=confirm, timeout=timeout)
        if not dry_run:
            self._store_artifact_specs(result, contract, specs)
        return result

    def poll(self, attempt_id: str, *, reconcile: bool = False, timeout: int = 15) -> dict[str, Any]:
        self._ensure_db()
        try:
            result = super().poll(attempt_id, reconcile=reconcile, timeout=timeout)
        except FabricError as exc:
            attempt, dispatch, node = self._attempt(attempt_id)
            state = "RECONCILING" if exc.code in {"FABRIC_TRANSPORT_TIMEOUT", "FABRIC_PEER_UNAVAILABLE"} else "BLOCKED"
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
        attempt, _dispatch, _node = self._attempt(attempt_id)
        node = self._node(attempt["node_name"])
        try:
            _, response = self.rpc(
                node,
                base._request(
                    "status",
                    node.coordinator_principal,
                    data={},
                    dispatch_id=attempt["dispatch_id"],
                    attempt_id=attempt_id,
                ),
                timeout,
            )
            response = base._validate_response(response, operation="status")
            data = response.get("data") or {}
            epoch = data.get("write_epoch")
            if isinstance(epoch, int) and not isinstance(epoch, bool):
                with base._connect(self.db_path) as db:
                    db.execute("UPDATE attempts SET write_epoch=? WHERE attempt_id=?", (epoch, attempt_id))
                result["write_epoch"] = epoch
            if isinstance(data.get("write_claim_state"), str):
                result["write_claim_state"] = data["write_claim_state"]
            if isinstance(data.get("execution_unit_state"), str):
                result["execution_unit_state"] = data["execution_unit_state"]
        except FabricError:
            pass
        return result

    def cancel(self, attempt_id: str, *, timeout: int = 15) -> dict[str, Any]:
        self._ensure_db()
        try:
            return super().cancel(attempt_id, timeout=timeout)
        except FabricError as exc:
            attempt, dispatch, node = self._attempt(attempt_id)
            if exc.code in {"FABRIC_TRANSPORT_TIMEOUT", "FABRIC_PEER_UNAVAILABLE"}:
                with base._connect(self.db_path) as db:
                    db.execute(
                        "UPDATE attempts SET state='CANCEL_AMBIGUOUS',error_code=?,updated_at=? WHERE attempt_id=?",
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
            raise

    def _artifact_request_row(self, attempt_id: str) -> sqlite3.Row | None:
        self._ensure_db()
        with base._connect_readonly(self.db_path) as db:
            return db.execute("SELECT * FROM artifact_requests WHERE attempt_id=?", (attempt_id,)).fetchone()

    def _validate_manifest(
        self,
        manifest: Any,
        *,
        attempt: sqlite3.Row,
        dispatch: sqlite3.Row,
        node: FabricNode,
        specs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        manifest = base._closed(
            manifest,
            required={
                "schema",
                "version",
                "dispatch_id",
                "attempt_id",
                "contract_sha256",
                "node_name",
                "artifacts",
                "total_bytes",
                "created_at",
                "manifest_sha256",
            },
            name="artifact manifest",
        )
        expected_sha = sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"})
        if manifest["manifest_sha256"] != expected_sha:
            raise FabricError("FABRIC_ARTIFACT_MANIFEST_REJECTED", "artifact manifest digest is invalid")
        if (
            manifest["schema"] != ARTIFACT_MANIFEST_SCHEMA
            or manifest["version"] != 1
            or manifest["dispatch_id"] != attempt["dispatch_id"]
            or manifest["attempt_id"] != attempt["attempt_id"]
            or manifest["contract_sha256"] != dispatch["contract_sha256"]
            or manifest["node_name"] != node.name
        ):
            raise FabricError("FABRIC_ARTIFACT_LINEAGE_MISMATCH", "artifact manifest lineage does not match attempt")
        artifacts = manifest["artifacts"]
        if not isinstance(artifacts, list) or len(artifacts) > _MAX_ARTIFACTS:
            raise FabricError("FABRIC_ARTIFACT_MANIFEST_REJECTED", "artifact manifest is not bounded")
        spec_map = {item["path"]: item for item in specs}
        seen: set[str] = set()
        clean: list[dict[str, Any]] = []
        total = 0
        for item in artifacts:
            item = base._closed(
                item,
                required={"artifact_id", "logical_name", "size_bytes", "sha256", "media_type", "active_content", "finalized_at"},
                name="artifact manifest item",
            )
            artifact_id = base._bounded_string(item["artifact_id"], field="artifact_id", pattern=base._ID_RE)
            logical_name = _logical_artifact_name(item["logical_name"])
            if logical_name not in spec_map or logical_name in seen:
                raise FabricError("FABRIC_ARTIFACT_MANIFEST_REJECTED", "manifest contains unexpected/duplicate artifact")
            size = item["size_bytes"]
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_ARTIFACT_BYTES:
                raise FabricError("FABRIC_ARTIFACT_TOO_LARGE", "artifact size is invalid or exceeds bound")
            sha = base._bounded_string(item["sha256"], field="artifact.sha256", pattern=base._SHA_RE)
            media = base._bounded_string(item["media_type"], field="artifact.media_type", maximum=128)
            if not isinstance(item["active_content"], bool):
                raise FabricError("FABRIC_ARTIFACT_MANIFEST_REJECTED", "artifact active_content must be boolean")
            total += size
            if total > _MAX_TOTAL_ARTIFACT_BYTES:
                raise FabricError("FABRIC_ARTIFACT_TOTAL_TOO_LARGE", "artifact manifest exceeds total bound")
            clean.append(
                {
                    "artifact_id": artifact_id,
                    "logical_name": logical_name,
                    "size_bytes": size,
                    "sha256": sha,
                    "media_type": media,
                    "active_content": item["active_content"],
                    "finalized_at": str(item["finalized_at"]),
                }
            )
            seen.add(logical_name)
        for spec in specs:
            if spec["must_exist"] and spec["path"] not in seen:
                raise FabricError("FABRIC_ARTIFACT_MISSING", "required artifact is absent from peer manifest")
        if manifest["total_bytes"] != total:
            raise FabricError("FABRIC_ARTIFACT_MANIFEST_REJECTED", "artifact manifest total is inconsistent")
        return clean

    def _pull_artifact(
        self,
        attempt: sqlite3.Row,
        node: FabricNode,
        item: dict[str, Any],
        *,
        timeout: int,
    ) -> dict[str, Any]:
        admission_dir = base._root(self.hermes_root) / "fabric" / "admitted" / attempt["attempt_id"]
        admission_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            admission_dir.chmod(0o700)
        except OSError:
            pass
        target = admission_dir / f"{item['artifact_id']}.blob"
        tmp = target.with_suffix(".tmp")
        digest = hashlib.sha256()
        offset = 0
        with tmp.open("wb") as fh:
            while offset < item["size_bytes"]:
                _, response = self.rpc(
                    node,
                    base._request(
                        "artifact_chunk",
                        node.coordinator_principal,
                        data={
                            "artifact_id": item["artifact_id"],
                            "offset": offset,
                            "max_bytes": min(_MAX_CHUNK_BYTES, item["size_bytes"] - offset),
                        },
                        dispatch_id=attempt["dispatch_id"],
                        attempt_id=attempt["attempt_id"],
                    ),
                    timeout,
                )
                response = base._validate_response(response, operation="artifact_chunk")
                data = base._closed(response["data"], required={"chunk"}, name="artifact chunk response")
                chunk = base._closed(
                    data["chunk"],
                    required={
                        "schema",
                        "version",
                        "dispatch_id",
                        "attempt_id",
                        "artifact_id",
                        "offset",
                        "next_offset",
                        "size_bytes",
                        "sha256",
                        "data_b64",
                        "eof",
                    },
                    name="artifact chunk",
                )
                if (
                    chunk["schema"] != ARTIFACT_CHUNK_SCHEMA
                    or chunk["version"] != 1
                    or chunk["dispatch_id"] != attempt["dispatch_id"]
                    or chunk["attempt_id"] != attempt["attempt_id"]
                    or chunk["artifact_id"] != item["artifact_id"]
                    or chunk["offset"] != offset
                    or chunk["size_bytes"] != item["size_bytes"]
                    or chunk["sha256"] != item["sha256"]
                ):
                    raise FabricError("FABRIC_ARTIFACT_LINEAGE_MISMATCH", "artifact chunk lineage mismatch")
                try:
                    raw = base64.b64decode(chunk["data_b64"], validate=True)
                except (ValueError, TypeError) as exc:
                    raise FabricError("FABRIC_ARTIFACT_CHUNK_INVALID", "artifact chunk is not valid base64") from exc
                next_offset = chunk["next_offset"]
                if not isinstance(next_offset, int) or next_offset != offset + len(raw) or next_offset > item["size_bytes"]:
                    raise FabricError("FABRIC_ARTIFACT_CHUNK_INVALID", "artifact chunk offset progression is invalid")
                if not raw and next_offset < item["size_bytes"]:
                    raise FabricError("FABRIC_ARTIFACT_CHUNK_INVALID", "artifact chunk made no progress")
                digest.update(raw)
                fh.write(raw)
                offset = next_offset
                if bool(chunk["eof"]) != (offset == item["size_bytes"]):
                    raise FabricError("FABRIC_ARTIFACT_CHUNK_INVALID", "artifact EOF marker is inconsistent")
            fh.flush()
            os.fsync(fh.fileno())
        if offset != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise FabricError("FABRIC_ARTIFACT_HASH_MISMATCH", "artifact bytes failed coordinator verification")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(target)
        with base._connect(self.db_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO artifact_admissions"
                "(artifact_id,attempt_id,dispatch_id,logical_name,admission_path,size_bytes,sha256,media_type,active_content,admitted_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    item["artifact_id"],
                    attempt["attempt_id"],
                    attempt["dispatch_id"],
                    item["logical_name"],
                    str(target),
                    item["size_bytes"],
                    item["sha256"],
                    item["media_type"],
                    1 if item["active_content"] else 0,
                    base._now(),
                ),
            )
        return {
            "artifact_id": item["artifact_id"],
            "logical_name": item["logical_name"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
            "media_type": item["media_type"],
            "active_content": item["active_content"],
            "admitted": True,
        }

    def collect_artifacts(self, attempt_id: str, *, timeout: int = 15) -> list[dict[str, Any]]:
        request_row = self._artifact_request_row(attempt_id)
        if request_row is None:
            return []
        attempt, dispatch, node = self._attempt(attempt_id)
        specs_raw = strict_json_loads(request_row["specs_json"], maximum=32_000)
        specs = specs_raw if isinstance(specs_raw, list) else []
        _, response = self.rpc(
            node,
            base._request(
                "artifact_manifest",
                node.coordinator_principal,
                data={
                    "artifacts": specs,
                    "max_artifact_bytes": _MAX_ARTIFACT_BYTES,
                    "max_total_bytes": _MAX_TOTAL_ARTIFACT_BYTES,
                },
                dispatch_id=attempt["dispatch_id"],
                attempt_id=attempt_id,
            ),
            timeout,
        )
        response = base._validate_response(response, operation="artifact_manifest")
        data = base._closed(response["data"], required={"manifest"}, name="artifact manifest response")
        items = self._validate_manifest(
            data["manifest"],
            attempt=attempt,
            dispatch=dispatch,
            node=node,
            specs=specs,
        )
        admitted: list[dict[str, Any]] = []
        spec_map = {spec["path"]: spec for spec in specs}
        total = 0
        for item in items:
            total += item["size_bytes"]
            if total > _MAX_TOTAL_ARTIFACT_BYTES:
                raise FabricError("FABRIC_ARTIFACT_TOTAL_TOO_LARGE", "artifact admission exceeds total bound")
            receipt = self._pull_artifact(attempt, node, item, timeout=timeout)
            if receipt["size_bytes"] < int(spec_map[item["logical_name"]]["min_bytes"]):
                raise FabricError("FABRIC_ARTIFACT_TOO_SMALL", "artifact is below the contract minimum size")
            admitted.append(receipt)
        return admitted

    def collect(self, attempt_id: str, *, timeout: int = 15) -> dict[str, Any]:
        request_row = self._artifact_request_row(attempt_id)
        artifacts: list[dict[str, Any]] = []
        if request_row is not None:
            try:
                artifacts = self.collect_artifacts(attempt_id, timeout=timeout)
            except FabricError:
                with base._connect(self.db_path) as db:
                    db.execute(
                        "UPDATE attempts SET state='EVIDENCE_PENDING',updated_at=? WHERE attempt_id=?",
                        (base._now(), attempt_id),
                    )
                raise
        result = super().collect(attempt_id, timeout=timeout)
        if request_row is not None:
            result["artifacts"] = artifacts
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
        self._ensure_db()
        prior, dispatch, node = self._attempt(prior_attempt_id)
        if base._contract_sha(contract) != dispatch["contract_sha256"]:
            raise FabricError("FABRIC_RETRY_LINEAGE_MISMATCH", "retry contract differs from original dispatch")
        status = self.poll(prior_attempt_id, reconcile=True, timeout=timeout)
        if status.get("write_claim_state") == "ACTIVE":
            raise FabricError("FABRIC_WRITE_OWNERSHIP_BLOCKED", "prior write ownership remains active")
        if status.get("state") not in {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED", "TERMINAL_REPORTED"}:
            raise FabricError("FABRIC_RETRY_BLOCKED", "prior attempt is not safely reconciled")
        node_name, remote_backend, logical_workspace, remote_options, evidence_policy = base._fabric_options(contract)
        if node_name != node.name:
            raise FabricError("FABRIC_RETRY_LINEAGE_MISMATCH", "retry targets a different managed node")
        capabilities = self._capabilities(node, timeout)
        with base._connect_readonly(self.db_path) as db:
            count = db.execute("SELECT COUNT(*) AS n FROM attempts WHERE dispatch_id=?", (prior["dispatch_id"],)).fetchone()["n"]
        envelope = base._build_envelope(
            contract,
            node,
            remote_backend=remote_backend,
            logical_workspace=logical_workspace,
            remote_options=remote_options,
            evidence_policy=evidence_policy,
            capability_sha=capabilities["snapshot_sha256"],
        )
        envelope["attempt_id"] = base._attempt_id(prior["dispatch_id"], int(count) + 1)
        envelope["retry_parent_attempt_id"] = prior_attempt_id
        attempt_id = envelope["attempt_id"]
        envelope_sha = sha256_json(envelope)
        now = base._now()
        with base._connect(self.db_path) as db:
            db.execute(
                "INSERT INTO attempts"
                "(attempt_id,dispatch_id,envelope_sha256,node_name,peer_name,remote_backend,coordinator_principal,"
                "capability_sha256,peer_policy_sha256,state,remote_task_id,evidence_json,error_code,created_at,updated_at,retry_parent_attempt_id)"
                " VALUES(?,?,?,?,?,?,?,?,NULL,'SUBMITTING',NULL,NULL,NULL,?,?,?)",
                (
                    attempt_id,
                    prior["dispatch_id"],
                    envelope_sha,
                    node.name,
                    node.a2a_peer_name,
                    remote_backend,
                    node.coordinator_principal,
                    capabilities["snapshot_sha256"],
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
        epoch = data.get("write_epoch") if isinstance(data.get("write_epoch"), int) else None
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,remote_task_id=?,peer_policy_sha256=?,write_epoch=?,error_code=?,updated_at=?"
                " WHERE attempt_id=?",
                (
                    state,
                    remote_task_id,
                    data.get("policy_sha256"),
                    epoch,
                    None if response["ok"] else response["code"],
                    base._now(),
                    attempt_id,
                ),
            )
        specs = _artifact_specs(contract)
        self._store_artifact_specs(
            {"attempt_id": attempt_id, "dispatch_id": prior["dispatch_id"]},
            contract,
            specs,
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
            "code": response["code"],
        }

    def reconcile_active(self, *, timeout: int = 10) -> list[dict[str, Any]]:
        self._ensure_db()
        with base._connect_readonly(self.db_path) as db:
            rows = db.execute(
                "SELECT attempt_id,state FROM attempts WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED') ORDER BY created_at LIMIT 128"
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                results.append(self.poll(row["attempt_id"], reconcile=True, timeout=timeout))
            except FabricError as exc:
                results.append({"attempt_id": row["attempt_id"], "success": False, "state": "BLOCKED", "code": exc.code})
        return results


class AutoRouter(router.AutoRouter):
    """G4-B router with remote G4-C hard guards unlocked by live features."""

    def __init__(self, *, remote_probe: Callable[[FabricNode, int], dict[str, Any]] | None = None, hermes_root: Path | None = None, **kwargs: Any) -> None:
        self._g4c_features: dict[str, set[str]] = {}
        if remote_probe is None:
            coordinator = FabricCoordinator(hermes_root=hermes_root)

            def probe(node: FabricNode, timeout: int) -> dict[str, Any]:
                started = time.perf_counter()
                snapshot = coordinator._capabilities(node, timeout)
                features = set(snapshot.get("features") or [])
                self._g4c_features[node.name] = features
                return {
                    "healthy": True,
                    "latency_ms": (time.perf_counter() - started) * 1000.0,
                    "snapshot_sha256": snapshot.get("snapshot_sha256", ""),
                    "features": sorted(features),
                }

            selected_probe = probe
        else:
            def selected_probe(node: FabricNode, timeout: int) -> dict[str, Any]:
                result = remote_probe(node, timeout)
                features = result.get("features") if isinstance(result, dict) else None
                self._g4c_features[node.name] = set(features) if isinstance(features, (list, tuple, set)) else set()
                return result

        super().__init__(remote_probe=selected_probe, hermes_root=hermes_root, **kwargs)

    def route(self, contract: dict[str, Any], *, timeout: int = 15, dry_run: bool = False) -> dict[str, Any]:
        decision = super().route(contract, timeout=timeout, dry_run=dry_run)
        auth_class = str((contract.get("authorization") or {}).get("class") or "")
        needs_write = auth_class in _WRITE_AUTH
        needs_artifacts = bool(contract.get("expected_artifacts"))
        if not (needs_write or needs_artifacts):
            return decision
        for candidate in decision.get("candidates", []):
            if not candidate.get("remote"):
                continue
            features = self._g4c_features.get(str(candidate.get("node") or ""), set())
            exclusions = list(candidate.get("exclusions") or [])
            filtered: list[dict[str, str]] = []
            for exclusion in exclusions:
                code = exclusion.get("code")
                if code == "WRITE_CONFLICT_GUARD_UNAVAILABLE" and needs_write:
                    if {FEATURE_WRITE_OWNERSHIP, FEATURE_EXECUTION_UNIT, FEATURE_WRITE_EPOCH} <= features:
                        continue
                if code == "REMOTE_ARTIFACT_ADMISSION_UNAVAILABLE" and needs_artifacts:
                    if FEATURE_ARTIFACT in features and FEATURE_ARTIFACT_SNAPSHOT in features:
                        continue
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
                "rank": winner["rank"],
            }
        decision["g4c_guards"] = {
            "write_required": needs_write,
            "artifact_required": needs_artifacts,
        }
        router._audit_route(decision, success=decision["selected"] is not None, dry_run=dry_run)
        return decision


def register_runtime() -> None:
    """Install G4-C coordinator/router backends after G4-A/G4-B bootstrap."""
    base.FabricCoordinator = FabricCoordinator
    base.FabricPeerService = FabricPeerService
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
                    raise FabricError("FABRIC_PAYLOAD_TOO_LARGE", "A2A request has an invalid body size")
                outer = base._closed(
                    strict_json_loads(self.rfile.read(length)),
                    required={"jsonrpc", "id", "method", "params"},
                    name="A2A JSON-RPC request",
                )
                if outer["jsonrpc"] != "2.0" or outer["method"] not in {"SendMessage", "message/send"}:
                    raise FabricError("FABRIC_PROTOCOL_ERROR", "only A2A SendMessage is accepted by Fabric peer")
                params = base._closed(outer["params"], required={"message"}, name="A2A params")
                message = base._closed(
                    params["message"],
                    required={"role", "parts", "messageId", "contextId"},
                    name="A2A message",
                )
                if message["role"] != "ROLE_USER" or not isinstance(message["parts"], list) or len(message["parts"]) != 1:
                    raise FabricError("FABRIC_PROTOCOL_ERROR", "Fabric A2A message must contain one structured DataPart")
                raw_part = message["parts"][0]
                if (
                    not isinstance(raw_part, dict)
                    or "text" in raw_part
                    or "data" not in raw_part
                    or raw_part.get("mediaType") != "application/json"
                ):
                    raise FabricError("FABRIC_PROTOCOL_ERROR", "Fabric accepts only structured JSON DataParts")
                part = base._closed(raw_part, required={"data", "mediaType"}, name="A2A DataPart")
                request = _validate_g4c_request(part["data"])
                response = self.service.handle(request, self.headers.get("Authorization", ""))
                context_id = request.get("dispatch_id") or message["contextId"]
                task_id = "ftask-" + hashlib.sha256(
                    f"{request['request_id']}:{request.get('attempt_id', '')}".encode()
                ).hexdigest()[:24]
                result = {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {
                            "role": "ROLE_AGENT",
                            "parts": [{"data": response, "mediaType": "application/json"}],
                            "messageId": "resp-" + task_id[6:],
                            "contextId": context_id,
                        },
                    },
                }
                self._send(200, {"jsonrpc": "2.0", "id": outer["id"], "result": result})
            except FabricError as exc:
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": outer.get("id") if isinstance(outer, dict) else None,
                        "error": {"code": -32001, "message": str(exc)[:300], "data": {"code": exc.code}},
                    },
                )
            except (OSError, ValueError, TypeError, sqlite3.Error):
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": outer.get("id") if isinstance(outer, dict) else None,
                        "error": {"code": -32603, "message": "internal Fabric peer error", "data": {"code": "FABRIC_INTERNAL_ERROR"}},
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
