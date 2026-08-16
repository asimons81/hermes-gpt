"""Hermes GPT browser chat bridge — core chat shell (kanban t_9d31cd92).

A UI conversation IS a Hermes SessionDB session (``source='webui'``) — no
separate UI database.  This module owns:

- ``GET    /api/sessions``            — sidebar history (webui sessions)
- ``POST   /api/sessions``            — create an empty session
- ``GET    /api/sessions/:id/messages`` — resume a thread from persisted rows
- ``POST   /api/chat``                — start a turn; SSE stream (meta|token|
                                         reasoning|tool_start|tool_end|
                                         message_complete|error|done)
- ``GET    /api/chat/stream``         — reconnect replay + live tail
- ``POST   /api/chat/stop``           — interrupt the running turn

Turns run Hermes' real agent loop (``AIAgent`` + ``run_conversation`` with
stream callbacks — the gateway pattern) in a daemon thread.  Every SSE event
is buffered per ``turn_id`` so a dropped browser tab can reconnect and replay
from its last seen ``id:`` seq before tailing live events.  The turn lease
(``session_turn_leases``) guarantees one live turn per session; a second send
on a busy session returns ``409 TURN_IN_PROGRESS``.

Redaction: browser payloads are scrubbed here as an interim boundary until the
``ui_security`` card lands the full ``redact_browser`` layer.  Tool summaries
are truncated to ``HERMES_GPT_UI_TOOL_PREVIEW_BYTES`` (default 8 KiB) and
obvious credential shapes are masked with ``[REDACTED]`` markers.  Raw
prompts, memory bodies, transcripts, and secret-file paths are never
serialized (AGENTS.md Mission Control invariants).

Route ownership: this module contributes ``ui_chat_routes()`` only.  It does
not edit operator modules, ``ui_ops.py``, ``ui_security.py``, or ``server.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, Deque, Dict, List, Optional, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger("hermes_gpt.ui_chat")

# ── Env knobs (existing env unchanged; UI gates opt-in) ───────────────────
UI_PROFILE_ENV = "HERMES_GPT_UI_PROFILE"
UI_STALE_LEASE_S_ENV = "HERMES_GPT_UI_STALE_LEASE_S"
UI_TOOL_PREVIEW_BYTES_ENV = "HERMES_GPT_UI_TOOL_PREVIEW_BYTES"
UI_MAX_CONCURRENT_ENV = "HERMES_GPT_UI_MAX_CONCURRENT"

DEFAULT_STALE_LEASE_S = 600
DEFAULT_TOOL_PREVIEW_BYTES = 8192
DEFAULT_MAX_CONCURRENT = 4

# Finished turns keep their replay buffer this long after the last event.
TURN_RETENTION_S = 1800.0
# SSE replay/tail heartbeat interval (keeps proxies from closing idle streams).
SSE_HEARTBEAT_S = 15.0
# Bounded event ring per turn (reconnect replay never grows unbounded).
TURN_EVENT_RING_MAX = 4096
MESSAGE_PAGE_LIMIT = 500
SESSION_LIST_LIMIT = 200


# ── Turn state + registry ─────────────────────────────────────────────────

class Turn:
    """One in-flight (or recently finished) chat turn.

    ``publish`` / ``snapshot_after`` are safe to call from the agent worker
    thread and from asyncio reader threads (via ``asyncio.to_thread``).
    """

    def __init__(self, session_id: str, turn_id: str, holder: str) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.holder = holder
        self.seq = 0
        self.events: Deque[Tuple[int, str, dict]] = deque()
        self.cond = threading.Condition()
        self.agent: Any = None
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.finish_reason: Optional[str] = None
        self.error: Optional[str] = None
        self.done = False
        self.cancel_requested = False
        self.tool_times: Dict[str, float] = {}

    def publish(self, event: str, data: dict) -> None:
        with self.cond:
            self.seq += 1
            self.events.append((self.seq, event, data))
            if len(self.events) > TURN_EVENT_RING_MAX:
                self.events.popleft()
            self.cond.notify_all()

    def mark_done(self, finish_reason: str, error: Optional[str] = None) -> None:
        with self.cond:
            self.done = True
            self.finish_reason = finish_reason
            self.error = error
            self.finished_at = time.time()
            self.cond.notify_all()

    def snapshot_after(self, after: int, timeout: Optional[float] = None) -> Tuple[List[Tuple[int, str, dict]], bool]:
        """Return ``(events with seq > after, done)``; block up to ``timeout``.

        Blocks on the condition variable when nothing new is available, so a
        long-lived SSE tail does not busy-poll.  ``timeout=None`` waits until
        the next event or turn completion.
        """
        wait_for = timeout if timeout is not None else 0.05
        with self.cond:
            deadline = time.monotonic() + max(0.0, wait_for)
            while True:
                events = [e for e in self.events if e[0] > after]
                if events or self.done:
                    return events, self.done
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], self.done
                self.cond.wait(remaining)


_turns_lock = threading.Lock()
_turns: Dict[str, Turn] = {}


def _register_turn(turn: Turn) -> None:
    _prune_turns()
    with _turns_lock:
        _turns[turn.session_id] = turn


def _get_turn(session_id: str, turn_id: Optional[str] = None) -> Optional[Turn]:
    with _turns_lock:
        turn = _turns.get(session_id)
        if turn is None:
            return None
        if turn_id is not None and turn.turn_id != turn_id:
            return None
        return turn


def _active_turn(session_id: str) -> Optional[Turn]:
    turn = _get_turn(session_id)
    if turn is not None and not turn.done:
        return turn
    return None


def _active_turn_count() -> int:
    now = time.time()
    with _turns_lock:
        return sum(1 for t in _turns.values() if not t.done and (now - t.started_at) < TURN_RETENTION_S)


def _prune_turns() -> None:
    now = time.time()
    with _turns_lock:
        expired = [sid for sid, t in _turns.items() if t.done and (now - (t.finished_at or t.started_at)) > TURN_RETENTION_S]
        for sid in expired:
            _turns.pop(sid, None)


# ── Session store (Hermes SessionDB, source='webui') ──────────────────────

_session_db_instance: Any = None
_session_db_lock = threading.Lock()


def _hermes_home() -> Path:
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".hermes"


def _session_db() -> Any:
    global _session_db_instance
    if _session_db_instance is None:
        with _session_db_lock:
            if _session_db_instance is None:
                try:
                    from hermes_state import SessionDB

                    _session_db_instance = SessionDB(db_path=_hermes_home() / "state.db")
                except Exception as exc:
                    logger.warning("ui_chat: hermes_state.SessionDB unavailable (%s), using local shim", exc)
                    from hermes_state import SessionDB as ShimSessionDB

                    _session_db_instance = ShimSessionDB(db_path=_hermes_home() / "state.db")
    return _session_db_instance


def _ui_profile() -> str:
    return os.environ.get(UI_PROFILE_ENV, "").strip() or "default"


def _tool_preview_bytes() -> int:
    try:
        return int(os.environ.get(UI_TOOL_PREVIEW_BYTES_ENV, DEFAULT_TOOL_PREVIEW_BYTES))
    except ValueError:
        return DEFAULT_TOOL_PREVIEW_BYTES


def _max_concurrent() -> int:
    try:
        return max(1, int(os.environ.get(UI_MAX_CONCURRENT_ENV, DEFAULT_MAX_CONCURRENT)))
    except ValueError:
        return DEFAULT_MAX_CONCURRENT


def _resolve_model() -> str:
    """Resolve the model from the Hermes config (gateway source of truth)."""
    try:
        from gateway.run import _load_gateway_config, _resolve_gateway_model

        return _resolve_gateway_model(_load_gateway_config()) or ""
    except Exception:
        logger.debug("ui_chat: model resolution unavailable", exc_info=True)
        return ""


def _resolve_toolsets() -> Optional[List[str]]:
    """Resolve the CLI default toolset set for the agent (gateway-style)."""
    try:
        from gateway.run import _load_gateway_config
        from hermes_cli.tools_config import _get_platform_tools

        return sorted(_get_platform_tools(_load_gateway_config(), "cli"))
    except Exception:
        logger.debug("ui_chat: toolset resolution unavailable", exc_info=True)
        return None


def _session_exists(db: Any, session_id: str) -> bool:
    try:
        return db.get_session(session_id) is not None
    except Exception:
        return False


def _create_session(db: Any, *, model: str, profile: str) -> str:
    session_id = uuid.uuid4().hex
    db.create_session(
        session_id=session_id,
        source="webui",
        model=model or None,
        profile_name=None if profile == "default" else profile,
    )
    return session_id


def _serialize_session(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": row.get("id"),
        "title": row.get("title") or "",
        "profile": row.get("profile_name") or "default",
        "model": row.get("model") or "",
        "message_count": int(row.get("message_count") or 0),
        "last_activity_at": row.get("last_active") or row.get("started_at") or 0,
        "created_at": row.get("started_at") or 0,
    }


# ── Redaction-lite (interim boundary until ui_security lands) ─────────────

_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{8,})\b"),
    re.compile(r"(?i)\b(bearer\s+[A-Za-z0-9._~+/=-]{8,})\b"),
    re.compile(r"(?i)\b(api[_-]?key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._-]{8,})"),
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token)[\"']?\s*[:=]\s*[\"']?[^\s\"']{4,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

# Tool args whose values are user/message content — never surfaced raw.
_CONTENT_ARG_KEYS = {
    "prompt", "message", "content", "text", "query", "body", "input",
    "user_message", "command", "code", "goal", "context",
}

_SECRET_PATH_RE = re.compile(r"(?i)(?:/|\\|^)(?:\.env|secrets?)(?:/|\\|\.|$)|\.pem\b|\.key\b")


def _redact_text(text: str) -> str:
    if not text:
        return text
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = _SECRET_PATH_RE.sub("[REDACTED]", text)
    return text


def _truncate(text: str, cap: int) -> str:
    if text is None:
        return ""
    if len(text) <= cap:
        return text
    return text[:cap] + "\n…[truncated]"


def _safe_tool_brief(name: str, args: Any) -> str:
    """One-line tool summary safe for the browser (no raw prompt content)."""
    if isinstance(args, dict):
        safe_parts = []
        for key, value in args.items():
            if key in _CONTENT_ARG_KEYS:
                safe_parts.append(f"{key}=…")
            else:
                try:
                    rendered = json.dumps(value, ensure_ascii=False)[:120]
                except Exception:
                    rendered = "…"
                safe_parts.append(f"{key}={rendered}")
        brief = ", ".join(safe_parts)
    else:
        brief = str(args)[:200]
    brief = _redact_text(brief)
    return _truncate(brief, 240)


# ── Message mapping ───────────────────────────────────────────────────────

def _parse_tool_calls(raw: Any) -> Optional[List[dict]]:
    if not raw:
        return None
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(calls, list):
        return None
    out = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        out.append({
            "call_id": call.get("id"),
            "name": function.get("name"),
            "arguments": function.get("arguments"),
        })
    return out or None


def _serialize_message(row: Dict[str, Any]) -> Dict[str, Any]:
    role = row.get("role") or ""
    finish_reason = row.get("finish_reason")
    base: Dict[str, Any] = {
        "message_id": row.get("id"),
        "role": role,
        "content": row.get("content") or "",
        "ts": row.get("timestamp") or 0,
        "tool_calls": _parse_tool_calls(row.get("tool_calls")),
        "tool_result": None,
        "tool_name": None,
        "tool_call_id": None,
        "finish_reason": finish_reason,
        "interrupted": finish_reason == "interrupted",
    }
    if role == "tool":
        base["tool_result"] = _truncate(_redact_text(row.get("content") or ""), _tool_preview_bytes())
        base["tool_name"] = row.get("tool_name")
        base["tool_call_id"] = row.get("tool_call_id")
    return base


# ── Agent turn execution ──────────────────────────────────────────────────

def _make_stream_callbacks(turn: Turn) -> Tuple[Any, Any, Any, Any]:
    """Build the stream callbacks that translate agent events into SSE events.

    Extracted from ``_build_agent`` so tests can drive the exact SSE event
    encoding with a stub agent (no live model, no AIAgent construction).
    """

    def on_token(delta: str) -> None:
        if delta:
            turn.publish("token", {"delta": delta})

    def on_reasoning(delta: str) -> None:
        if delta:
            turn.publish("reasoning", {"delta": delta})

    def on_tool_start(call_id: str, name: str, display_args: Any) -> None:
        turn.tool_times[call_id] = time.monotonic()
        turn.publish("tool_start", {
            "call_id": call_id,
            "name": name,
            "brief": _safe_tool_brief(name, display_args),
        })

    def on_tool_complete(call_id: str, name: str, display_args: Any, result: Any) -> None:
        started = turn.tool_times.pop(call_id, None)
        duration_ms = int(round((time.monotonic() - started) * 1000)) if started else None
        if isinstance(result, str):
            summary = _redact_text(result)
        else:
            try:
                summary = _redact_text(json.dumps(result, ensure_ascii=False, default=str))
            except Exception:
                summary = ""
        turn.publish("tool_end", {
            "call_id": call_id,
            "name": name,
            "status": "ok",
            "summary": _truncate(summary, 400),
            "duration_ms": duration_ms,
        })

    return on_token, on_reasoning, on_tool_start, on_tool_complete


def _build_agent(*, turn: Turn, db: Any, model: str, profile: str) -> Any:
    """Construct the Hermes AIAgent exactly like the gateway does.

    The agent persists user/assistant/tool rows into SessionDB itself and
    streams deltas through the callbacks below.
    """
    from run_agent import AIAgent

    on_token, on_reasoning, on_tool_start, on_tool_complete = _make_stream_callbacks(turn)

    return AIAgent(
        model=model,
        session_id=turn.session_id,
        platform="webui",
        quiet_mode=True,
        verbose_logging=False,
        ephemeral_system_prompt=None,
        enabled_toolsets=_resolve_toolsets(),
        session_db=db,
        max_iterations=60,
        stream_delta_callback=on_token,
        reasoning_callback=on_reasoning,
        tool_start_callback=on_tool_start,
        tool_complete_callback=on_tool_complete,
    )


def _execute_turn(agent: Any, turn: Turn, *, message: str, db: Any) -> dict:
    """Run one Hermes turn; the agent persists the transcript itself."""
    if turn.cancel_requested:
        agent.interrupt(hard_cancel=True)
    return agent.run_conversation(user_message=message, task_id=turn.session_id)


def _latest_message_id(db: Any, session_id: str, role: Optional[str] = None) -> Optional[int]:
    try:
        rows = db.get_messages(session_id, include_compacted=True, limit=1, latest=True)
    except Exception:
        return None
    if role is not None:
        for row in rows:
            if row.get("role") == role:
                return row.get("id")
        return None
    return rows[0].get("id") if rows else None


def _finalize_turn(turn: Turn, result: Optional[dict], db: Any) -> None:
    result = result or {}
    interrupted = bool(result.get("interrupted"))
    failed = bool(result.get("failed"))
    if interrupted:
        finish_reason = "interrupted"
    elif failed:
        finish_reason = "error"
    else:
        finish_reason = "end_turn"
    message_id = _latest_message_id(db, turn.session_id, role="assistant")
    if message_id is None:
        message_id = _latest_message_id(db, turn.session_id)
    if message_id is not None:
        turn.publish("message_complete", {"message_id": message_id, "role": "assistant"})
    turn.publish("done", {
        "turn_id": turn.turn_id,
        "message_id": message_id,
        "finish_reason": finish_reason,
    })
    turn.mark_done(finish_reason)


def _run_turn(turn: Turn, *, message: str, profile: str, model: str, db: Any, holder: str) -> None:
    """Daemon-thread body: build the agent, run the loop, emit done/error."""
    try:
        if turn.cancel_requested:
            turn.mark_done("interrupted")
            return
        agent = _build_agent(turn=turn, db=db, model=model, profile=profile)
        turn.agent = agent
        result = _execute_turn(agent, turn, message=message, db=db)
        _finalize_turn(turn, result, db)
    except ImportError as exc:
        logger.error("ui_chat: agent imports unavailable: %s", exc)
        turn.publish("error", {"code": "IMPORT_UNAVAILABLE", "message": "Hermes agent imports are unavailable in this install."})
        turn.mark_done("error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — turn must always close cleanly
        logger.exception("ui_chat: turn failed for session %s", turn.session_id)
        turn.publish("error", {"code": "INTERNAL", "message": str(exc) or exc.__class__.__name__})
        turn.mark_done("error", error=str(exc))
    finally:
        try:
            db.release_session_turn_lease(turn.session_id, holder)
        except Exception:
            logger.debug("ui_chat: lease release failed (already free?)", exc_info=True)
        turn.agent = None


# ── SSE formatting / generators ───────────────────────────────────────────

def _format_sse(event: str, seq: int, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\nid: {seq}\ndata: {payload}\n\n"


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def _sse_generator(turn: Turn) -> AsyncIterator[str]:
    last = 0
    try:
        while True:
            batch, done = await asyncio.to_thread(turn.snapshot_after, last, SSE_HEARTBEAT_S)
            for seq, event, data in batch:
                yield _format_sse(event, seq, data)
                last = seq
            if done and last >= turn.seq:
                return
            if not batch:
                yield ": ping\n\n"
    except asyncio.CancelledError:
        # Client disconnected — the turn keeps running in its worker thread
        # and remains reachable via GET /api/chat/stream (replay + tail).
        raise


async def _replay_generator(turn: Turn, after: int) -> AsyncIterator[str]:
    last = after
    try:
        while True:
            batch, done = await asyncio.to_thread(turn.snapshot_after, last, SSE_HEARTBEAT_S)
            for seq, event, data in batch:
                yield _format_sse(event, seq, data)
                last = seq
            if done and last >= turn.seq:
                return
            if not batch:
                yield ": ping\n\n"
    except asyncio.CancelledError:
        raise


# ── Envelope helpers ──────────────────────────────────────────────────────

def _ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data}, status_code=status)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status_code=status)


# ── Route handlers ────────────────────────────────────────────────────────

async def _handle_sessions_list(request: Request) -> Response:
    db = _session_db()
    try:
        rows = db.list_sessions_rich(
            source="webui",
            order_by_last_active=True,
            compact_rows=True,
            include_archived=False,
            limit=SESSION_LIST_LIMIT,
        )
    except Exception as exc:
        logger.warning("ui_chat: session list failed: %s", exc)
        return _error(500, "INTERNAL", "Failed to list sessions")
    sessions = [_serialize_session(dict(row)) for row in rows]
    return _ok({"sessions": sessions})


async def _handle_sessions_create(request: Request) -> Response:
    db = _session_db()
    session_id = _create_session(db, model=_resolve_model(), profile=_ui_profile())
    return _ok({"session_id": session_id, "title": ""})


async def _handle_session_messages(request: Request) -> Response:
    session_id = request.path_params.get("session_id", "")
    if not session_id:
        return _error(400, "BAD_REQUEST", "session_id is required")
    db = _session_db()
    if not _session_exists(db, session_id):
        return _error(404, "NOT_FOUND", "session not found")
    try:
        rows = db.get_messages(session_id, include_compacted=True, limit=MESSAGE_PAGE_LIMIT)
    except Exception as exc:
        logger.warning("ui_chat: message read failed: %s", exc)
        return _error(500, "INTERNAL", "Failed to load messages")
    messages = [_serialize_message(dict(row)) for row in rows]
    return _ok({"messages": messages})


async def _handle_chat_post(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return _error(400, "BAD_REQUEST", "Request body must be JSON")
    if not isinstance(body, dict):
        return _error(400, "BAD_REQUEST", "Request body must be a JSON object")
    message = str(body.get("message") or "").strip()
    if not message:
        return _error(400, "BAD_REQUEST", "message is required and must be non-empty")
    session_id = str(body.get("session_id") or "").strip() or None
    profile = str(body.get("profile") or "").strip() or None
    effective_profile = profile or _ui_profile()
    model = _resolve_model()

    db = _session_db()
    if session_id is None:
        session_id = _create_session(db, model=model, profile=effective_profile)
    elif not _session_exists(db, session_id):
        return _error(404, "NOT_FOUND", "session not found")

    if _active_turn_count() >= _max_concurrent():
        return _error(429, "RATE_LIMITED", "Too many concurrent chat turns")

    holder = f"webui-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    if not db.try_acquire_session_turn_lease(session_id, holder, ttl_seconds=300.0):
        return _error(409, "TURN_IN_PROGRESS", "A turn is already running for this session")

    turn = Turn(session_id=session_id, turn_id=f"t-{uuid.uuid4().hex[:12]}", holder=holder)
    _register_turn(turn)
    turn.publish("meta", {
        "session_id": session_id,
        "title": "",
        "ts": time.time(),
        "turn_id": turn.turn_id,
    })

    threading.Thread(
        target=_run_turn,
        args=(turn,),
        kwargs={
            "message": message,
            "profile": effective_profile,
            "model": model,
            "db": db,
            "holder": holder,
        },
        daemon=True,
        name=f"ui-chat-{session_id[:8]}",
    ).start()

    return StreamingResponse(_sse_generator(turn), media_type="text/event-stream", headers=_SSE_HEADERS)


async def _handle_chat_stream(request: Request) -> Response:
    session_id = request.query_params.get("session_id", "")
    turn_id = request.query_params.get("turn_id", "")
    if not session_id:
        return _error(400, "BAD_REQUEST", "session_id is required")
    try:
        after = max(0, int(request.query_params.get("after", "0")))
    except ValueError:
        after = 0
    turn = _get_turn(session_id, turn_id or None)
    if turn is None:
        return _error(404, "TURN_NOT_FOUND", "No replay buffer for this turn; re-read the thread")
    return StreamingResponse(_replay_generator(turn, after), media_type="text/event-stream", headers=_SSE_HEADERS)


async def _handle_chat_stop(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return _error(400, "BAD_REQUEST", "Request body must be JSON")
    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        return _error(400, "BAD_REQUEST", "session_id is required")
    turn = _active_turn(session_id)
    if turn is None:
        return _ok({"stopped": False})
    turn.cancel_requested = True
    agent = turn.agent
    if agent is not None and hasattr(agent, "interrupt"):
        try:
            agent.interrupt(hard_cancel=True)
        except Exception:
            logger.debug("ui_chat: interrupt call failed", exc_info=True)
    return _ok({"stopped": True})


# ── Route registration ────────────────────────────────────────────────────

def ui_chat_routes() -> List[Route]:
    """Chat + sessions routes composed by ``ui_api.routes()``."""
    return [
        Route("/api/sessions", _handle_sessions_list, methods=["GET"]),
        Route("/api/sessions", _handle_sessions_create, methods=["POST"]),
        Route("/api/sessions/{session_id}/messages", _handle_session_messages, methods=["GET"]),
        Route("/api/chat", _handle_chat_post, methods=["POST"]),
        Route("/api/chat/stream", _handle_chat_stream, methods=["GET"]),
        Route("/api/chat/stop", _handle_chat_stop, methods=["POST"]),
    ]
