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

SCHEMA_VERSION = "0.9-delegation.1"
DELEGATION_SCHEMA = "hermes.delegation/v1"
DELEGATION_ID_RE = re.compile(r"^dlg-[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
STATES = frozenset({"queued", "running", "reconciling", "succeeded", "failed", "cancelled"})
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
        """
    )
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
    if not payload.get("success"):
        raise LookupError(f"mission {mission_id!r} was not found")


def _mission_state(state: str) -> str:
    if state in {"queued", "reconciling"}:
        return "pending" if state == "queued" else "blocked"
    return state


def _latest_observation(task_id: str, hermes_root: Path) -> dict[str, Any] | None:
    runs = contract_mod._observed_runs(task_id, hermes_root)
    if not runs:
        return None

    def key(run: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(run.get("ended_at") or run.get("completed_at") or ""),
            str(run.get("started_at") or run.get("dispatched_at") or ""),
            str(run.get("status") or run.get("state") or ""),
        )

    return max(runs, key=key)


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
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        canonical, contract, contract_sha = contract_mod._parse_contract(contract_json)
        task_id = contract["task_id"]
        backend = runners.selected_backend(contract)
        delegation_id = delegation_id.strip() or _new_id(contract_sha, task_id)
        if not DELEGATION_ID_RE.fullmatch(delegation_id):
            raise ValueError("delegation_id must match dlg-<bounded-id>")
        _ensure_mission(mission_id, hermes_root)
        root = _root(hermes_root)
        path = _db_path(hermes_root)
        effective_dry = policy.effective_dry_run(dry_run)
        if path.is_file():
            with _connect(path, write=False) as db:
                existing = db.execute(
                    "SELECT delegation_id FROM delegations WHERE delegation_id=? OR task_id=?",
                    (delegation_id, task_id),
                ).fetchone()
                if existing is not None:
                    raise ValueError("delegation_id/task_id already has a lifecycle record")
        dispatch = json.loads(
            contract_mod.hermes_contract_dispatch(
                canonical,
                confirm=confirm,
                dry_run=effective_dry,
                timeout=timeout,
                hermes_root=root,
            )
        )
        ambiguous = bool(dispatch.get("submission_may_have_succeeded") or (dispatch.get("changed") and not dispatch.get("success")))
        if effective_dry:
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
        if not dispatch.get("success") and not ambiguous:
            _audit(tool="hermes_delegation_dispatch", policy=policy, dry_run=False, success=False, changed=False, delegation_id=delegation_id, task_id=task_id, backend=backend)
            return json.dumps({
                "success": False,
                "schema_version": SCHEMA_VERSION,
                "delegation_id": delegation_id,
                "task_id": task_id,
                "backend": backend,
                "changed": False,
                "dispatch": dispatch,
            }, ensure_ascii=False, indent=2)
        now = _now()
        backend_state = str(dispatch.get("state") or dispatch.get("status") or ("ambiguous" if ambiguous else "queued"))
        state = "reconciling" if ambiguous else _normalize_state(backend_state)
        if state == "reconciling" and not ambiguous and not backend_state:
            state = "queued"
        with _connect(path, write=True) as db:
            _init(db)
            db.execute(
                "INSERT INTO delegations(delegation_id,schema,mission_id,task_id,contract_sha256,backend,state,backend_state,outcome,backend_ref_json,created_at,dispatched_at,updated_at,terminal_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    delegation_id,
                    DELEGATION_SCHEMA,
                    mission_id,
                    task_id,
                    contract_sha,
                    backend,
                    state,
                    _bounded(backend_state, 128),
                    "",
                    json.dumps(_backend_ref(dispatch), sort_keys=True),
                    now,
                    now,
                    now,
                    now if state in TERMINAL_STATES else None,
                ),
            )
            _event(db, delegation_id, "delegation.dispatched", to_state=state, backend_state=backend_state)
            db.commit()
            row = dict(_get_row(db, delegation_id))
        mission_linked = False
        if mission_id:
            linked = json.loads(mission_runtime.hermes_mission_attach(
                mission_id,
                "delegation",
                delegation_id,
                relationship="contains",
                state=_mission_state(state),
                evidence_ref=f"contract:{contract_sha}",
                confirm=True,
                dry_run=False,
                hermes_root=root,
            ))
            mission_linked = bool(linked.get("success"))
        _live_event("delegation.dispatched", row)
        _audit(tool="hermes_delegation_dispatch", policy=policy, dry_run=False, success=True, changed=True, delegation_id=delegation_id, task_id=task_id, backend=backend)
        return json.dumps({
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "changed": True,
            "delegation": _surface(row),
            "mission_linked": mission_linked,
            "dispatch": dispatch,
        }, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "DELEGATION_DISPATCH_REJECTED", "Check Work Contract, Mission linkage, runner availability, and mutation policy.")


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
    """Derive normalized state from authoritative runner/Fabric observations."""
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
        observed = _latest_observation(stored["task_id"], root)
        if observed is None:
            desired = "reconciling" if stored["state"] not in TERMINAL_STATES else stored["state"]
            backend_state = "unobserved"
            outcome = stored.get("outcome") or ""
        else:
            backend_state = str(observed.get("status") or observed.get("state") or observed.get("outcome") or "unknown")
            outcome = str(observed.get("outcome") or observed.get("state") or backend_state)
            desired = _normalize_state(outcome or backend_state)
            if observed.get("error"):
                desired = "failed"
        verdict = str(stored.get("validation_verdict") or "")
        if contract_json:
            canonical, contract, sha = contract_mod._parse_contract(contract_json)
            if sha != stored["contract_sha256"] or contract["task_id"] != stored["task_id"]:
                raise ValueError("contract_json does not match delegation lineage")
            validation = json.loads(contract_mod.hermes_contract_validate(canonical, hermes_root=root))
            verdict = str(validation.get("verdict") or "")
        changed = desired != stored["state"] or _bounded(backend_state, 128) != stored["backend_state"] or verdict != stored.get("validation_verdict", "")
        preview = dict(stored)
        preview.update({"state": desired, "backend_state": _bounded(backend_state, 128), "outcome": _bounded(outcome, 128), "validation_verdict": _bounded(verdict, 64)})
        if not apply:
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "changed": changed, "applied": False, "delegation": _surface(preview), "observed": observed}, ensure_ascii=False, indent=2)
        now = _now()
        terminal_at = stored.get("terminal_at") or (now if desired in TERMINAL_STATES else None)
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
        if row.get("mission_id"):
            mission_runtime.record_attachment_state(row["mission_id"], "delegation", delegation_id, _mission_state(desired), evidence_ref=f"delegation:{delegation_id}", hermes_root=root)
        if changed:
            _live_event("delegation.reconciled", row)
        _audit(tool="hermes_delegation_reconcile", policy=policy, dry_run=False, success=True, changed=changed, delegation_id=delegation_id, task_id=row["task_id"], backend=row["backend"])
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "changed": changed, "applied": True, "delegation": _surface(row), "observed": observed}, ensure_ascii=False, indent=2)
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
        path = _db_path(hermes_root)
        with _connect(path, write=False) as db:
            stored = dict(_get_row(db, delegation_id))
        if stored["state"] in TERMINAL_STATES:
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "changed": False, "delegation": _surface(stored)}, ensure_ascii=False, indent=2)
        result = json.loads(runners.hermes_runner_cancel(
            stored["task_id"],
            backend=stored["backend"],
            confirm=confirm,
            dry_run=dry_run,
            hermes_root=_root(hermes_root),
        ))
        if dry_run or policy.effective_dry_run(dry_run):
            return json.dumps({"success": bool(result.get("success")), "schema_version": SCHEMA_VERSION, "changed": False, "dry_run": True, "delegation_id": delegation_id, "cancel": result}, ensure_ascii=False, indent=2)
        if not result.get("success"):
            return json.dumps({"success": False, "schema_version": SCHEMA_VERSION, "changed": False, "delegation_id": delegation_id, "cancel": result}, ensure_ascii=False, indent=2)
        now = _now()
        desired = _normalize_state(result.get("state") or "cancelled")
        with _connect(path, write=True) as db:
            _init(db)
            db.execute(
                "UPDATE delegations SET state=?,backend_state=?,outcome=?,cancel_requested=1,updated_at=?,terminal_at=? WHERE delegation_id=?",
                (desired, _bounded(result.get("state") or "cancelled", 128), desired, now, now if desired in TERMINAL_STATES else None, delegation_id),
            )
            _event(db, delegation_id, "delegation.cancelled", from_state=stored["state"], to_state=desired, backend_state=str(result.get("state") or "cancelled"))
            db.commit()
            row = dict(_get_row(db, delegation_id))
        if row.get("mission_id"):
            mission_runtime.record_attachment_state(row["mission_id"], "delegation", delegation_id, _mission_state(desired), evidence_ref=f"delegation:{delegation_id}", hermes_root=_root(hermes_root))
        _live_event("delegation.cancelled", row)
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
