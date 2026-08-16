"""Minimal SessionDB-compatible shim for hermes-gpt UI chat.

The full Hermes Agent ``hermes_state.SessionDB`` is a heavy, multi-mixin module
with FTS5, CJK trigrams, and deep integrations into the Hermes CLI.  The
hermes-gpt chat UI only needs a small, stable subset of its interface for
persisting webui conversations.  This module provides a lightweight SQLite
implementation of that subset so the sidecar can run in environments where the
Hermes Agent source tree is not available (e.g., CI, fresh installs, or packaged
distributions).

Behavioral notes:
- The schema is intentionally the narrow set the chat UI exercises:
  ``create_session``, ``append_message``, ``get_messages``, ``list_sessions_rich``,
  ``get_session``, ``try_acquire_session_turn_lease``, and
  ``release_session_turn_lease``.
- The full ``SessionDB`` API is **not** implemented.  Any code that calls
  unimplemented methods will fail fast with ``NotImplementedError``.
- ``source='webui'`` sessions are filtered by ``list_sessions_rich`` exactly as
  the chat UI expects.
- This is **not** a drop-in replacement for the real SessionDB; it is a runtime
  fallback so the chat UI can function without a Hermes Agent checkout.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes_gpt.ui_state")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    model TEXT,
    profile_name TEXT,
    started_at REAL NOT NULL,
    last_activity_at REAL,
    message_count INTEGER NOT NULL DEFAULT 0,
    title TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    finish_reason TEXT,
    timestamp REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_turn_leases (
    conversation_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
"""


class SessionDB:
    """Minimal session store implementing the chat-UI subset of SessionDB."""

    def __init__(self, db_path: Optional[Path] = None, read_only: bool = False) -> None:
        if db_path is None:
            db_path = Path.home() / ".hermes" / "state.db"
        self.db_path = Path(db_path)
        self.read_only = read_only
        self._lock = threading.Lock()
        self._local = threading.local()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        if self.read_only:
            return
        with self._lock:
            self._conn().executescript(SCHEMA_SQL)
            self._conn().commit()

    def _now(self) -> float:
        return time.time()

    def create_session(
        self,
        session_id: str,
        source: str,
        **kwargs: Any,
    ) -> str:
        if self.read_only:
            raise RuntimeError("SessionDB is read-only")
        now = self._now()
        with self._lock:
            self._conn().execute(
                """
                INSERT INTO sessions (id, source, model, profile_name, started_at, last_activity_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    source,
                    kwargs.get("model"),
                    kwargs.get("profile_name"),
                    now,
                    now,
                ),
            )
            self._conn().commit()
        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_calls: Any = None,
        tool_call_id: Optional[str] = None,
        finish_reason: Optional[str] = None,
        **kwargs: Any,
    ) -> int:
        if self.read_only:
            raise RuntimeError("SessionDB is read-only")
        tool_calls_json = json.dumps(tool_calls) if tool_calls is not None else None
        now = self._now()
        with self._lock:
            cur = self._conn().execute(
                """
                INSERT INTO messages (session_id, role, content, tool_name, tool_call_id, tool_calls, finish_reason, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    tool_name,
                    tool_call_id,
                    tool_calls_json,
                    finish_reason,
                    now,
                ),
            )
            self._conn().execute(
                """
                UPDATE sessions
                SET message_count = message_count + 1, last_activity_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )
            self._conn().commit()
        return cur.lastrowid or 0

    def get_messages(
        self,
        session_id: str,
        include_inactive: bool = False,
        include_compacted: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
        latest: bool = False,
        after_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        order = "DESC" if latest else "ASC"
        clauses = ["session_id = ?"]
        params: List[Any] = [session_id]
        if not include_inactive:
            clauses.append("active = 1")
        if not include_compacted:
            clauses.append("compacted = 0")
        if after_id is not None:
            clauses.append("id > ?")
            params.append(after_id)
        sql = f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY timestamp {order}, id {order}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(offset)
        rows = self._conn().execute(sql, params).fetchall()
        return [dict(row) for row in (reversed(rows) if latest else rows)]

    def list_sessions_rich(
        self,
        source: Optional[str] = None,
        sources: Optional[List[str]] = None,
        exclude_sources: Optional[List[str]] = None,
        cwd_prefix: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        min_message_count: int = 0,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
        include_archived: bool = False,
        archived_only: bool = False,
        id_query: Optional[str] = None,
        search_query: Optional[str] = None,
        compact_rows: bool = False,
        include_pinned: bool = False,
        session_key: Optional[str] = None,
        include_hidden: bool = False,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        clauses = ["1=1"]
        params: List[Any] = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        elif sources:
            placeholders = ", ".join("?" for _ in sources)
            clauses.append(f"source IN ({placeholders})")
            params.extend(sources)
        if exclude_sources:
            placeholders = ", ".join("?" for _ in exclude_sources)
            clauses.append(f"source NOT IN ({placeholders})")
            params.extend(exclude_sources)
        if id_query:
            clauses.append("id LIKE ?")
            params.append(f"%{id_query}%")
        if min_message_count:
            clauses.append("message_count >= ?")
            params.append(min_message_count)
        order = "ORDER BY last_activity_at DESC, started_at DESC" if order_by_last_active else "ORDER BY started_at DESC"
        sql = f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} {order} LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn().execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def try_acquire_session_turn_lease(
        self,
        session_id: str,
        holder: str,
        *,
        ttl_seconds: float = 300.0,
        patience_s: Optional[float] = None,
    ) -> bool:
        if patience_s:
            deadline = time.monotonic() + patience_s
            while time.monotonic() < deadline:
                if self._try_acquire(session_id, holder, ttl_seconds):
                    return True
                time.sleep(0.05)
            return False
        return self._try_acquire(session_id, holder, ttl_seconds)

    def _try_acquire(self, session_id: str, holder: str, ttl_seconds: float) -> bool:
        now = self._now()
        expires = now + ttl_seconds
        with self._lock:
            # Expire any stale lease first.
            self._conn().execute(
                "DELETE FROM session_turn_leases WHERE conversation_id = ? AND expires_at < ?",
                (session_id, now),
            )
            existing = self._conn().execute(
                "SELECT holder FROM session_turn_leases WHERE conversation_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None and existing["holder"] != holder:
                self._conn().commit()
                return False
            self._conn().execute(
                """
                INSERT INTO session_turn_leases (conversation_id, holder, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    holder = excluded.holder,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                """,
                (session_id, holder, now, expires),
            )
            self._conn().commit()
        return True

    def refresh_session_turn_lease(
        self, session_id: str, holder: str, ttl_seconds: float = 300.0
    ) -> bool:
        now = self._now()
        expires = now + ttl_seconds
        with self._lock:
            cur = self._conn().execute(
                "UPDATE session_turn_leases SET expires_at = ? WHERE conversation_id = ? AND holder = ?",
                (expires, session_id, holder),
            )
            self._conn().commit()
        return cur.rowcount > 0

    def release_session_turn_lease(self, session_id: str, holder: str) -> None:
        with self._lock:
            self._conn().execute(
                "DELETE FROM session_turn_leases WHERE conversation_id = ? AND holder = ?",
                (session_id, holder),
            )
            self._conn().commit()

    def __getattr__(self, name: str) -> Any:
        # Fail closed on any SessionDB method the chat UI does not exercise.
        raise NotImplementedError(
            f"hermes_gpt SessionDB shim does not implement '{name}'"
        )
