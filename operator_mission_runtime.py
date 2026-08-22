"""First-class durable Mission runtime for Hermes GPT v0.9.

A Mission is the durable parent object for coordinated work.  It does not
replace Work Contracts, Swarms, Fabric, or delegation runners; it binds those
objects together under one bounded lifecycle while preserving their existing
authority/evidence semantics.

Security properties:
- reads require Operator read_only; mutations require workspace + direct + confirm;
- final approval is Owner-gated when ``final_approval_required`` is true;
- context is reference-only and bounded (no fetched source bodies are persisted);
- skills are explicit bounded manifests;
- child evidence is referenced, never copied;
- restart reconciliation is fail-closed and never fabricates child success.
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

import operator_policy as op

SCHEMA_VERSION = "0.9-mission.2"
MISSION_SPEC_SCHEMA = "hermes.mission-spec/v1"
MISSION_SCHEMA = "hermes.mission/v1"
MISSION_EVENT_SCHEMA = "hermes.mission-event/v1"

MISSION_ID_RE = re.compile(r"^msn-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
WORKFLOW_REF_RE = re.compile(r"^sw-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

STATUSES = (
    "draft",
    "running",
    "paused",
    "blocked",
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
)
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ATTACHMENT_KINDS = {"workflow", "contract", "delegation", "evidence", "artifact"}
ATTACHMENT_STATES = {"unknown", "pending", "running", "blocked", "succeeded", "failed", "cancelled"}
MISSION_TRANSITIONS = {
    "draft": {"running", "paused", "blocked", "failed", "cancelled"},
    "running": {"paused", "blocked", "failed", "cancelled"},
    "paused": {"running", "blocked", "failed", "cancelled"},
    "blocked": {"running", "paused", "failed", "cancelled"},
    "awaiting_approval": {"running", "blocked", "failed", "cancelled"},
}

MAX_TITLE = 200
MAX_OBJECTIVE = 8_000
MAX_ACCEPTANCE = 32
MAX_ACCEPTANCE_ITEM = 500
MAX_CONTEXT = 64
MAX_SKILLS = 64
MAX_ATTACHMENTS = 512
MAX_LIST = 200

_ALLOWED_SPEC_KEYS = {
    "schema",
    "mission_id",
    "title",
    "objective",
    "owner_profile",
    "acceptance_criteria",
    "context_refs",
    "skills",
    "final_approval_required",
}
_ALLOWED_PATCH_KEYS = {
    "title",
    "objective",
    "owner_profile",
    "acceptance_criteria",
    "context_refs",
    "skills",
}
_ALLOWED_CONTEXT_KEYS = {"kind", "ref", "label", "sha256"}
_ALLOWED_SKILL_KEYS = {"name", "version", "ref", "sha256"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(hermes_root: Path | None) -> Path:
    if hermes_root is not None:
        return Path(hermes_root)
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    return Path.home() / ".hermes"


def _db_path(hermes_root: Path | None) -> Path:
    return _root(hermes_root) / "missions" / "missions.db"


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        _init_db(db)
    else:
        if not path.is_file():
            raise FileNotFoundError(path)
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _init_db(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS missions (
            mission_id TEXT PRIMARY KEY,
            spec_json TEXT NOT NULL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            approval_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attachments (
            mission_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            ref TEXT NOT NULL,
            relationship TEXT NOT NULL,
            state TEXT NOT NULL,
            evidence_ref TEXT NOT NULL DEFAULT '',
            verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mission_id, kind, ref),
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS mission_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_status TEXT NOT NULL DEFAULT '',
            to_status TEXT NOT NULL DEFAULT '',
            reason_sha256 TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_missions_status ON missions(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_attachments_mission ON attachments(mission_id, kind,state);
        CREATE INDEX IF NOT EXISTS idx_events_mission ON mission_events(mission_id, seq);
        """
    )
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(attachments)").fetchall()}
    if "verified" not in columns:
        db.execute("ALTER TABLE attachments ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
    db.commit()


def _begin_write(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")


def _closed(value: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name} contains unknown fields: {', '.join(sorted(unknown))}")


def _bounded_text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _normalize_acceptance(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ACCEPTANCE:
        raise ValueError(f"acceptance_criteria must be a list with at most {MAX_ACCEPTANCE} items")
    return [_bounded_text(v, "acceptance_criteria item", MAX_ACCEPTANCE_ITEM, required=True) for v in raw]


def _normalize_context(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_CONTEXT:
        raise ValueError(f"context_refs must be a list with at most {MAX_CONTEXT} items")
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("context_refs items must be objects")
        _closed(item, _ALLOWED_CONTEXT_KEYS, "context ref")
        kind = _bounded_text(item.get("kind"), "context kind", 64, required=True)
        ref = _bounded_text(item.get("ref"), "context ref", 256, required=True)
        if not REF_RE.fullmatch(ref):
            raise ValueError("context ref contains unsupported characters")
        normalized = {"kind": kind, "ref": ref}
        label = _bounded_text(item.get("label"), "context label", 160)
        if label:
            normalized["label"] = label
        sha = _bounded_text(item.get("sha256"), "context sha256", 64)
        if sha:
            if not SHA_RE.fullmatch(sha):
                raise ValueError("context sha256 must be lowercase SHA-256")
            normalized["sha256"] = sha
        out.append(normalized)
    return out


def _normalize_skills(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SKILLS:
        raise ValueError(f"skills must be a list with at most {MAX_SKILLS} items")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("skills items must be objects")
        _closed(item, _ALLOWED_SKILL_KEYS, "skill")
        name = _bounded_text(item.get("name"), "skill name", 128, required=True)
        if name in seen:
            raise ValueError(f"duplicate skill {name!r}")
        seen.add(name)
        normalized = {"name": name}
        for key, limit in (("version", 64), ("ref", 256), ("sha256", 64)):
            value = _bounded_text(item.get(key), f"skill {key}", limit)
            if value:
                if key == "ref" and not REF_RE.fullmatch(value):
                    raise ValueError("skill ref contains unsupported characters")
                if key == "sha256" and not SHA_RE.fullmatch(value):
                    raise ValueError("skill sha256 must be lowercase SHA-256")
                normalized[key] = value
        out.append(normalized)
    return out


def _normalize_spec(raw: dict[str, Any], *, mission_id: str | None = None) -> dict[str, Any]:
    _closed(raw, _ALLOWED_SPEC_KEYS, "mission spec")
    schema = raw.get("schema", MISSION_SPEC_SCHEMA)
    if schema != MISSION_SPEC_SCHEMA:
        raise ValueError(f"mission spec schema must be {MISSION_SPEC_SCHEMA!r}")
    mid = mission_id or _bounded_text(raw.get("mission_id"), "mission_id", 68)
    if not mid:
        digest = hashlib.sha256(
            (str(raw.get("title") or "") + "\0" + str(raw.get("objective") or "") + "\0" + _now()).encode()
        ).hexdigest()[:20]
        mid = f"msn-{digest}"
    if not MISSION_ID_RE.fullmatch(mid):
        raise ValueError("mission_id is invalid")
    final_approval = raw.get("final_approval_required", True)
    if not isinstance(final_approval, bool):
        raise TypeError("final_approval_required must be boolean")
    return {
        "schema": MISSION_SPEC_SCHEMA,
        "mission_id": mid,
        "title": _bounded_text(raw.get("title"), "title", MAX_TITLE, required=True),
        "objective": _bounded_text(raw.get("objective"), "objective", MAX_OBJECTIVE, required=True),
        "owner_profile": _bounded_text(raw.get("owner_profile") or "default", "owner_profile", 128, required=True),
        "acceptance_criteria": _normalize_acceptance(raw.get("acceptance_criteria")),
        "context_refs": _normalize_context(raw.get("context_refs")),
        "skills": _normalize_skills(raw.get("skills")),
        "final_approval_required": final_approval,
    }


def _row_to_mission(db: sqlite3.Connection, row: sqlite3.Row, *, include_events: bool = False) -> dict[str, Any]:
    spec = json.loads(row["spec_json"])
    attachments = [
        dict(r)
        for r in db.execute(
            "SELECT kind,ref,relationship,state,evidence_ref,verified,created_at,updated_at "
            "FROM attachments WHERE mission_id=? ORDER BY kind,ref",
            (row["mission_id"],),
        ).fetchall()
    ]
    approval = json.loads(row["approval_json"] or "{}")
    value: dict[str, Any] = {
        "schema": MISSION_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        **spec,
        "status": row["status"],
        "version": int(row["version"]),
        "approval": approval,
        "attachments": attachments,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_events:
        value["events"] = [
            {
                "schema": MISSION_EVENT_SCHEMA,
                **dict(r),
                "details": json.loads(r["details_json"] or "{}"),
            }
            for r in db.execute(
                "SELECT seq,event_type,from_status,to_status,reason_sha256,details_json,created_at "
                "FROM mission_events WHERE mission_id=? ORDER BY seq DESC LIMIT 200",
                (row["mission_id"],),
            ).fetchall()
        ]
    return value


def _get_row(db: sqlite3.Connection, mission_id: str) -> sqlite3.Row:
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    row = db.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
    if row is None:
        raise LookupError(f"mission {mission_id!r} not found")
    return row


def _event(
    db: sqlite3.Connection,
    mission_id: str,
    event_type: str,
    *,
    from_status: str = "",
    to_status: str = "",
    reason: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reason_sha = hashlib.sha256(reason.encode()).hexdigest() if reason else ""
    detail_value = details or {}
    db.execute(
        "INSERT INTO mission_events(mission_id,event_type,from_status,to_status,reason_sha256,details_json,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (mission_id, event_type, from_status, to_status, reason_sha, json.dumps(detail_value, sort_keys=True), _now()),
    )
    return {
        "mission_id": mission_id,
        "event_type": event_type,
        "from_status": from_status,
        "to_status": to_status,
        "reason_sha256": reason_sha,
        "details": detail_value,
    }


def _publish_live_event(notice: dict[str, Any] | None, hermes_root: Path | None) -> None:
    """Publish a wake-up only after the authoritative Mission commit succeeds."""
    if notice is None:
        return
    try:
        import operator_live_events as live_events

        live_events.publish_event(
            topic="mission",
            kind=str(notice["event_type"]),
            subject_type="mission",
            subject_id=str(notice["mission_id"]),
            mission_id=str(notice["mission_id"]),
            source="mission-runtime",
            payload={
                "from_status": notice.get("from_status", ""),
                "to_status": notice.get("to_status", ""),
                "reason_sha256": notice.get("reason_sha256", ""),
                "details": notice.get("details", {}),
            },
            hermes_root=_root(hermes_root),
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return

def _audit(tool: str, policy: op.OperatorPolicy, *, dry_run: bool, success: bool, changed: bool, mission_id: str = "", summary: str = "") -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=summary,
            extra={"mission_id": mission_id},
        )
    except (OSError, TypeError, ValueError):
        return


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(op.error_from_exception(exc, layer="operator", code=code, suggested_action=action))


def hermes_mission_create(
    mission_json: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        raw = json.loads(mission_json)
        if not isinstance(raw, dict):
            raise TypeError("mission_json must contain an object")
        spec = _normalize_spec(raw)
        if not spec["final_approval_required"]:
            policy.require_owner(dry_run)
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct mission creation requires confirm=true")
        plan = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "tool": "hermes_mission_create",
            "mission_id": spec["mission_id"],
            "status": "draft",
            "changed": not effective_dry,
            "dry_run": effective_dry,
        }
        if effective_dry:
            _audit("hermes_mission_create", policy, dry_run=True, success=True, changed=False, mission_id=spec["mission_id"], summary="mission create planned")
            return json.dumps(plan)
        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            _begin_write(db)
            if db.execute("SELECT 1 FROM missions WHERE mission_id=?", (spec["mission_id"],)).fetchone():
                raise ValueError("mission already exists")
            now = _now()
            db.execute(
                "INSERT INTO missions(mission_id,spec_json,status,version,approval_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (spec["mission_id"], json.dumps(spec, sort_keys=True), "draft", 1, "{}", now, now),
            )
            live_notice = _event(db, spec["mission_id"], "mission.created", to_status="draft")
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_create", policy, dry_run=False, success=True, changed=True, mission_id=spec["mission_id"], summary="mission created")
        return json.dumps(plan)
    except (ValueError, TypeError, json.JSONDecodeError, PermissionError, OSError, sqlite3.Error) as exc:
        _audit("hermes_mission_create", policy, dry_run=dry_run, success=False, changed=False, summary="mission create rejected")
        return _error(exc, "MISSION_CREATE_REJECTED", "Check mission schema and Operator workspace/direct policy.")

def hermes_mission_get(mission_id: str, hermes_root: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        with _connect(_db_path(hermes_root), write=False) as db:
            value = _row_to_mission(db, _get_row(db, mission_id), include_events=True)
        _audit("hermes_mission_get", policy, dry_run=True, success=True, changed=False, mission_id=mission_id, summary="mission read")
        return json.dumps({"success": True, **value})
    except FileNotFoundError:
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "mission_id": mission_id, "found": False})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_READ_FAILED", "Check mission id and Operator read access.")


def hermes_mission_list(status: str = "", limit: int = 50, hermes_root: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if status and status not in STATUSES:
            raise ValueError("status filter is invalid")
        limit = max(1, min(int(limit), MAX_LIST))
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "missions": [], "count": 0})
        with _connect(path, write=False) as db:
            if status:
                rows = db.execute("SELECT * FROM missions WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM missions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            missions = [_row_to_mission(db, row) for row in rows]
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "missions": missions, "count": len(missions)})
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_LIST_FAILED", "Check status/limit and Operator read access.")


def hermes_mission_update(
    mission_id: str,
    patch_json: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        patch = json.loads(patch_json)
        if not isinstance(patch, dict):
            raise TypeError("patch_json must contain an object")
        _closed(patch, _ALLOWED_PATCH_KEYS, "mission patch")
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct mission update requires confirm=true")
        path = _db_path(hermes_root)

        def validate(row: sqlite3.Row, db: sqlite3.Connection) -> dict[str, Any]:
            if row["status"] in TERMINAL_STATUSES:
                raise ValueError("terminal missions cannot be edited")
            if any(key in patch for key in ("acceptance_criteria", "owner_profile")):
                attached = db.execute("SELECT 1 FROM attachments WHERE mission_id=? LIMIT 1", (mission_id,)).fetchone()
                if row["status"] != "draft" or attached:
                    raise ValueError("acceptance criteria and owner_profile freeze once mission work is attached or started")
            old_spec = json.loads(row["spec_json"])
            candidate = dict(old_spec)
            candidate.update(patch)
            candidate["mission_id"] = mission_id
            return _normalize_spec(candidate, mission_id=mission_id)

        if effective_dry:
            with _connect(path, write=False) as db:
                spec = validate(_get_row(db, mission_id), db)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_update", "mission_id": mission_id, "changed": False, "dry_run": True, "spec": spec})
        with _connect(path, write=True) as db:
            _begin_write(db)
            row = _get_row(db, mission_id)
            spec = validate(row, db)
            version = int(row["version"]) + 1
            now = _now()
            db.execute("UPDATE missions SET spec_json=?,version=?,updated_at=? WHERE mission_id=?", (json.dumps(spec, sort_keys=True), version, now, mission_id))
            live_notice = _event(db, mission_id, "mission.updated", details={"version": version, "fields": sorted(patch)})
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_update", policy, dry_run=False, success=True, changed=True, mission_id=mission_id, summary="mission updated")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_update", "mission_id": mission_id, "version": version, "changed": True})
    except (ValueError, LookupError, TypeError, json.JSONDecodeError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_UPDATE_REJECTED", "Check patch schema and mission lifecycle state.")

def hermes_mission_attach(
    mission_id: str,
    kind: str,
    ref: str,
    relationship: str = "contains",
    state: str = "unknown",
    evidence_ref: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if kind not in ATTACHMENT_KINDS:
            raise ValueError("attachment kind is invalid")
        ref = _bounded_text(ref, "attachment ref", 256, required=True)
        if not REF_RE.fullmatch(ref):
            raise ValueError("attachment ref contains unsupported characters")
        if kind == "workflow" and not WORKFLOW_REF_RE.fullmatch(ref):
            raise ValueError("workflow attachment ref must be a canonical workflow id")
        relationship = _bounded_text(relationship, "relationship", 64, required=True)
        if state not in ATTACHMENT_STATES:
            raise ValueError("attachment state is invalid")
        if state == "succeeded":
            raise PermissionError("public mission attachment cannot assert succeeded state")
        if kind == "delegation" and state == "cancelled":
            raise PermissionError("public mission attachment cannot assert delegation cancellation")
        evidence_ref = _bounded_text(evidence_ref, "evidence_ref", 256)
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct mission attachment requires confirm=true")
        path = _db_path(hermes_root)

        def validate(db: sqlite3.Connection) -> None:
            mission_row = _get_row(db, mission_id)
            if mission_row["status"] in TERMINAL_STATUSES:
                raise ValueError("terminal missions cannot be modified")
            count = int(db.execute("SELECT COUNT(*) FROM attachments WHERE mission_id=?", (mission_id,)).fetchone()[0])
            exists = db.execute("SELECT 1 FROM attachments WHERE mission_id=? AND kind=? AND ref=?", (mission_id, kind, ref)).fetchone()
            if count >= MAX_ATTACHMENTS and not exists:
                raise ValueError("mission attachment cap reached")

        if effective_dry:
            with _connect(path, write=False) as db:
                validate(db)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_attach", "mission_id": mission_id, "kind": kind, "ref": ref, "changed": False, "dry_run": True})
        now = _now()
        with _connect(path, write=True) as db:
            _begin_write(db)
            validate(db)
            db.execute(
                "INSERT INTO attachments(mission_id,kind,ref,relationship,state,evidence_ref,verified,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(mission_id,kind,ref) DO UPDATE SET relationship=excluded.relationship,state=excluded.state,evidence_ref=excluded.evidence_ref,verified=0,updated_at=excluded.updated_at",
                (mission_id, kind, ref, relationship, state, evidence_ref, 0, now, now),
            )
            db.execute("UPDATE missions SET version=version+1,updated_at=? WHERE mission_id=?", (now, mission_id))
            live_notice = _event(db, mission_id, "mission.attachment", details={"kind": kind, "ref": ref, "relationship": relationship, "state": state})
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_attach", policy, dry_run=False, success=True, changed=True, mission_id=mission_id, summary="mission attachment updated")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_attach", "mission_id": mission_id, "kind": kind, "ref": ref, "changed": True})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_ATTACH_REJECTED", "Check attachment kind/ref, cap, and mutation policy.")

def record_attachment_state(
    mission_id: str,
    kind: str,
    ref: str,
    state: str,
    *,
    evidence_ref: str = "",
    verified: bool = False,
    hermes_root: Path | None = None,
) -> bool:
    """Internal lifecycle bridge; successful state requires verified evidence."""
    if kind not in ATTACHMENT_KINDS or state not in ATTACHMENT_STATES:
        return False
    if kind == "workflow" and not WORKFLOW_REF_RE.fullmatch(ref):
        return False
    evidence_ref = evidence_ref[:256]
    if state == "succeeded" and (not verified or not evidence_ref):
        return False
    path = _db_path(hermes_root)
    if not path.is_file():
        return False
    try:
        with _connect(path, write=True) as db:
            _begin_write(db)
            row = db.execute("SELECT 1 FROM attachments WHERE mission_id=? AND kind=? AND ref=?", (mission_id, kind, ref)).fetchone()
            if row is None:
                return False
            now = _now()
            verified_value = 1 if verified and state == "succeeded" else 0
            db.execute("UPDATE attachments SET state=?,evidence_ref=?,verified=?,updated_at=? WHERE mission_id=? AND kind=? AND ref=?", (state, evidence_ref, verified_value, now, mission_id, kind, ref))
            db.execute("UPDATE missions SET version=version+1,updated_at=? WHERE mission_id=?", (now, mission_id))
            live_notice = _event(db, mission_id, "mission.attachment_state", details={"kind": kind, "ref": ref, "state": state, "verified": bool(verified_value)})
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        return True
    except (OSError, sqlite3.Error):
        return False

def _workflow_state(root: Path, ref: str) -> str:
    if not WORKFLOW_REF_RE.fullmatch(ref):
        return "unknown"
    base = (root / "swarm-workflows").resolve()
    path = (base / f"{ref}.json").resolve()
    if path.parent != base or not path.is_file():
        return "unknown"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    status = raw.get("status")
    return {
        "running": "running",
        "blocked": "blocked",
        "awaiting_approval": "blocked",
        "done": "succeeded",
    }.get(status, "unknown")


def _delegation_state(root: Path, mission_id: str, attachment: dict[str, Any]) -> tuple[str, bool]:
    """Re-observe one delegation from its authoritative durable lifecycle."""
    try:
        import operator_delegations as delegations

        payload = json.loads(delegations.hermes_delegation_get(str(attachment["ref"]), hermes_root=root))
    except (ImportError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError, sqlite3.Error):
        return "blocked", False
    row = payload.get("delegation")
    if not payload.get("success") or not isinstance(row, dict):
        return "blocked", False
    if str(row.get("mission_id") or "") != mission_id:
        return "blocked", False
    state = {
        "queued": "pending",
        "running": "running",
        "reconciling": "blocked",
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(str(row.get("state") or ""), "blocked")
    if state != "succeeded":
        return state, False
    contract_sha = str(row.get("contract_sha256") or "")
    expected_evidence = f"contract:{contract_sha}" if contract_sha else ""
    verified = (
        bool(expected_evidence)
        and str(row.get("validation_verdict") or "") == "SATISFIED"
        and str(attachment.get("evidence_ref") or "") == expected_evidence
    )
    return ("succeeded", True) if verified else ("blocked", False)


def _observe_attachments(root: Path, mission: dict[str, Any]) -> list[dict[str, Any]]:
    observed: list[dict[str, Any]] = []
    for att in mission["attachments"]:
        state = str(att["state"])
        verified = bool(att.get("verified"))
        if att["kind"] == "workflow":
            state = _workflow_state(root, str(att["ref"]))
            verified = state == "succeeded"
        elif att["kind"] == "delegation":
            state, verified = _delegation_state(root, str(mission["mission_id"]), att)
        elif state == "succeeded" and not verified:
            state = "blocked"
        observed.append({"kind": att["kind"], "ref": att["ref"], "state": state, "verified": verified})
    return observed


def _desired_mission_status(mission: dict[str, Any], observed: list[dict[str, Any]]) -> str:
    current = str(mission["status"])
    if current in TERMINAL_STATUSES:
        return current
    states = {str(item["state"]) for item in observed}
    if "failed" in states:
        return "failed"
    if "blocked" in states or "unknown" in states:
        return "blocked"
    if "running" in states or "pending" in states:
        return "running"
    if observed and states <= {"succeeded", "cancelled"} and "succeeded" in states:
        if mission["final_approval_required"] and not mission["approval"].get("approved"):
            return "awaiting_approval"
        return "completed"
    return current

def hermes_mission_reconcile(
    mission_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct mission reconciliation requires confirm=true")
        path = _db_path(hermes_root)
        root = _root(hermes_root)
        if effective_dry:
            with _connect(path, write=False) as db:
                mission = _row_to_mission(db, _get_row(db, mission_id))
            observed = _observe_attachments(root, mission)
            desired = _desired_mission_status(mission, observed)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_reconcile", "mission_id": mission_id, "status": mission["status"], "desired_status": desired, "observed": observed, "changed": False, "dry_run": True})

        live_notice: dict[str, Any] | None = None
        with _connect(path, write=True) as db:
            _begin_write(db)
            mission = _row_to_mission(db, _get_row(db, mission_id))
            current = str(mission["status"])
            observed = _observe_attachments(root, mission)
            desired = _desired_mission_status(mission, observed)
            original = {(a["kind"], a["ref"]): (a["state"], bool(a.get("verified"))) for a in mission["attachments"]}
            attachment_changed = any(original.get((item["kind"], item["ref"])) != (item["state"], bool(item["verified"])) for item in observed)
            status_changed = desired != current
            changed = attachment_changed or status_changed
            if changed:
                now = _now()
                for item in observed:
                    db.execute("UPDATE attachments SET state=?,verified=?,updated_at=? WHERE mission_id=? AND kind=? AND ref=?", (item["state"], 1 if item["verified"] else 0, now, mission_id, item["kind"], item["ref"]))
                db.execute("UPDATE missions SET status=?,version=version+1,updated_at=? WHERE mission_id=?", (desired, now, mission_id))
                live_notice = _event(db, mission_id, "mission.reconciled", from_status=current, to_status=desired, details={"observed": observed})
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_reconcile", policy, dry_run=False, success=True, changed=changed, mission_id=mission_id, summary=f"mission reconciled {current}->{desired}")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_reconcile", "mission_id": mission_id, "status": desired, "observed": observed, "changed": changed})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_RECONCILE_FAILED", "Check mission attachments and lifecycle state.")

def hermes_mission_transition(
    mission_id: str,
    status: str,
    reason: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        if status not in STATUSES:
            raise ValueError("target mission status is invalid")
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        reason = _bounded_text(reason, "reason", 1000)
        effective_dry = policy.effective_dry_run(dry_run)
        if status == "completed":
            policy.require_owner(dry_run)
        if status == "awaiting_approval":
            raise ValueError("awaiting_approval is entered only by reconciliation")
        if not effective_dry and not confirm:
            raise PermissionError("direct mission transition requires confirm=true")
        path = _db_path(hermes_root)

        def validate(mission: dict[str, Any]) -> tuple[str, bool]:
            current = str(mission["status"])
            if current in TERMINAL_STATUSES:
                raise ValueError("terminal mission status cannot transition")
            if status == current:
                return current, False
            allowed = MISSION_TRANSITIONS.get(current, set())
            if status != "completed" and status not in allowed:
                raise ValueError(f"mission transition {current}->{status} is not allowed")
            if status == "completed":
                if mission["final_approval_required"]:
                    raise PermissionError("approval-required missions complete only through hermes_mission_approve")
                observed = _observe_attachments(_root(hermes_root), mission)
                if _desired_mission_status(mission, observed) != "completed":
                    raise ValueError("mission children do not justify completion")
            return current, True

        if effective_dry:
            with _connect(path, write=False) as db:
                mission = _row_to_mission(db, _get_row(db, mission_id))
            current, would_change = validate(mission)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_transition", "mission_id": mission_id, "from_status": current, "to_status": status, "changed": False, "would_change": would_change, "dry_run": True})
        live_notice: dict[str, Any] | None = None
        with _connect(path, write=True) as db:
            _begin_write(db)
            mission = _row_to_mission(db, _get_row(db, mission_id))
            current, changed = validate(mission)
            if changed:
                now = _now()
                db.execute("UPDATE missions SET status=?,version=version+1,updated_at=? WHERE mission_id=?", (status, now, mission_id))
                live_notice = _event(db, mission_id, "mission.transition", from_status=current, to_status=status, reason=reason)
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_transition", policy, dry_run=False, success=True, changed=changed, mission_id=mission_id, summary=f"mission {current}->{status}")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_transition", "mission_id": mission_id, "from_status": current, "to_status": status, "changed": changed})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_TRANSITION_REJECTED", "Check mission lifecycle and Owner approval requirements.")

def hermes_mission_approve(
    mission_id: str,
    approval_reference: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_owner(dry_run)
        approval_reference = _bounded_text(approval_reference, "approval_reference", 256, required=True)
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct mission approval requires confirm=true")
        path = _db_path(hermes_root)

        root = _root(hermes_root)

        def validate(mission: dict[str, Any]) -> None:
            if mission["status"] != "awaiting_approval":
                raise ValueError("mission must be awaiting_approval before Owner approval")
            observed = _observe_attachments(root, mission)
            succeeded = [a for a in observed if a["state"] == "succeeded" and bool(a.get("verified"))]
            invalid = [a for a in observed if a["state"] not in {"succeeded", "cancelled"} or (a["state"] == "succeeded" and not bool(a.get("verified")))]
            if invalid or not succeeded:
                raise ValueError("mission children are not currently terminal with at least one verified success")

        approval = {"approved": True, "approved_by": "owner", "approval_reference": approval_reference, "approved_at": _now()}
        target = "completed"
        if effective_dry:
            with _connect(path, write=False) as db:
                mission = _row_to_mission(db, _get_row(db, mission_id))
            validate(mission)
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_approve", "mission_id": mission_id, "status": target, "approval": approval, "changed": False, "dry_run": True})
        with _connect(path, write=True) as db:
            _begin_write(db)
            mission = _row_to_mission(db, _get_row(db, mission_id))
            validate(mission)
            current = str(mission["status"])
            now = _now()
            db.execute("UPDATE missions SET status=?,approval_json=?,version=version+1,updated_at=? WHERE mission_id=?", (target, json.dumps(approval, sort_keys=True), now, mission_id))
            live_notice = _event(db, mission_id, "mission.approved", from_status=current, to_status=target, details={"approval_reference": approval_reference})
            db.commit()
        _publish_live_event(live_notice, hermes_root)
        _audit("hermes_mission_approve", policy, dry_run=False, success=True, changed=True, mission_id=mission_id, summary="mission Owner-approved")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_mission_approve", "mission_id": mission_id, "status": target, "approval": approval, "changed": True})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "MISSION_APPROVAL_REJECTED", "Reconcile verified child work and use Owner direct mode with confirm=true.")