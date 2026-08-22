"""Unified durable delegation lifecycle for Hermes GPT v0.9.

Delegations are orchestration records, not execution authorities. The canonical
Work Contract and its selected runner/Fabric backend remain authoritative for
scope, mutation gates, dispatch, cancellation, and completion evidence.

This store deliberately persists no objective/prompt/transcript. It records only
bounded lineage and normalized lifecycle metadata so Missions and clients can
observe Pi, OpenCode, Codex, Fleet/Fabric, and future runner backends uniformly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_contract as contract_mod
import operator_mission_runtime as mission_runtime
import operator_policy as op
import operator_runners as runners

SCHEMA_VERSION = "0.9-delegation.2"
DELEGATION_SCHEMA = "hermes.delegation/v1"
DELEGATION_ID_RE = re.compile(r"^dlg-[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
STATES = frozenset({"reserved", "queued", "running", "reconciling", "succeeded", "failed", "cancelled"})
DISPATCH_PHASES = frozenset({"reserved", "invoking", "dispatched"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
MAX_LIST = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(hermes_root: Path | None = None) -> Path:
    if hermes_root is not None:
        normalized = op.normalize_hermes_data_root(Path(hermes_root).expanduser())
        return Path(normalized or hermes_root)
    raw = os.environ.get("HERMES_HOME", "").strip()
    if raw:
        normalized = op.normalize_hermes_data_root(Path(raw).expanduser())
        if normalized is not None:
            return Path(normalized)
    return Path.home() / ".hermes"


def _db_path(hermes_root: Path | None = None) -> Path:
    return _root(hermes_root) / "delegations" / "delegations.db"


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(path)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    else:
        if not path.is_file():
            raise FileNotFoundError(path)
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _init(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS delegations (
            delegation_id TEXT PRIMARY KEY,
            schema TEXT NOT NULL,
            mission_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL UNIQUE,
            contract_sha256 TEXT NOT NULL,
            backend TEXT NOT NULL,
            state TEXT NOT NULL,
            backend_state TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            backend_ref_json TEXT NOT NULL DEFAULT '{}',
            validation_verdict TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            dispatch_phase TEXT NOT NULL DEFAULT 'dispatched',
            created_at TEXT NOT NULL,
            dispatched_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            terminal_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_delegations_mission ON delegations(mission_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_delegations_state ON delegations(state, updated_at);
        CREATE TABLE IF NOT EXISTS delegation_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            delegation_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_state TEXT NOT NULL DEFAULT '',
            to_state TEXT NOT NULL DEFAULT '',
            backend_state TEXT NOT NULL DEFAULT '',
            observed_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (delegation_id) REFERENCES delegations(delegation_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_delegation_events ON delegation_events(delegation_id, seq);
        CREATE TABLE IF NOT EXISTS delegation_validation_manifests (
            delegation_id TEXT PRIMARY KEY,
            schema TEXT NOT NULL,
            context_sha256 TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (delegation_id) REFERENCES delegations(delegation_id) ON DELETE CASCADE
        );
        """
    )
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(delegations)").fetchall()}
    if "dispatch_phase" not in columns:
        db.execute("ALTER TABLE delegations ADD COLUMN dispatch_phase TEXT NOT NULL DEFAULT 'dispatched'")
    db.commit()


def _bounded(value: Any, maximum: int = 256) -> str:
    text = op.redact_output(str(value or "")).strip()
    return text if len(text) <= maximum else text[: maximum - 3] + "..."


def _new_id(contract_sha256: str, task_id: str) -> str:
    seed = f"{contract_sha256}\0{task_id}\0{_now()}".encode()
    return f"dlg-{hashlib.sha256(seed).hexdigest()[:20]}"


def _normalize_state(value: Any) -> str:
    state = str(value or "").strip().lower().replace("-", "_")
    if state in {"queued", "accepted", "pending", "created", "submitted"}:
        return "queued"
    if state in {"running", "active", "in_progress", "started"}:
        return "running"
    if state in {"completed", "complete", "succeeded", "success", "done", "satisfied"}:
        return "succeeded"
    if state in {"failed", "failure", "error", "errored", "not_satisfied"}:
        return "failed"
    if state in {"cancelled", "canceled", "cancel_requested"}:
        return "cancelled" if state != "cancel_requested" else "running"
    if state in {"reconciling", "ambiguous", "unknown", "unavailable"}:
        return "reconciling"
    return "reconciling"


def _backend_ref(payload: dict[str, Any]) -> dict[str, str]:
    allowed = (
        "job_id",
        "dispatch_id",
        "attempt_id",
        "a2a_task_id",
        "node",
        "selected_node",
        "remote_backend",
    )
    out: dict[str, str] = {}
    for key in allowed:
        value = payload.get(key)
        if value is not None and str(value).strip():
            out[key] = _bounded(value, 192)
    return out


def _surface(row: sqlite3.Row | dict[str, Any], *, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    value = dict(row)
    try:
        backend_ref = json.loads(value.pop("backend_ref_json", "{}") or "{}")
    except ValueError:
        backend_ref = {}
    value["backend_ref"] = backend_ref if isinstance(backend_ref, dict) else {}
    value["cancel_requested"] = bool(value.get("cancel_requested"))
    if events is not None:
        value["events"] = events
    return value


def _event(
    db: sqlite3.Connection,
    delegation_id: str,
    event_type: str,
    *,
    from_state: str = "",
    to_state: str = "",
    backend_state: str = "",
    observed: dict[str, Any] | None = None,
) -> None:
    observed_sha = ""
    if observed:
        encoded = json.dumps(observed, sort_keys=True, default=str, separators=(",", ":"))
        observed_sha = hashlib.sha256(encoded.encode()).hexdigest()
    db.execute(
        "INSERT INTO delegation_events(delegation_id,event_type,from_state,to_state,backend_state,observed_sha256,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (delegation_id, event_type, from_state, to_state, _bounded(backend_state, 128), observed_sha, _now()),
    )


def _live_event(kind: str, row: dict[str, Any], *, hermes_root: Path | None = None) -> None:
    try:
        import operator_live_events as live_events

        live_events.publish_event(
            topic="delegation",
            kind=kind,
            subject_type="delegation",
            subject_id=str(row["delegation_id"]),
            mission_id=str(row.get("mission_id") or ""),
            source="delegation-runtime",
            payload={
                "task_id": row.get("task_id"),
                "backend": row.get("backend"),
                "state": row.get("state"),
                "backend_state": row.get("backend_state"),
                "validation_verdict": row.get("validation_verdict"),
            },
            hermes_root=_root(hermes_root),
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return


def _audit(
    tool: str,
    policy: op.OperatorPolicy,
    *,
    dry_run: bool,
    success: bool,
    changed: bool,
    delegation_id: str = "",
    task_id: str = "",
    backend: str = "",
) -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=f"delegation lifecycle {tool}",
            extra={
                "delegation_id": delegation_id,
                "task_id": task_id,
                "backend": backend,
            },
        )
    except (OSError, TypeError, ValueError):
        return


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(op.error_from_exception(exc, layer="operator", code=code, suggested_action=action))


def _get_row(db: sqlite3.Connection, delegation_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
    if row is None:
        raise LookupError(f"delegation {delegation_id!r} was not found")
    return row


def _ensure_mission(mission_id: str, hermes_root: Path | None) -> None:
    if not mission_id:
        return
    payload = json.loads(mission_runtime.hermes_mission_get(mission_id, hermes_root=hermes_root))
    if (
        not payload.get("success")
        or payload.get("found") is False
        or payload.get("mission_id") != mission_id
        or "status" not in payload
    ):
        raise LookupError(f"mission {mission_id!r} was not found")


def _mission_state(state: str) -> str:
    if state in {"reserved", "queued", "reconciling"}:
        return "pending" if state in {"reserved", "queued"} else "blocked"
    return state


def _sync_mission_attachment(
    row: dict[str, Any],
    state: str,
    *,
    evidence_ref: str,
    verified: bool = False,
    hermes_root: Path | None = None,
) -> bool:
    mission_id = str(row.get("mission_id") or "")
    if not mission_id:
        return True
    return mission_runtime.record_attachment_state(
        mission_id,
        "delegation",
        str(row["delegation_id"]),
        state,
        evidence_ref=evidence_ref,
        verified=verified,
        hermes_root=_root(hermes_root),
    )


def _mission_sync_failure(
    operation: str,
    row: dict[str, Any],
    *,
    changed: bool,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "success": False,
        "schema_version": SCHEMA_VERSION,
        "code": "DELEGATION_MISSION_SYNC_FAILED",
        "safe_message": f"Delegation {operation} committed but Mission linkage could not be synchronized.",
        "changed": changed,
        "delegation": _surface(row),
        "suggested_action": "Reconcile the Delegation and Mission before allowing Mission completion.",
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _latest_observation(task_id: str, hermes_root: Path) -> dict[str, Any] | None:
    runs = contract_mod._observed_runs(task_id, hermes_root)
    if not runs:
        return None

    def key(run: dict[str, Any]) -> tuple[str, str, str]:
        latest = max(
            str(run.get("ended_at") or run.get("completed_at") or ""),
            str(run.get("started_at") or run.get("dispatched_at") or ""),
            str(run.get("created_at") or run.get("updated_at") or ""),
        )
        return (
            latest,
            str(run.get("ended_at") or run.get("completed_at") or ""),
            str(run.get("status") or run.get("state") or ""),
        )

    return max(runs, key=key)


def _manifest_row(db: sqlite3.Connection, delegation_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT schema,context_sha256,manifest_json,created_at,updated_at FROM delegation_validation_manifests WHERE delegation_id=?",
        (delegation_id,),
    ).fetchone()
    if row is None:
        raise LookupError("delegation validation manifest is missing")
    try:
        manifest = json.loads(row["manifest_json"])
    except (TypeError, ValueError) as exc:
        raise ValueError("delegation validation manifest is corrupt") from exc
    if manifest.get("schema") != row["schema"] or manifest.get("context_sha256") != row["context_sha256"]:
        raise ValueError("delegation validation manifest metadata mismatch")
    return manifest


def _mission_dispatch_guard(mission_id: str, delegation_id: str, contract_sha: str, root: Path) -> None:
    if not mission_id:
        return
    payload = json.loads(mission_runtime.hermes_mission_get(mission_id, hermes_root=root))
    if not payload.get("success") or payload.get("status") in mission_runtime.TERMINAL_STATUSES:
        raise RuntimeError("Mission is not dispatchable")
    expected = f"contract:{contract_sha}"
    attached = next((a for a in payload.get("attachments", []) if a.get("kind") == "delegation" and a.get("ref") == delegation_id), None)
    if not attached or attached.get("evidence_ref") != expected or attached.get("state") != "pending":
        raise RuntimeError("Mission delegation reservation is not current")


def hermes_delegation_dispatch(
    contract_json: str,
    mission_id: str = "",
    delegation_id: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
    hermes_root: Path | None = None,
) -> str:
    """Dispatch a Work Contract and create one normalized delegation record."""
    policy = op.OperatorPolicy()
    invocation_claimed = False
    backend_called = False
    contract_sha = ""
    root: Path | None = None
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        canonical, contract, contract_sha = contract_mod._parse_contract(contract_json)
        manifest = contract_mod._validation_manifest(contract, contract_sha)
        task_id = contract["task_id"]
        backend = runners.selected_backend(contract)
        delegation_id = delegation_id.strip() or _new_id(contract_sha, task_id)
        if not DELEGATION_ID_RE.fullmatch(delegation_id):
            raise ValueError("delegation_id must match dlg-<bounded-id>")
        _ensure_mission(mission_id, hermes_root)
        root = _root(hermes_root)
        path = _db_path(hermes_root)
        effective_dry = policy.effective_dry_run(dry_run)
        if effective_dry:
            dispatch = json.loads(contract_mod.hermes_contract_dispatch(
                canonical, confirm=confirm, dry_run=True, timeout=timeout, hermes_root=root,
            ))
            _audit(tool="hermes_delegation_dispatch", policy=policy, dry_run=True, success=bool(dispatch.get("success")), changed=False, delegation_id=delegation_id, task_id=task_id, backend=backend)
            return json.dumps({
                "success": bool(dispatch.get("success")),
                "schema_version": SCHEMA_VERSION,
                "delegation_id": delegation_id,
                "mission_id": mission_id,
                "task_id": task_id,
                "contract_sha256": contract_sha,
                "backend": backend,
                "dry_run": True,
                "changed": False,
                "dispatch": dispatch,
            }, ensure_ascii=False, indent=2)

        now = _now()
        with _connect(path, write=True) as db:
            _init(db)
            db.execute("BEGIN IMMEDIATE")
            collisions = db.execute(
                "SELECT * FROM delegations WHERE delegation_id=? OR task_id=?",
                (delegation_id, task_id),
            ).fetchall()
            if collisions:
                if len(collisions) != 1:
                    raise ValueError("delegation_id/task_id collision has conflicting lineage")
                existing = dict(collisions[0])
                exact = (
                    existing["delegation_id"] == delegation_id and existing["task_id"] == task_id
                    and existing["contract_sha256"] == contract_sha and existing["mission_id"] == mission_id
                    and existing["backend"] == backend
                )
                if not exact:
                    raise ValueError("delegation_id/task_id already belongs to different lineage")
                stored_manifest = _manifest_row(db, delegation_id)
                if stored_manifest["context_sha256"] != manifest["context_sha256"]:
                    raise ValueError("delegation validation lineage mismatch")
                if existing["dispatch_phase"] != "reserved":
                    db.commit()
                    return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "changed": False, "idempotent": True, "delegation": _surface(existing)}, ensure_ascii=False, indent=2)
            else:
                db.execute(
                    "INSERT INTO delegations(delegation_id,schema,mission_id,task_id,contract_sha256,backend,state,backend_state,outcome,backend_ref_json,validation_verdict,cancel_requested,dispatch_phase,created_at,dispatched_at,updated_at,terminal_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (delegation_id, DELEGATION_SCHEMA, mission_id, task_id, contract_sha, backend, "reserved", "", "", "{}", "", 0, "reserved", now, "", now, None),
                )
                db.execute(
                    "INSERT INTO delegation_validation_manifests(delegation_id,schema,context_sha256,manifest_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (delegation_id, manifest["schema"], manifest["context_sha256"], json.dumps(manifest, sort_keys=True, separators=(",", ":")), now, now),
                )
                _event(db, delegation_id, "delegation.reserved", to_state="reserved")
            db.commit()

        if mission_id and not mission_runtime.reserve_delegation_attachment(
            mission_id, delegation_id, evidence_ref=f"contract:{contract_sha}", hermes_root=root,
        ):
            raise RuntimeError("Mission delegation reservation was rejected")

        with _connect(path, write=True) as db:
            _init(db)
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE delegations SET dispatch_phase='invoking',updated_at=? WHERE delegation_id=? AND dispatch_phase='reserved'",
                (_now(), delegation_id),
            ).rowcount
            if changed != 1:
                row = dict(_get_row(db, delegation_id))
                db.commit()
                return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "changed": False, "idempotent": True, "delegation": _surface(row)}, ensure_ascii=False, indent=2)
            _event(db, delegation_id, "delegation.invoking", from_state="reserved", to_state="reserved")
            db.commit()
        invocation_claimed = True

        try:
            _mission_dispatch_guard(mission_id, delegation_id, contract_sha, root)
        except Exception:
            with _connect(path, write=True) as db:
                _init(db)
                db.execute("UPDATE delegations SET dispatch_phase='reserved',updated_at=? WHERE delegation_id=? AND dispatch_phase='invoking'", (_now(), delegation_id))
                db.commit()
            invocation_claimed = False
            raise

        backend_called = True
        dispatch = json.loads(contract_mod.hermes_contract_dispatch(
            canonical, confirm=confirm, dry_run=False, timeout=timeout, hermes_root=root,
        ))
        ambiguous = bool(dispatch.get("submission_may_have_succeeded") or (dispatch.get("changed") and not dispatch.get("success")))
        if not dispatch.get("success") and not ambiguous:
            with _connect(path, write=True) as db:
                _init(db)
                db.execute("UPDATE delegations SET dispatch_phase='reserved',updated_at=? WHERE delegation_id=? AND dispatch_phase='invoking'", (_now(), delegation_id))
                _event(db, delegation_id, "delegation.dispatch_rejected", from_state="reserved", to_state="reserved")
                db.commit()
            invocation_claimed = False
            return json.dumps({"success": False, "schema_version": SCHEMA_VERSION, "delegation_id": delegation_id, "task_id": task_id, "backend": backend, "changed": False, "dispatch": dispatch}, ensure_ascii=False, indent=2)

        now = _now()
        backend_state = str(dispatch.get("state") or dispatch.get("status") or ("ambiguous" if ambiguous else "queued"))
        state = "reconciling" if ambiguous else _normalize_state(backend_state)
        if state == "succeeded":
            # Dispatch self-report is never completion proof. Reconciliation must
            # validate the matching Work Contract before success is durable.
            state = "reconciling"
        if state == "reconciling" and not ambiguous and not backend_state:
            state = "queued"
        with _connect(path, write=True) as db:
            _init(db)
            phase = "invoking" if ambiguous else "dispatched"
            db.execute("UPDATE delegations SET state=?,backend_state=?,backend_ref_json=?,dispatch_phase=?,dispatched_at=?,updated_at=? WHERE delegation_id=? AND dispatch_phase='invoking'", (state, _bounded(backend_state, 128), json.dumps(_backend_ref(dispatch), sort_keys=True), phase, now, now, delegation_id))
            _event(db, delegation_id, "delegation.dispatched", to_state=state, backend_state=backend_state)
            db.commit()
            row = dict(_get_row(db, delegation_id))
        invocation_claimed = False
        mission_linked = True
        if mission_id:
            mission_linked = _sync_mission_attachment(
                row,
                _mission_state(state),
                evidence_ref=f"contract:{contract_sha}",
                hermes_root=root,
            )
        _live_event("delegation.dispatched", row, hermes_root=root)
        if not mission_linked:
            _audit(tool="hermes_delegation_dispatch", policy=policy, dry_run=False, success=False, changed=True, delegation_id=delegation_id, task_id=task_id, backend=backend)
            return _mission_sync_failure(
                "dispatch",
                row,
                changed=True,
                extra={"dispatch": dispatch, "submission_may_have_succeeded": True},
            )
        _audit(tool="hermes_delegation_dispatch", policy=policy, dry_run=False, success=True, changed=True, delegation_id=delegation_id, task_id=task_id, backend=backend)
        return json.dumps({
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "changed": True,
            "delegation": _surface(row),
            "mission_linked": bool(mission_id),
            "dispatch": dispatch,
        }, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, LookupError, PermissionError, RuntimeError, OSError, sqlite3.Error) as exc:
        if invocation_claimed and backend_called and root is not None:
            try:
                with _connect(_db_path(root), write=True) as db:
                    _init(db)
                    db.execute("UPDATE delegations SET state='reconciling',backend_state='ambiguous',updated_at=? WHERE delegation_id=? AND dispatch_phase='invoking'", (_now(), delegation_id))
                    _event(db, delegation_id, "delegation.dispatch_ambiguous", from_state="reserved", to_state="reconciling", backend_state="ambiguous")
                    db.commit()
            except (OSError, sqlite3.Error):
                pass
        envelope = _error(exc, "DELEGATION_DISPATCH_REJECTED", "Check Work Contract, Mission linkage, runner availability, and mutation policy.")
        if invocation_claimed and backend_called:
            # Backend already accepted the submission; the failure was local
            # persistence, not dispatch rejection. Surface the ambiguity so
            # callers reconcile instead of treating this as a safe no-op.
            payload = json.loads(envelope)
            payload["submission_may_have_succeeded"] = True
            payload["changed"] = True
            payload["suggested_action"] = (
                "Backend invocation outcome is ambiguous. Reconcile the durable delegation; it will not be redispatched."
            )
            envelope = json.dumps(payload, ensure_ascii=False, indent=2)
        return envelope


def hermes_delegation_get(delegation_id: str, hermes_root: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if not DELEGATION_ID_RE.fullmatch(delegation_id or ""):
            raise ValueError("delegation_id is invalid")
        path = _db_path(hermes_root)
        if not path.is_file():
            raise LookupError(f"delegation {delegation_id!r} was not found")
        with _connect(path, write=False) as db:
            row = _get_row(db, delegation_id)
            events = [dict(r) for r in db.execute(
                "SELECT event_type,from_state,to_state,backend_state,observed_sha256,created_at FROM delegation_events WHERE delegation_id=? ORDER BY seq DESC LIMIT 100",
                (delegation_id,),
            ).fetchall()]
        _audit(tool="hermes_delegation_get", policy=policy, dry_run=True, success=True, changed=False, delegation_id=delegation_id, task_id=row["task_id"], backend=row["backend"])
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "delegation": _surface(row, events=events)}, ensure_ascii=False, indent=2)
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "DELEGATION_GET_FAILED", "Check delegation id and Operator read access.")


def hermes_delegation_list(
    mission_id: str = "",
    state: str = "",
    limit: int = 50,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if state and state not in STATES:
            raise ValueError("state filter is invalid")
        limit = max(1, min(int(limit), MAX_LIST))
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "delegations": [], "count": 0})
        clauses: list[str] = []
        params: list[Any] = []
        if mission_id:
            clauses.append("mission_id=?")
            params.append(mission_id)
        if state:
            clauses.append("state=?")
            params.append(state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with _connect(path, write=False) as db:
            rows = db.execute(
                f"SELECT * FROM delegations{where} ORDER BY updated_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "delegations": [_surface(r) for r in rows], "count": len(rows)}, ensure_ascii=False, indent=2)
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "DELEGATION_LIST_FAILED", "Check filters and Operator read access.")


def hermes_delegation_reconcile(
    delegation_id: str,
    contract_json: str = "",
    apply: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """Derive normalized state from authoritative runner/Fabric observations.

    Backend terminal success is necessary but never sufficient. A delegation can
    become ``succeeded`` only when its matching Work Contract is currently
    ``SATISFIED`` (or a previously persisted SATISFIED verdict exists for the
    same immutable contract digest). Missing/unreadable/UNVERIFIED evidence
    therefore remains fail-closed as ``reconciling``.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if apply:
            policy.require_level("workspace")
            policy.require_mutation(False)
        if not DELEGATION_ID_RE.fullmatch(delegation_id or ""):
            raise ValueError("delegation_id is invalid")
        root = _root(hermes_root)
        path = _db_path(hermes_root)
        with _connect(path, write=False) as db:
            stored = dict(_get_row(db, delegation_id))
            manifest = _manifest_row(db, delegation_id)

        if contract_json:
            _canonical, contract, sha = contract_mod._parse_contract(contract_json)
            supplied = contract_mod._validation_manifest(contract, sha)
            if sha != stored["contract_sha256"] or contract["task_id"] != stored["task_id"] or supplied["context_sha256"] != manifest["context_sha256"]:
                raise ValueError("contract_json does not match delegation validation lineage")
        manifest_contract, manifest_sha = contract_mod._contract_from_validation_manifest(manifest)
        if manifest_sha != stored["contract_sha256"] or manifest_contract["task_id"] != stored["task_id"]:
            raise ValueError("delegation validation manifest does not match lineage")

        observed = _latest_observation(stored["task_id"], root)
        if observed is None:
            observed_desired = "reconciling" if stored["state"] not in TERMINAL_STATES else stored["state"]
            backend_state = "unobserved"
            outcome = stored.get("outcome") or ""
        else:
            backend_state = str(observed.get("status") or observed.get("state") or observed.get("outcome") or "unknown")
            outcome = str(observed.get("outcome") or observed.get("state") or backend_state)
            observed_desired = _normalize_state(outcome or backend_state)
            if observed.get("error"):
                observed_desired = "failed"

        validation = contract_mod._validate_manifest_impl(manifest, None, root)
        verdict = str(validation.get("verdict") or "")

        desired = observed_desired
        if observed is None or (observed_desired == "succeeded" and verdict != "SATISFIED"):
            desired = "reconciling"
        verified_success = desired == "succeeded" and verdict == "SATISFIED"

        changed = (
            desired != stored["state"]
            or _bounded(backend_state, 128) != stored["backend_state"]
            or verdict != stored.get("validation_verdict", "")
        )
        preview = dict(stored)
        preview.update({
            "state": desired,
            "backend_state": _bounded(backend_state, 128),
            "outcome": _bounded(outcome, 128),
            "validation_verdict": _bounded(verdict, 64),
        })
        if not apply:
            return json.dumps({
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "changed": changed,
                "applied": False,
                "delegation": _surface(preview),
                "observed": observed,
                "evidence_ref": f"contract:{stored['contract_sha256']}" if verified_success else "",
            }, ensure_ascii=False, indent=2)

        now = _now()
        terminal_at = stored.get("terminal_at") or (now if desired in TERMINAL_STATES else None)
        if desired not in TERMINAL_STATES:
            terminal_at = None
        with _connect(path, write=True) as db:
            _init(db)
            db.execute(
                "UPDATE delegations SET state=?,backend_state=?,outcome=?,validation_verdict=?,updated_at=?,terminal_at=? WHERE delegation_id=?",
                (desired, _bounded(backend_state, 128), _bounded(outcome, 128), _bounded(verdict, 64), now, terminal_at, delegation_id),
            )
            if changed:
                _event(db, delegation_id, "delegation.reconciled", from_state=stored["state"], to_state=desired, backend_state=backend_state, observed=observed)
            db.commit()
            row = dict(_get_row(db, delegation_id))

        mission_synced = True
        if row.get("mission_id"):
            evidence_ref = f"contract:{row['contract_sha256']}" if verified_success else f"delegation:{delegation_id}"
            mission_synced = _sync_mission_attachment(
                row,
                _mission_state(desired),
                evidence_ref=evidence_ref,
                verified=verified_success,
                hermes_root=root,
            )
        if changed:
            _live_event("delegation.reconciled", row, hermes_root=root)
        if not mission_synced:
            _audit(tool="hermes_delegation_reconcile", policy=policy, dry_run=False, success=False, changed=changed, delegation_id=delegation_id, task_id=row["task_id"], backend=row["backend"])
            return _mission_sync_failure("reconciliation", row, changed=changed, extra={"applied": True, "observed": observed})
        _audit(tool="hermes_delegation_reconcile", policy=policy, dry_run=False, success=True, changed=changed, delegation_id=delegation_id, task_id=row["task_id"], backend=row["backend"])
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "changed": changed, "applied": True, "delegation": _surface(row), "observed": observed, "evidence_ref": f"contract:{row['contract_sha256']}" if verified_success else ""}, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "DELEGATION_RECONCILE_FAILED", "Check delegation lineage, observed backend state, and mutation policy.")


def hermes_delegation_cancel(
    delegation_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        root = _root(hermes_root)
        path = _db_path(hermes_root)
        with _connect(path, write=False) as db:
            stored = dict(_get_row(db, delegation_id))
        if stored["state"] in TERMINAL_STATES:
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "changed": False, "delegation": _surface(stored)}, ensure_ascii=False, indent=2)
        if stored.get("dispatch_phase") == "reserved":
            if dry_run or policy.effective_dry_run(dry_run):
                return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "changed": False, "dry_run": True, "delegation_id": delegation_id}, ensure_ascii=False, indent=2)
            now = _now()
            with _connect(path, write=True) as db:
                _init(db)
                db.execute("UPDATE delegations SET state='cancelled',backend_state='not_invoked',outcome='cancelled',cancel_requested=1,updated_at=?,terminal_at=? WHERE delegation_id=? AND dispatch_phase='reserved'", (now, now, delegation_id))
                _event(db, delegation_id, "delegation.cancelled", from_state="reserved", to_state="cancelled", backend_state="not_invoked")
                db.commit()
                row = dict(_get_row(db, delegation_id))
            if row.get("mission_id"):
                _sync_mission_attachment(row, "cancelled", evidence_ref=f"delegation:{delegation_id}", hermes_root=root)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "changed": True, "delegation": _surface(row)}, ensure_ascii=False, indent=2)
        result = json.loads(runners.hermes_runner_cancel(
            stored["task_id"],
            backend=stored["backend"],
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=root,
        ))
        if dry_run or policy.effective_dry_run(dry_run):
            return json.dumps({"success": bool(result.get("success")), "schema_version": SCHEMA_VERSION, "changed": False, "dry_run": True, "delegation_id": delegation_id, "cancel": result}, ensure_ascii=False, indent=2)
        if not result.get("success"):
            return json.dumps({"success": False, "schema_version": SCHEMA_VERSION, "changed": False, "delegation_id": delegation_id, "cancel": result}, ensure_ascii=False, indent=2)
        now = _now()
        backend_state = str(result.get("state") or "cancelled")
        normalized = _normalize_state(backend_state)
        desired = normalized if normalized in {"cancelled", "failed"} else "reconciling"
        outcome = desired if desired in TERMINAL_STATES else ""
        event_type = "delegation.cancelled" if desired == "cancelled" else "delegation.cancel_requested"
        with _connect(path, write=True) as db:
            _init(db)
            db.execute(
                "UPDATE delegations SET state=?,backend_state=?,outcome=?,cancel_requested=1,updated_at=?,terminal_at=? WHERE delegation_id=?",
                (desired, _bounded(backend_state, 128), outcome, now, now if desired in TERMINAL_STATES else None, delegation_id),
            )
            _event(db, delegation_id, event_type, from_state=stored["state"], to_state=desired, backend_state=backend_state)
            db.commit()
            row = dict(_get_row(db, delegation_id))
        mission_synced = True
        if row.get("mission_id"):
            mission_synced = _sync_mission_attachment(
                row,
                _mission_state(desired),
                evidence_ref=f"delegation:{delegation_id}",
                hermes_root=root,
            )
        _live_event(event_type, row, hermes_root=root)
        if not mission_synced:
            _audit(tool="hermes_delegation_cancel", policy=policy, dry_run=False, success=False, changed=True, delegation_id=delegation_id, task_id=row["task_id"], backend=row["backend"])
            return _mission_sync_failure("cancellation", row, changed=True, extra={"cancel": result})
        _audit(tool="hermes_delegation_cancel", policy=policy, dry_run=False, success=True, changed=True, delegation_id=delegation_id, task_id=row["task_id"], backend=row["backend"])
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "changed": True, "delegation": _surface(row), "cancel": result}, ensure_ascii=False, indent=2)
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "DELEGATION_CANCEL_FAILED", "Check delegation state, backend cancellation support, and mutation policy.")


__all__ = [
    "DELEGATION_SCHEMA",
    "SCHEMA_VERSION",
    "STATES",
    "hermes_delegation_cancel",
    "hermes_delegation_dispatch",
    "hermes_delegation_get",
    "hermes_delegation_list",
    "hermes_delegation_reconcile",
]
