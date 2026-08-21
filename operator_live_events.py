"""Durable live-event bus and authenticated WebSocket stream for Hermes GPT v0.9.

The bus is an append-only, bounded notification layer.  It does not replace the
source-of-truth journals owned by Work Contracts, Swarm, Fabric, Missions, or
runners.  Events wake interested parents/clients and point back to durable state.

The public MCP surfaces are read-only cursor/long-poll reads.  The WebSocket
surface is also read-only with respect to Hermes state; inbound control frames
may only change the client's subscription/ack cursor or request a ping.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.routing import BaseRoute, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

import operator_policy as op

SCHEMA_VERSION = "0.9-live.1"
EVENT_SCHEMA = "hermes.live-event/v1"
STREAM_SCHEMA = "hermes.live-stream/v1"

MAX_PAYLOAD_BYTES = 16 * 1024
MAX_EVENT_STRING = 1_000
MAX_TOPIC = 96
MAX_KIND = 128
MAX_SUBJECT = 192
MAX_QUERY = 500
MAX_WAIT_MS = 30_000
DEFAULT_RETENTION = 20_000
HARD_RETENTION = 100_000

SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,191}$")
SECRETISH_KEYS = (
    "prompt",
    "content",
    "body",
    "transcript",
    "secret",
    "credential",
    "authorization",
    "cookie",
    "password",
    "private_key",
    "api_key",
    "access_token",
    "refresh_token",
)

_condition = threading.Condition()


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
    return _root(hermes_root) / "live-events" / "events.db"


def _retention() -> int:
    raw = os.environ.get("HERMES_GPT_LIVE_EVENT_RETENTION", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_RETENTION
    except ValueError:
        value = DEFAULT_RETENTION
    return max(100, min(value, HARD_RETENTION))


def _init(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS live_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            topic TEXT NOT NULL,
            kind TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_live_events_mission ON live_events(mission_id, seq);
        CREATE INDEX IF NOT EXISTS idx_live_events_topic ON live_events(topic, seq);
        CREATE INDEX IF NOT EXISTS idx_live_events_kind ON live_events(kind, seq);
        """
    )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=5)
        _init(db)
    else:
        if not path.is_file():
            raise FileNotFoundError(path)
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def _secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SECRETISH_KEYS)


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:64]:
            k = str(key)[:128]
            out[k] = "[REDACTED]" if _secret_key(k) else _sanitize(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_sanitize(item, depth=depth + 1) for item in value[:64]]
    if isinstance(value, str):
        return op.redact_output(value)[:MAX_EVENT_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return op.redact_output(str(value))[:MAX_EVENT_STRING]


def _bounded_ref(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{field} is too long")
    if value and not SAFE_REF_RE.fullmatch(value):
        raise ValueError(f"{field} contains unsupported characters")
    return value


def publish_event(
    *,
    topic: str,
    kind: str,
    subject_type: str,
    subject_id: str,
    mission_id: str = "",
    source: str,
    payload: dict[str, Any] | None = None,
    event_id: str = "",
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """Append one bounded event from trusted internal code."""
    topic = _bounded_ref(topic, "topic", MAX_TOPIC)
    kind = _bounded_ref(kind, "kind", MAX_KIND)
    subject_type = _bounded_ref(subject_type, "subject_type", 64)
    subject_id = _bounded_ref(subject_id, "subject_id", MAX_SUBJECT)
    mission_id = _bounded_ref(mission_id, "mission_id", MAX_SUBJECT)
    source = _bounded_ref(source, "source", 96)
    if not topic or not kind or not subject_type or not subject_id or not source:
        raise ValueError("topic, kind, subject_type, subject_id, and source are required")
    safe_payload = _sanitize(payload or {})
    encoded = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        safe_payload = {"truncated": True, "sha256": hashlib.sha256(encoded.encode()).hexdigest()}
        encoded = json.dumps(safe_payload, sort_keys=True)
    created = _now()
    if not event_id:
        event_id = "lev-" + hashlib.sha256(
            f"{topic}\0{kind}\0{subject_type}\0{subject_id}\0{created}\0{encoded}".encode()
        ).hexdigest()[:24]
    event_id = _bounded_ref(event_id, "event_id", MAX_SUBJECT)
    path = _db_path(hermes_root)
    with _connect(path, write=True) as db:
        existing = db.execute("SELECT * FROM live_events WHERE event_id=?", (event_id,)).fetchone()
        if existing is not None:
            return _row_event(existing)
        cur = db.execute(
            "INSERT INTO live_events(event_id,topic,kind,subject_type,subject_id,mission_id,source,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, topic, kind, subject_type, subject_id, mission_id, source, encoded, created),
        )
        seq = int(cur.lastrowid)
        retention = _retention()
        if seq > retention:
            floor = max(0, seq - retention)
            db.execute("DELETE FROM live_events WHERE seq<=?", (floor,))
        db.commit()
        row = db.execute("SELECT * FROM live_events WHERE seq=?", (seq,)).fetchone()
    with _condition:
        _condition.notify_all()
    assert row is not None
    return _row_event(row)


def _row_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema": EVENT_SCHEMA,
        "seq": int(row["seq"]),
        "event_id": row["event_id"],
        "topic": row["topic"],
        "kind": row["kind"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "mission_id": row["mission_id"],
        "source": row["source"],
        "payload": json.loads(row["payload_json"]),
        "created_at": row["created_at"],
    }


def read_since(
    cursor: int = 0,
    *,
    mission_id: str = "",
    topic: str = "",
    kind: str = "",
    limit: int = 100,
    hermes_root: Path | None = None,
) -> tuple[list[dict[str, Any]], int]:
    cursor = max(0, int(cursor))
    limit = max(1, min(int(limit), MAX_QUERY))
    mission_id = _bounded_ref(mission_id, "mission_id", MAX_SUBJECT)
    topic = _bounded_ref(topic, "topic", MAX_TOPIC)
    kind = _bounded_ref(kind, "kind", MAX_KIND)
    path = _db_path(hermes_root)
    if not path.is_file():
        return [], cursor
    clauses = ["seq>?"]
    args: list[Any] = [cursor]
    for column, value in (("mission_id", mission_id), ("topic", topic), ("kind", kind)):
        if value:
            clauses.append(f"{column}=?")
            args.append(value)
    args.append(limit)
    with _connect(path, write=False) as db:
        rows = db.execute(
            f"SELECT * FROM live_events WHERE {' AND '.join(clauses)} ORDER BY seq ASC LIMIT ?",
            tuple(args),
        ).fetchall()
        high = db.execute("SELECT COALESCE(MAX(seq),0) FROM live_events").fetchone()[0]
    events = [_row_event(row) for row in rows]
    next_cursor = events[-1]["seq"] if events else cursor
    return events, max(next_cursor, min(int(high or 0), next_cursor))


def high_watermark(hermes_root: Path | None = None) -> int:
    path = _db_path(hermes_root)
    if not path.is_file():
        return 0
    with _connect(path, write=False) as db:
        return int(db.execute("SELECT COALESCE(MAX(seq),0) FROM live_events").fetchone()[0] or 0)


def hermes_live_events_cursor(hermes_root: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "cursor": high_watermark(hermes_root)})
    except (PermissionError, OSError, sqlite3.Error) as exc:
        return json.dumps(op.error_from_exception(exc, layer="operator", code="LIVE_EVENT_CURSOR_FAILED", suggested_action="Check Operator read access."))


def hermes_live_events_since(
    cursor: int = 0,
    mission_id: str = "",
    topic: str = "",
    kind: str = "",
    limit: int = 100,
    wait_ms: int = 0,
    hermes_root: Path | None = None,
) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        wait_ms = max(0, min(int(wait_ms), MAX_WAIT_MS))
        deadline = time.monotonic() + wait_ms / 1000
        events, next_cursor = read_since(cursor, mission_id=mission_id, topic=topic, kind=kind, limit=limit, hermes_root=hermes_root)
        while not events and wait_ms and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            with _condition:
                _condition.wait(timeout=min(0.25, remaining))
            events, next_cursor = read_since(cursor, mission_id=mission_id, topic=topic, kind=kind, limit=limit, hermes_root=hermes_root)
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "stream_schema": STREAM_SCHEMA,
                "cursor": int(cursor),
                "next_cursor": next_cursor,
                "high_watermark": high_watermark(hermes_root),
                "events": events,
                "count": len(events),
            },
            ensure_ascii=False,
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return json.dumps(op.error_from_exception(exc, layer="operator", code="LIVE_EVENT_READ_FAILED", suggested_action="Check cursor/filter bounds and Operator read access."))


async def _websocket_endpoint(
    websocket: WebSocket,
    root_getter: Callable[[], Path | None],
    auth_check: Callable[[WebSocket], Awaitable[bool]] | None = None,
) -> None:
    if auth_check is not None and not await auth_check(websocket):
        await websocket.close(code=4401)
        return
    try:
        op.OperatorPolicy().require_level("read_only")
    except PermissionError:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    try:
        cursor = max(0, int(websocket.query_params.get("cursor", "0") or 0))
    except ValueError:
        cursor = 0
    mission_id = websocket.query_params.get("mission_id", "")[:MAX_SUBJECT]
    topic = websocket.query_params.get("topic", "")[:MAX_TOPIC]
    kind = websocket.query_params.get("kind", "")[:MAX_KIND]
    last_heartbeat = time.monotonic()
    try:
        while True:
            try:
                events, next_cursor = read_since(
                    cursor,
                    mission_id=mission_id,
                    topic=topic,
                    kind=kind,
                    limit=100,
                    hermes_root=root_getter(),
                )
            except (ValueError, OSError, sqlite3.Error):
                await websocket.send_json({"schema": STREAM_SCHEMA, "type": "error", "code": "LIVE_EVENT_READ_FAILED"})
                await websocket.close(code=1011)
                return
            if events:
                cursor = next_cursor
                await websocket.send_json(
                    {"schema": STREAM_SCHEMA, "type": "events", "cursor": cursor, "events": events}
                )
                continue
            now = time.monotonic()
            if now - last_heartbeat >= 15:
                await websocket.send_json({"schema": STREAM_SCHEMA, "type": "heartbeat", "cursor": cursor})
                last_heartbeat = now
            try:
                text = await asyncio.wait_for(websocket.receive_text(), timeout=0.5)
            except TimeoutError:
                continue
            if len(text) > 4096:
                await websocket.close(code=1009)
                return
            try:
                control = json.loads(text)
            except ValueError:
                await websocket.send_json({"schema": STREAM_SCHEMA, "type": "error", "code": "INVALID_CONTROL"})
                continue
            if not isinstance(control, dict):
                continue
            action = control.get("action")
            if action == "ping":
                await websocket.send_json({"schema": STREAM_SCHEMA, "type": "pong", "cursor": cursor})
            elif action == "ack":
                try:
                    ack = max(0, int(control.get("cursor", cursor)))
                except (TypeError, ValueError):
                    ack = cursor
                cursor = max(cursor, ack)
            elif action == "subscribe":
                candidate_mission = str(control.get("mission_id") or "")[:MAX_SUBJECT]
                candidate_topic = str(control.get("topic") or "")[:MAX_TOPIC]
                candidate_kind = str(control.get("kind") or "")[:MAX_KIND]
                try:
                    _bounded_ref(candidate_mission, "mission_id", MAX_SUBJECT)
                    _bounded_ref(candidate_topic, "topic", MAX_TOPIC)
                    _bounded_ref(candidate_kind, "kind", MAX_KIND)
                except ValueError:
                    await websocket.send_json({"schema": STREAM_SCHEMA, "type": "error", "code": "INVALID_SUBSCRIPTION"})
                    continue
                mission_id, topic, kind = candidate_mission, candidate_topic, candidate_kind
                if "cursor" in control:
                    try:
                        cursor = max(0, int(control["cursor"]))
                    except (TypeError, ValueError):
                        pass
                await websocket.send_json(
                    {
                        "schema": STREAM_SCHEMA,
                        "type": "subscribed",
                        "cursor": cursor,
                        "mission_id": mission_id,
                        "topic": topic,
                        "kind": kind,
                    }
                )
            else:
                await websocket.send_json({"schema": STREAM_SCHEMA, "type": "error", "code": "UNSUPPORTED_CONTROL"})
    except WebSocketDisconnect:
        return


def websocket_routes(
    root_getter: Callable[[], Path | None],
    auth_check: Callable[[WebSocket], Awaitable[bool]] | None = None,
) -> list[BaseRoute]:
    async def endpoint(websocket: WebSocket) -> None:
        await _websocket_endpoint(websocket, root_getter, auth_check)

    return [WebSocketRoute("/events/ws", endpoint)]
