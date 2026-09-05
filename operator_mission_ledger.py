"""Per-mission merged operational ledger read model (vNext slice-1, phase 1).

Implements proposal §17 item 3 / §2.2 row 4 / §6.2 (``mission_ledger``): an
append-only, replayable, **merged cursor stream** per mission reconciled at
query time from the existing authoritative stores — it does **not** replace
them and introduces no new store.

Sources reconciled (each authoritative; none is re-derived):
- ``mission_events``     -> ``<root>/missions/missions.db``
- ``delegation_events``  -> ``<root>/delegations/delegations.db`` (linked via
  the mission's delegations; tasks are carried for the kanban join)
- operator audit         -> ``<root>/logs/hermes_gpt_operator_audit.jsonl``
- kanban ``task_events`` -> ``<root>/kanban/boards/<slug>/kanban.db`` (for the
  tasks owned by this mission's delegations)

Every event is assigned a monotonic merged ``cursor``; the stream is
deterministic given the same store state, so **replay reproduces the event
history**. Read-only by construction: all SQLite sources open ``mode=ro``
and no mutation path is exposed.

INV-9 (data containment): no raw prompt, transcript, memory body, credential,
or secret-path content crosses the surface. Raw payloads are summarized and
content-addressed (sha256) rather than emitted.

Conventions mirror ``operator_events``: bounded JSON envelope, audited call,
allowlist env (``HERMES_GPT_LEDGER_ALLOWED_SOURCES``), bounded output.
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

import operator_delegations as op_delegations
import operator_mission_runtime as op_mission_runtime
import operator_policy as op

SCHEMA_VERSION = "0.1-ld.1"
LEDGER_SCHEMA = "hermes.mission-ledger/v1"

LEDGER_SOURCES_ENV = "HERMES_GPT_LEDGER_ALLOWED_SOURCES"
LEDGER_SOURCES: tuple[str, ...] = ("mission", "delegation", "audit", "kanban")

MAX_LIMIT = 500
MAX_PER_SOURCE = 500
_ERROR_STRING_CAP = 500
_SOURCE_RANK = {"mission": 0, "delegation": 1, "audit": 2, "kanban": 3}

_PII_STRIP = re.compile(
    r"(?i)(sk-[a-zA-Z0-9]{20,}|[A-Za-z0-9._~-]{43,128}@[A-Za-z0-9._-]+|"
    r"Bearer\s+[A-Za-z0-9._~-]{20,}|ghp_[A-Za-z0-9]{20,})"
)
_WHITESPACE = re.compile(r"\s+")


def _sanitize(text: Any, limit: int = _ERROR_STRING_CAP) -> str:
    if text is None:
        return ""
    value = _WHITESPACE.sub(" ", str(text)).strip()
    value = _PII_STRIP.sub("[REDACTED]", value)
    if len(value) > limit:
        return value[:limit] + "…[truncated]"
    return value


def _sha256(text: str | None) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _resolve_root(hermes_root: Path | None) -> Path:
    if hermes_root is not None:
        return Path(hermes_root)
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for cand in [Path.home() / ".hermes"]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return Path.home() / ".hermes"


def _open_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def _allowed_sources() -> set[str]:
    raw = os.environ.get(LEDGER_SOURCES_ENV)
    if raw is None:
        return set(LEDGER_SOURCES)
    allowed: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if item in LEDGER_SOURCES:
            allowed.add(item)
    return allowed


def _source_allowed(source: str) -> bool:
    return source in _allowed_sources()


def _parse_iso_ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Per-source readers (read-only; redacted; bounded)
# ---------------------------------------------------------------------------


def _read_mission_events(root: Path, mission_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    db = op_mission_runtime._db_path(root)
    if not db.is_file():
        return events
    try:
        conn = _open_ro(db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(mission_events)")}
            if "mission_id" not in cols:
                return events
            rows = conn.execute(
                "SELECT seq, event_type, from_status, to_status, reason_sha256, details_json, created_at "
                "FROM mission_events WHERE mission_id=? ORDER BY seq ASC LIMIT ?",
                (mission_id, MAX_PER_SOURCE),
            ).fetchall()
            for row in rows:
                details_json = (
                    str(row["details_json"])
                    if row["details_json"] is not None
                    else "{}"
                )
                events.append(
                    {
                        "source": "mission",
                        "source_seq": int(row["seq"]),
                        "ts": str(row["created_at"] or ""),
                        "kind": _sanitize(row["event_type"] or ""),
                        "status_before": _sanitize(row["from_status"] or ""),
                        "status_after": _sanitize(row["to_status"] or ""),
                        "reason_sha256": _sanitize(row["reason_sha256"] or "", 64),
                        "event_id": f"mission:{int(row['seq'])}",
                        "refs": [f"mission:{mission_id}"],
                        "summary": "",
                        "provenance_sha256": _sha256(details_json),
                    }
                )
        finally:
            conn.close()
    except (FileNotFoundError, sqlite3.Error, OSError):
        pass
    return events


def _read_delegation_events(
    root: Path, mission_id: str
) -> tuple[list[dict[str, Any]], set[str]]:
    events: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    db = op_delegations._db_path(root)
    if not db.is_file():
        return events, task_ids
    try:
        conn = _open_ro(db)
        try:
            dcols = {r[1] for r in conn.execute("PRAGMA table_info(delegations)")}
            if "mission_id" not in dcols:
                return events, task_ids
            deps = conn.execute(
                "SELECT delegation_id, task_id, state FROM delegations WHERE mission_id=? LIMIT ?",
                (mission_id, MAX_PER_SOURCE),
            ).fetchall()
            delegation_ids = [str(d["delegation_id"]) for d in deps]
            for d in deps:
                task_ids.add(str(d["task_id"]))
            ecols = {r[1] for r in conn.execute("PRAGMA table_info(delegation_events)")}
            if "delegation_id" not in ecols:
                return events, task_ids
            for delegation_id in delegation_ids:
                rows = conn.execute(
                    "SELECT seq, event_type, from_state, to_state, backend_state, observed_sha256, created_at "
                    "FROM delegation_events WHERE delegation_id=? ORDER BY seq ASC LIMIT ?",
                    (delegation_id, MAX_PER_SOURCE),
                ).fetchall()
                for row in rows:
                    events.append(
                        {
                            "source": "delegation",
                            "source_seq": int(row["seq"]),
                            "ts": str(row["created_at"] or ""),
                            "kind": _sanitize(row["event_type"] or ""),
                            "status_before": _sanitize(row["from_state"] or ""),
                            "status_after": _sanitize(row["to_state"] or ""),
                            "event_id": f"delegation:{delegation_id}:{int(row['seq'])}",
                            "refs": [
                                f"delegation:{delegation_id}",
                                f"mission:{mission_id}",
                            ],
                            "summary": _sanitize(
                                row["backend_state"] or "", _ERROR_STRING_CAP
                            ),
                            "provenance_sha256": _sanitize(
                                row["observed_sha256"] or "", 64
                            ),
                        }
                    )
        finally:
            conn.close()
    except (FileNotFoundError, sqlite3.Error, OSError):
        pass
    return events, task_ids


def _read_audit_events(root: Path, mission_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    path = root / "logs" / "hermes_gpt_operator_audit.jsonl"
    if not path.is_file():
        return events
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("mission_id") != mission_id:
                    continue
                events.append(
                    {
                        "source": "audit",
                        "source_seq": n,
                        "ts": str(rec.get("timestamp") or ""),
                        "kind": "tool_call",
                        "status_before": "",
                        "status_after": "success" if rec.get("success") else "error",
                        "event_id": f"audit:{rec.get('timestamp') or ''}:{n}",
                        "refs": [rec.get("tool") or "", f"mission:{mission_id}"],
                        "summary": _sanitize(
                            rec.get("summary")
                            or rec.get("error")
                            or rec.get("tool")
                            or ""
                        ),
                        "provenance_sha256": _sha256(
                            str(rec.get("prompt_sha256") or "")
                        ),
                    }
                )
                if len(events) >= MAX_PER_SOURCE:
                    break
    except OSError:
        pass
    return events


def _read_kanban_events(root: Path, task_ids: set[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not task_ids:
        return events
    boards = root / "kanban" / "boards"
    if not boards.is_dir():
        return events
    try:
        for board in sorted(p for p in boards.iterdir() if p.is_dir()):
            db = board / "kanban.db"
            if not db.is_file():
                continue
            slug = board.name
            try:
                conn = _open_ro(db)
                try:
                    cols = {
                        r[1] for r in conn.execute("PRAGMA table_info(task_events)")
                    }
                    if "task_id" not in cols:
                        continue
                    placeholders = ",".join("?" for _ in task_ids)
                    rows = conn.execute(
                        f"SELECT task_id, kind, created_at, actor, summary FROM task_events "
                        f"WHERE task_id IN ({placeholders}) ORDER BY created_at ASC LIMIT ?",
                        (*sorted(task_ids), MAX_PER_SOURCE),
                    ).fetchall()
                    for row in rows:
                        task_id = str(row["task_id"])
                        kind = str(row["kind"] or "")
                        ts = str(row["created_at"] or "")
                        events.append(
                            {
                                "source": "kanban",
                                "source_seq": len(events),
                                "ts": ts,
                                "kind": _sanitize(kind or "task_event"),
                                "status_before": "",
                                "status_after": _sanitize(kind or ""),
                                "event_id": f"kanban:{slug}:{task_id}:{ts}:{len(events)}",
                                "refs": [f"kanban:{task_id}", f"task:{task_id}"],
                                "summary": _sanitize(row["summary"])
                                if row["summary"]
                                else f"kanban event {kind}",
                                "provenance_sha256": "",
                            }
                        )
                finally:
                    conn.close()
            except (FileNotFoundError, sqlite3.Error, OSError):
                continue
    except OSError:
        pass
    return events


_SOURCE_READERS: dict[str, Any] = {
    "mission": _read_mission_events,
    "delegation": _read_delegation_events,
    "audit": _read_audit_events,
    "kanban": _read_kanban_events,
}


# ---------------------------------------------------------------------------
# Merge (deterministic cursor stream)
# ---------------------------------------------------------------------------


def _merge(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order events causally and assign a monotonic merged cursor.

    Primary key: parseable ISO timestamp (fallback 0). Tie-break by source
    rank then source_seq so the ordering is deterministic given the same
    store state -> replay reproduces the exact event history.
    """

    def key(e: dict[str, Any]) -> tuple[float, int, int]:
        return (
            _parse_iso_ts(e.get("ts")) or 0.0,
            _SOURCE_RANK.get(e.get("source", ""), 9),
            int(e.get("source_seq", 0)),
        )

    ordered = sorted(events, key=key)
    for idx, e in enumerate(ordered, start=1):
        e["cursor"] = idx
    return ordered


# ---------------------------------------------------------------------------
# Audit + envelope + public tool
# ---------------------------------------------------------------------------


def _audit(
    tool: str, *, success: bool, summary: str, extra: dict[str, Any] | None = None
) -> None:
    policy = op.OperatorPolicy()
    try:
        op.audit_record(
            tool=tool,
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=True,
            success=success,
            changed=False,
            summary=_sanitize(summary, 300),
            extra=extra or {},
        )
    except Exception:  # noqa: BLE001, S110 - audit must never break the call
        pass


def _envelope(
    *,
    tool: str,
    mission_id: str,
    events: list[dict[str, Any]],
    limit: int,
    sources: list[str],
    warnings: list[str],
    trace_id: str,
    mission_status: str,
) -> dict[str, Any]:
    truncated = len(events) > limit
    visible = events[:limit]
    max_cursor = max((int(e.get("cursor", 0)) for e in events), default=0)
    return {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "ledger_schema": LEDGER_SCHEMA,
        "tool": tool,
        "surface": "mission_ledger",
        "mission_id": mission_id,
        "mission_status": mission_status,
        "trace_id": trace_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count_returned": len(visible),
        "count_total": len(events),
        "truncated": truncated,
        "max_cursor": max_cursor,
        "next_cursor": max_cursor + 1 if not truncated else max_cursor,
        "sources_queried": sources,
        "sources_allowed": sorted(_allowed_sources()),
        "warnings": warnings,
        "events": visible,
    }


def _mission_status(root: Path, mission_id: str) -> str:
    db = op_mission_runtime._db_path(root)
    if not db.is_file():
        return ""
    try:
        conn = _open_ro(db)
        try:
            row = conn.execute(
                "SELECT status FROM missions WHERE mission_id=?", (mission_id,)
            ).fetchone()
            return _sanitize(row["status"]) if row else ""
        finally:
            conn.close()
    except (FileNotFoundError, sqlite3.Error, OSError):
        return ""


def hermes_mission_ledger(
    mission_id: str,
    source: str = "",
    cursor: int = 0,
    limit: int = 100,
    replay: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """Query the merged, replayable per-mission ledger (read-only, INV-9).

    ``source`` one of mission|delegation|audit|kanban (empty = all allowed).
    ``cursor`` resumes the stream after a cursor value (append-only read).
    ``replay=True`` returns the full ordered stream from the beginning,
    reproducing the mission's event history.
    """
    tool = "hermes_mission_ledger"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    warnings: list[str] = []

    mission_id = _sanitize(mission_id, 256)
    if not mission_id:
        warnings.append("mission_id is required")
        return json.dumps(
            _envelope(
                tool=tool,
                mission_id="",
                events=[],
                limit=0,
                sources=[],
                warnings=warnings,
                trace_id=tid,
                mission_status="",
            )
        )

    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = 100
    try:
        cursor = max(0, int(cursor))
    except (TypeError, ValueError):
        cursor = 0

    sources = [source] if source else list(LEDGER_SOURCES)
    if source and source not in LEDGER_SOURCES:
        warnings.append(f"unknown source {source!r}")
        sources = []
    queried = [s for s in sources if _source_allowed(s)]
    if len(queried) < len(sources):
        warnings.append("some sources filtered by allowlist")

    all_events: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    for s in queried:
        if s == "delegation":
            deps, task_ids = _read_delegation_events(root, mission_id)
            all_events.extend(deps)
        elif s == "kanban":
            # kanban needs the mission's task set; if delegation not queried,
            # fetch the task set quietly for the join.
            if not task_ids:
                _, task_ids = _read_delegation_events(root, mission_id)
            all_events.extend(_read_kanban_events(root, task_ids))
        elif s in _SOURCE_READERS:
            all_events.extend(
                _SOURCE_READERS[s](root, mission_id) if s != "kanban" else []
            )

    merged = _merge(all_events)
    if not replay:
        merged = [e for e in merged if int(e.get("cursor", 0)) > cursor]

    status = _mission_status(root, mission_id)
    _audit(
        tool,
        success=True,
        summary=f"mission ledger mission={mission_id[:64]} count={len(merged)}",
        extra={"mission_id": mission_id[:128], "count": len(merged)},
    )
    return json.dumps(
        _envelope(
            tool=tool,
            mission_id=mission_id,
            events=merged,
            limit=limit,
            sources=sources,
            warnings=warnings,
            trace_id=tid,
            mission_status=status,
        ),
        ensure_ascii=False,
        indent=2,
    )


def hermes_mission_ledger_replay(
    mission_id: str,
    limit: int = 500,
    hermes_root: Path | None = None,
) -> str:
    """Replay a mission's full event history (read-only, INV-9).

    Thin convenience wrapper over ``hermes_mission_ledger`` with ``replay``
    set: reproduces the append-only merged cursor stream from the beginning.
    """
    return hermes_mission_ledger(
        mission_id=mission_id,
        replay=True,
        limit=limit,
        hermes_root=hermes_root,
    )
