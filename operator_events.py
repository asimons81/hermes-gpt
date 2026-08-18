"""Structured event history read-model for hermes-gpt v0.7 (Flight Deck, S4).

Implements ADR-004/ADR-005: ``hermes_events_query`` / ``hermes_events_tail``
normalize the existing durable stores (operator audit JSONL, swarm-workflows
JSON, codex-jobs JSON, cron executions.db, kanban task_events) into the
EventRecord view schema at query time. The surface is **read-only by
construction** — no parallel write path exists (ADR-005).

Redaction invariants match Mission Control: no raw messages, memory bodies,
transcripts, request dumps, credentials, or profile-secret bodies; free text
is summarized; prompts appear only as length/sha when present in the source.

Allowlist: ``HERMES_GPT_EVENTS_ALLOWED_SOURCES`` with the same semantics as
``HERMES_GPT_MISSION_ALLOWED_SURFACES`` (unset = all read-only sources; list =
only listed; empty = none). Retention window:
``HERMES_GPT_EVENTS_MAX_AGE_DAYS`` (default 90). Every call is audited.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import operator_policy as op

SCHEMA_VERSION = "0.7-ev.1"
EVENT_SCHEMA = "hermes.event/v1"

EVENTS_ALLOWED_SOURCES_ENV = "HERMES_GPT_EVENTS_ALLOWED_SOURCES"
EVENTS_MAX_AGE_DAYS_ENV = "HERMES_GPT_EVENTS_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 90

EVENT_SOURCES: tuple[str, ...] = ("audit", "swarm", "codex", "cron", "kanban")

MAX_QUERY_LIMIT = 200
MAX_TAIL_LIMIT = 100
MAX_PER_SOURCE = 500
_ERROR_STRING_CAP = 500
_TTL_SECONDS = 5

_PII_STRIP = re.compile(
    r"(?i)(sk-[a-zA-Z0-9]{20,}|[A-Za-z0-9._~-]{43,128}@[A-Za-z0-9._-]+|"
    r"Bearer\s+[A-Za-z0-9._~-]{20,}|ghp_[A-Za-z0-9]{20,})"
)
_WHITESPACE = re.compile(r"\s+")


def _sanitize(text: Any, limit: int = _ERROR_STRING_CAP) -> str:
    """Truncate + strip PII-like tokens from a free-text summary."""
    if text is None:
        return ""
    value = _WHITESPACE.sub(" ", str(text)).strip()
    value = _PII_STRIP.sub("[REDACTED]", value)
    if len(value) > limit:
        return value[:limit] + "…[truncated]"
    return value


def _hash_meta(text: str | None) -> dict[str, Any]:
    if not text:
        return {"len": 0, "sha256": ""}
    data = text.encode("utf-8", errors="replace")
    return {"len": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _resolve_root(hermes_root: Path | None) -> Path:
    """Resolve a concrete Hermes root (falls back to the default)."""
    if hermes_root is not None:
        return hermes_root
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for cand in [
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return Path.home() / ".hermes"


# ---------------------------------------------------------------------------
# Allowlist + retention
# ---------------------------------------------------------------------------


def _allowed_sources() -> set[str]:
    """Return the set of event sources this deployment permits.

    Unset env: all read-only sources. List: only listed valid sources.
    Empty string: no sources. Do not describe the unset state as deny-by-default.
    """
    raw = os.environ.get(EVENTS_ALLOWED_SOURCES_ENV)
    if raw is None:
        return set(EVENT_SOURCES)
    allowed: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if item in EVENT_SOURCES:
            allowed.add(item)
    return allowed


def _source_allowed(source: str) -> bool:
    return source in _allowed_sources()


def _max_age_days() -> int:
    raw = os.environ.get(EVENTS_MAX_AGE_DAYS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_DAYS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS
    return max(1, min(value, 3650))


def _retention_cutoff() -> float:
    return time.time() - _max_age_days() * 24 * 60 * 60


# ---------------------------------------------------------------------------
# Cache (in-process, ephemeral, post-redaction only) — mirrors Mission Control
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _cache_key(source: str, root: Path) -> str:
    return f"{source}::{str(root)}"


def _cache_get(source: str, root: Path) -> list[dict[str, Any]] | None:
    entry = _cache.get(_cache_key(source, root))
    if not entry:
        return None
    ts, events = entry
    if time.time() - ts > _TTL_SECONDS:
        return None
    return events


def _cache_set(source: str, root: Path, events: list[dict[str, Any]]) -> None:
    _cache[_cache_key(source, root)] = (time.time(), events)


# ---------------------------------------------------------------------------
# Per-source readers (read-only; redacted; bounded)
# ---------------------------------------------------------------------------


def _read_audit_events(root: Path) -> list[dict[str, Any]]:
    path = root / "logs" / "hermes_gpt_operator_audit.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = rec.get("timestamp") or ""
                events.append(
                    {
                        "event_id": f"audit:{rec.get('timestamp') or ''}:{len(events)}",
                        "ts": ts,
                        "source": "audit",
                        "kind": "tool_call",
                        "actor": rec.get("profile") or rec.get("tool") or "",
                        "subject_id": rec.get("task_id") or rec.get("job_id") or rec.get("skill_name") or "",
                        "status_before": "",
                        "status_after": "success" if rec.get("success") else "error",
                        "summary": _sanitize(rec.get("summary") or rec.get("error") or rec.get("tool") or ""),
                        "refs": [rec.get("tool") or ""] if rec.get("tool") else [],
                        "trace_id": "",
                    }
                )
                if len(events) >= MAX_PER_SOURCE:
                    break
    except OSError:
        pass
    return events


def _read_swarm_events(root: Path) -> list[dict[str, Any]]:
    d = root / "swarm-workflows"
    if not d.is_dir():
        return []
    events: list[dict[str, Any]] = []
    try:
        for path in sorted(d.glob("sw-*.json"))[:20]:
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            workflow_id = rec.get("workflow_id") or path.stem
            ts = rec.get("updated_at") or rec.get("created_at") or ""
            events.append(
                {
                    "event_id": f"swarm:{workflow_id}:status",
                    "ts": ts,
                    "source": "swarm",
                    "kind": "workflow_status",
                    "actor": "",
                    "subject_id": workflow_id,
                    "status_before": "",
                    "status_after": _sanitize(rec.get("status") or ""),
                    "summary": f"workflow {workflow_id} {rec.get('status') or 'unknown'}",
                    "refs": [f"swarm:{workflow_id}"],
                    "trace_id": "",
                }
            )
            for stage in rec.get("stages", []):
                sid = stage.get("id", "")
                sts = stage.get("status", "")
                if not sts:
                    continue
                events.append(
                    {
                        "event_id": f"swarm:{workflow_id}:{sid}:{sts}",
                        "ts": stage.get("ended_at") or stage.get("started_at") or ts,
                        "source": "swarm",
                        "kind": f"stage_{sts}",
                        "actor": stage.get("owner") or "",
                        "subject_id": workflow_id,
                        "status_before": "",
                        "status_after": _sanitize(sts),
                        "summary": f"stage {sid} {sts}",
                        "refs": [f"swarm:{workflow_id}"],
                        "trace_id": "",
                    }
                )
    except OSError:
        pass
    return events


def _read_codex_events(root: Path) -> list[dict[str, Any]]:
    d = root / "codex-jobs"
    if not d.is_dir():
        return []
    events: list[dict[str, Any]] = []
    try:
        for path in sorted(d.glob("*.json"))[:20]:
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            job_id = rec.get("job_id") or path.stem
            ts = rec.get("updated_at") or rec.get("created_at") or rec.get("started_at") or ""
            events.append(
                {
                    "event_id": f"codex:{job_id}:status",
                    "ts": ts,
                    "source": "codex",
                    "kind": "job_status",
                    "actor": rec.get("profile") or rec.get("agent") or "",
                    "subject_id": job_id,
                    "status_before": "",
                    "status_after": _sanitize(rec.get("status") or ""),
                    "summary": f"codex job {job_id} {rec.get('status') or 'unknown'}",
                    "refs": [f"codex:{job_id}"],
                    "trace_id": "",
                }
            )
    except OSError:
        pass
    return events


def _iter_profiles(root: Path) -> list[str]:
    profiles: list[str] = []
    root_profiles = root / "profiles"
    try:
        for p in sorted(root_profiles.iterdir()):
            if p.is_dir():
                profiles.append(p.name)
    except OSError:
        pass
    if not profiles:
        profiles.append("default")
    return profiles


def _open_ro(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise FileNotFoundError(f"database not found: {db.name}")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_cron_events(root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for profile in _iter_profiles(root):
        db = root / "profiles" / profile / "cron" / "executions.db"
        if not db.exists():
            continue
        try:
            conn = _open_ro(db)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(executions)")}
                rows = conn.execute(
                    "SELECT * FROM executions ORDER BY started_at DESC LIMIT ?", (MAX_PER_SOURCE,)
                ).fetchall()
                for row in rows:
                    job_id = str(row["job_id"]) if "job_id" in cols and row["job_id"] is not None else ""
                    status = str(row["status"]) if "status" in cols and row["status"] is not None else ""
                    ts = str(row["started_at"]) if "started_at" in cols and row["started_at"] is not None else ""
                    error = str(row["error"]) if "error" in cols and row["error"] is not None else ""
                    events.append(
                        {
                            "event_id": f"cron:{profile}:{job_id}:{ts}:{len(events)}",
                            "ts": ts,
                            "source": "cron",
                            "kind": "job_run",
                            "actor": profile,
                            "subject_id": job_id,
                            "status_before": "",
                            "status_after": _sanitize(status),
                            "summary": f"cron job {job_id} {status}" + (f" error={_sanitize(error, 120)}" if error and status == "failed" else ""),
                            "refs": [f"cron:{job_id}"],
                            "trace_id": "",
                        }
                    )
            finally:
                conn.close()
        except (FileNotFoundError, sqlite3.Error, OSError):
            continue
    return events


def _read_kanban_events(root: Path) -> list[dict[str, Any]]:
    boards = root / "kanban" / "boards"
    if not boards.is_dir():
        return []
    events: list[dict[str, Any]] = []
    try:
        for board in sorted(p for p in boards.iterdir() if p.is_dir()):
            db = board / "kanban.db"
            if not db.exists():
                continue
            slug = board.name
            try:
                conn = _open_ro(db)
                try:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
                    rows = conn.execute(
                        "SELECT * FROM task_events ORDER BY created_at DESC LIMIT ?", (MAX_PER_SOURCE,)
                    ).fetchall()
                    for row in rows:
                        task_id = str(row["task_id"]) if "task_id" in cols and row["task_id"] is not None else ""
                        kind = str(row["kind"]) if "kind" in cols and row["kind"] is not None else ""
                        ts = str(row["created_at"]) if "created_at" in cols and row["created_at"] is not None else ""
                        summary = str(row["summary"]) if "summary" in cols and row["summary"] is not None else ""
                        events.append(
                            {
                                "event_id": f"kanban:{slug}:{task_id}:{ts}:{len(events)}",
                                "ts": ts,
                                "source": "kanban",
                                "kind": _sanitize(kind or "task_event"),
                                "actor": str(row["actor"]) if "actor" in cols and row["actor"] is not None else "",
                                "subject_id": task_id,
                                "status_before": "",
                                "status_after": _sanitize(kind),
                                "summary": _sanitize(summary) if summary else f"kanban event {kind}",
                                "refs": [f"kanban:{task_id}"],
                                "trace_id": "",
                            }
                        )
                finally:
                    conn.close()
            except (FileNotFoundError, sqlite3.Error, OSError):
                continue
    except OSError:
        pass
    return events


_SOURCE_READERS: dict[str, Callable[[Path], list[dict[str, Any]]]] = {
    "audit": _read_audit_events,
    "swarm": _read_swarm_events,
    "codex": _read_codex_events,
    "cron": _read_cron_events,
    "kanban": _read_kanban_events,
}


def _events_for_source(source: str, root: Path) -> list[dict[str, Any]]:
    cached = _cache_get(source, root)
    if cached is not None:
        return cached
    reader = _SOURCE_READERS.get(source)
    events = reader(root) if reader else []
    _cache_set(source, root, events)
    return events


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


def _collect(
    root: Path,
    *,
    sources: Iterable[str],
    since: str = "",
    until: str = "",
    subject_id: str = "",
    kind: str = "",
) -> list[dict[str, Any]]:
    cutoff = _retention_cutoff()
    since_ts = _parse_iso_ts(since)
    until_ts = _parse_iso_ts(until)
    events: list[dict[str, Any]] = []
    for source in sources:
        if not _source_allowed(source):
            continue
        for ev in _events_for_source(source, root):
            ts = _parse_iso_ts(ev.get("ts"))
            if ts is not None and ts < cutoff:
                continue
            if since_ts is not None and (ts is None or ts < since_ts):
                continue
            if until_ts is not None and (ts is None or ts > until_ts):
                continue
            if subject_id and subject_id not in str(ev.get("subject_id") or ""):
                continue
            if kind and ev.get("kind") != kind:
                continue
            events.append(ev)
    # Stable ordering: newest first by ts; ties by event_id.
    events.sort(key=lambda e: (_parse_iso_ts(e.get("ts")) or 0.0, e.get("event_id", "")), reverse=True)
    return events


def _audit_events_call(tool: str, *, success: bool, summary: str, extra: dict[str, Any] | None = None) -> None:
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
    except Exception:
        pass


def _events_envelope(
    *,
    tool: str,
    events: list[dict[str, Any]],
    limit: int,
    sources: list[str],
    warnings: list[str],
    trace_id: str,
) -> dict[str, Any]:
    truncated = len(events) > limit
    visible = events[:limit]
    return {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "event_schema": EVENT_SCHEMA,
        "tool": tool,
        "surface": "events",
        "trace_id": trace_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count_returned": len(visible),
        "count_total": len(events),
        "truncated": truncated,
        "sources_queried": sources,
        "sources_allowed": sorted(_allowed_sources()),
        "retention_max_age_days": _max_age_days(),
        "warnings": warnings,
        "events": visible,
    }


def hermes_events_query(
    source: str = "",
    subject_id: str = "",
    kind: str = "",
    since: str = "",
    until: str = "",
    limit: int = 50,
    hermes_root: Path | None = None,
) -> str:
    """Query the normalized event timeline (read-only, redacted, bounded).

    ``source`` one of audit|swarm|codex|cron|kanban (empty = all allowed).
    Returns newest-first events filtered by subject_id/kind/time window.
    """
    tool = "hermes_events_query"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    warnings: list[str] = []

    try:
        limit = max(1, min(int(limit), MAX_QUERY_LIMIT))
    except (TypeError, ValueError):
        limit = 50

    sources = [source] if source else list(EVENT_SOURCES)
    if source and source not in EVENT_SOURCES:
        warnings.append(f"unknown source {source!r}")
        sources = []
    queried = [s for s in sources if _source_allowed(s)]
    if len(queried) < len(sources):
        warnings.append("some sources filtered by allowlist")

    events = _collect(root, sources=sources, since=since, until=until, subject_id=subject_id, kind=kind)
    _audit_events_call(
        tool,
        success=True,
        summary=f"events query source={source or 'all'} subject={subject_id or ''} kind={kind or ''} count={len(events)}",
        extra={"subject_id": subject_id[:80], "kind": kind[:80], "count": len(events)},
    )
    return json.dumps(_events_envelope(tool=tool, events=events, limit=limit, sources=sources, warnings=warnings, trace_id=tid), ensure_ascii=False, indent=2)


def hermes_events_tail(limit: int = 20, hermes_root: Path | None = None) -> str:
    """Recent events across all allowed sources (read-only, redacted)."""
    tool = "hermes_events_tail"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)

    try:
        limit = max(1, min(int(limit), MAX_TAIL_LIMIT))
    except (TypeError, ValueError):
        limit = 20

    events = _collect(root, sources=EVENT_SOURCES)
    _audit_events_call(
        tool,
        success=True,
        summary=f"events tail count={len(events)}",
        extra={"count": len(events)},
    )
    return json.dumps(_events_envelope(tool=tool, events=events, limit=limit, sources=list(EVENT_SOURCES), warnings=[], trace_id=tid), ensure_ascii=False, indent=2)
