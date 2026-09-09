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

import base64
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
_CURSOR_PREFIX = "ld1."
_MAX_CURSOR_TOKEN = 8192

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


def _read_mission_events(
    root: Path, mission_id: str, since: int = 0
) -> list[dict[str, Any]]:
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
            # Resume from the source-local watermark so pagination eventually
            # delivers events beyond the first MAX_PER_SOURCE window.
            rows = conn.execute(
                "SELECT seq, event_type, from_status, to_status, reason_sha256, details_json, created_at "
                "FROM mission_events WHERE mission_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
                (mission_id, since, MAX_PER_SOURCE),
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
                        "cursor_key": "mission",
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
    root: Path, mission_id: str, since: int = 0
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
                    "FROM delegation_events WHERE delegation_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
                    (delegation_id, since, MAX_PER_SOURCE),
                ).fetchall()
                for row in rows:
                    events.append(
                        {
                            "source": "delegation",
                            "cursor_key": "delegation",
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


def _read_audit_events(
    root: Path, mission_id: str, since: int = 0
) -> list[dict[str, Any]]:
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
                seq = n + 1
                if seq <= since:
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
                        "cursor_key": "audit",
                        "source_seq": seq,
                        "ts": str(rec.get("timestamp") or ""),
                        "kind": "tool_call",
                        "status_before": "",
                        "status_after": "success" if rec.get("success") else "error",
                        "event_id": f"audit:{rec.get('timestamp') or ''}:{seq}",
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


def _read_kanban_events(
    root: Path, task_ids: set[str], since_by_board: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not task_ids:
        return events
    since_by_board = since_by_board or {}
    boards = root / "kanban" / "boards"
    if not boards.is_dir():
        return events
    try:
        for board in sorted(p for p in boards.iterdir() if p.is_dir()):
            db = board / "kanban.db"
            if not db.is_file():
                continue
            slug = board.name
            since = int(since_by_board.get(f"kanban:{slug}", 0))
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
                        f"SELECT rowid AS source_rowid, task_id, kind, created_at, actor, summary FROM task_events "
                        f"WHERE task_id IN ({placeholders}) AND rowid>? ORDER BY rowid ASC LIMIT ?",
                        (*sorted(task_ids), since, MAX_PER_SOURCE),
                    ).fetchall()
                    for row in rows:
                        task_id = str(row["task_id"])
                        kind = str(row["kind"] or "")
                        ts = str(row["created_at"] or "")
                        events.append(
                            {
                                "source": "kanban",
                                "cursor_key": f"kanban:{slug}",
                                "source_seq": int(row["source_rowid"]),
                                "ts": ts,
                                "kind": _sanitize(kind or "task_event"),
                                "status_before": "",
                                "status_after": _sanitize(kind or ""),
                                "event_id": f"kanban:{slug}:{int(row['source_rowid'])}",
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
# Merge (stable vector cursor stream)
# ---------------------------------------------------------------------------


def _encode_cursor(watermarks: dict[str, int]) -> str:
    payload = json.dumps(
        {"v": 1, "w": dict(sorted(watermarks.items()))},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(value: Any) -> dict[str, int]:
    if value in (None, "", 0, "0"):
        return {}
    if isinstance(value, int):
        raise ValueError("legacy numeric ledger cursors are not resumable; restart from cursor=0")
    token = str(value).strip()
    if len(token) > _MAX_CURSOR_TOKEN or not token.startswith(_CURSOR_PREFIX):
        raise ValueError("ledger cursor is invalid")
    encoded = token[len(_CURSOR_PREFIX):]
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("ledger cursor is invalid") from exc
    watermarks = payload.get("w") if isinstance(payload, dict) and payload.get("v") == 1 else None
    if not isinstance(watermarks, dict) or len(watermarks) > 256:
        raise ValueError("ledger cursor is invalid")
    out: dict[str, int] = {}
    for key, seq in watermarks.items():
        if not isinstance(key, str) or not key or len(key) > 128 or not isinstance(seq, int) or seq < 0:
            raise ValueError("ledger cursor is invalid")
        out[key] = seq
    return out


def _event_cursor_key(event: dict[str, Any]) -> str:
    return str(event.get("cursor_key") or event.get("source") or "")


def _merge(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge append-only source streams without reordering within a source.

    Each authoritative source keeps its own stable monotonic sequence. The
    merge chooses the oldest timestamp only among each source's current head,
    so a late event with an older timestamp can never move behind a watermark
    that was already returned to a client.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        groups.setdefault(_event_cursor_key(event), []).append(event)
    for group in groups.values():
        group.sort(key=lambda e: int(e.get("source_seq", 0)))

    positions = {key: 0 for key in groups}
    ordered: list[dict[str, Any]] = []
    while True:
        candidates: list[tuple[tuple[float, int, str, int], str, dict[str, Any]]] = []
        for cursor_key, group in groups.items():
            pos = positions[cursor_key]
            if pos >= len(group):
                continue
            event = group[pos]
            sort_key = (
                _parse_iso_ts(event.get("ts")) or 0.0,
                _SOURCE_RANK.get(event.get("source", ""), 9),
                cursor_key,
                int(event.get("source_seq", 0)),
            )
            candidates.append((sort_key, cursor_key, event))
        if not candidates:
            break
        _key, cursor_key, event = min(candidates, key=lambda item: item[0])
        ordered.append(event)
        positions[cursor_key] += 1
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
    cursor_state: dict[str, int] | None = None,
) -> dict[str, Any]:
    truncated = len(events) > limit
    visible = events[:limit]
    watermarks = dict(cursor_state or {})
    for event in visible:
        cursor_key = _event_cursor_key(event)
        watermarks[cursor_key] = max(
            int(watermarks.get(cursor_key, 0)), int(event.get("source_seq", 0))
        )
        event["cursor"] = _encode_cursor(watermarks)
    next_cursor = _encode_cursor(watermarks)
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
        "max_cursor": next_cursor,
        "next_cursor": next_cursor,
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
    cursor: int | str = 0,
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
        cursor_state = {} if replay else _decode_cursor(cursor)
    except (TypeError, ValueError) as exc:
        warnings.append(str(exc))
        return json.dumps(
            _envelope(
                tool=tool,
                mission_id=mission_id,
                events=[],
                limit=limit,
                sources=[],
                warnings=warnings,
                trace_id=tid,
                mission_status=_mission_status(root, mission_id),
                cursor_state={},
            ),
            ensure_ascii=False,
            indent=2,
        )

    sources = [source] if source else list(LEDGER_SOURCES)
    if source and source not in LEDGER_SOURCES:
        warnings.append(f"unknown source {source!r}")
        sources = []
    queried = [s for s in sources if _source_allowed(s)]
    if len(queried) < len(sources):
        warnings.append("some sources filtered by allowlist")

    all_events: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    # Source-local resume watermarks: readers start at/after these, so
    # pagination walks successive MAX_PER_SOURCE windows instead of stalling
    # on the first one.
    since = {key: int(seq) for key, seq in cursor_state.items()} if not replay else {}
    for s in queried:
        if s == "delegation":
            deps, task_ids = _read_delegation_events(
                root, mission_id, since.get("delegation", 0)
            )
            all_events.extend(deps)
        elif s == "kanban":
            # kanban needs the mission's task set; if delegation not queried,
            # fetch the task set quietly for the join.
            if not task_ids:
                _, task_ids = _read_delegation_events(root, mission_id)
            all_events.extend(_read_kanban_events(root, task_ids, since))
        elif s in _SOURCE_READERS:
            all_events.extend(
                _SOURCE_READERS[s](root, mission_id, since.get(s, 0))
                if s != "kanban"
                else []
            )

    merged = _merge(all_events)
    if not replay:
        merged = [
            event
            for event in merged
            if int(event.get("source_seq", 0))
            > int(cursor_state.get(_event_cursor_key(event), 0))
        ]

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
            cursor_state=cursor_state,
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
