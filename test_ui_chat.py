"""Backend tests for the chat bridge (ui_chat.py) — no live model.

The agent turn is stubbed: ``ui_chat._build_agent`` is replaced with a stub
agent that drives the real stream callbacks (``_make_stream_callbacks``), so
the SSE event encoding, lease handling, stop plumbing, replay buffer, and
persistence are all exercised end-to-end without any LLM call.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

# The chat UI resolves ``hermes_state`` lazily (ui_chat._session_db). The repo
# ships a SessionDB-compatible shim (hermes_state.py) for environments without
# a Hermes Agent checkout (CI, packaged installs) — the same resolution order
# applies here, with the repo dir on sys.path below. Do NOT insert the real
# agent source root at collection time: it makes ``hermes_cli`` importable in
# the pytest process and lets fleet tests read the invoking machine's real
# Hermes config (audit t_9d200636 Class A).

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ui_chat  # noqa: E402


# ── Test doubles ──────────────────────────────────────────────────────────

class StubAgent:
    """Minimal AIAgent double that emits events through the real callbacks.

    ``block_event`` (optional) makes the stub block inside ``run_conversation``
    until the event is set or the agent is interrupted — used to keep a turn
    alive while the test asserts lease conflicts and stop behavior.
    """

    def __init__(self, callbacks, *, db, session_id, block_event=None):
        self._callbacks = callbacks
        self.db = db
        self.session_id = session_id
        self.interrupted = threading.Event()
        self.block_event = block_event

    def interrupt(self, message=None, *, hard_cancel=False):
        self.interrupted.set()

    def run_conversation(self, user_message, task_id=None, **kwargs):
        on_token, on_reasoning, on_tool_start, on_tool_complete = self._callbacks
        self.db.append_message(self.session_id, "user", content=user_message)
        if self.block_event is not None:
            while not self.interrupted.is_set() and not self.block_event.is_set():
                self.block_event.wait(0.1)
            if self.interrupted.is_set():
                return {"interrupted": True, "failed": False, "final_response": ""}
        on_tool_start("call_1", "hermes_search_files", {"pattern": "test", "path": "."})
        time.sleep(0.01)
        on_tool_complete("call_1", "hermes_search_files", {}, "3 matches")
        self.db.append_message(
            self.session_id, "assistant",
            content="Hello world",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "hermes_search_files", "arguments": "{}"}}],
            finish_reason="end_turn",
        )
        for chunk in ("Hello ", "world"):
            if self.interrupted.is_set():
                return {"interrupted": True, "failed": False, "final_response": "Hello "}
            on_token(chunk)
            time.sleep(0.01)
        return {"interrupted": False, "failed": False, "final_response": "Hello world"}


def install_stub_agent(monkeypatch, **agent_kwargs):
    captured = {}

    def _stub_build_agent(*, turn, db, model, profile):
        callbacks = ui_chat._make_stream_callbacks(turn)
        agent = StubAgent(callbacks, db=db, session_id=turn.session_id, **agent_kwargs)
        captured["agent"] = agent
        return agent

    monkeypatch.setattr(ui_chat, "_build_agent", _stub_build_agent)
    return captured


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_ui_env(monkeypatch, tmp_path):
    """Point HERMES_HOME at a temp dir and reset module state per test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(ui_chat.UI_PROFILE_ENV, raising=False)
    monkeypatch.delenv(ui_chat.UI_TOOL_PREVIEW_BYTES_ENV, raising=False)
    monkeypatch.delenv(ui_chat.UI_MAX_CONCURRENT_ENV, raising=False)
    ui_chat._session_db_instance = None
    with ui_chat._turns_lock:
        ui_chat._turns.clear()
    yield
    with ui_chat._turns_lock:
        ui_chat._turns.clear()
    ui_chat._session_db_instance = None


@pytest.fixture()
def app():
    return Starlette(routes=ui_chat.ui_chat_routes())


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def _wait_for_turn(session_id: str, timeout: float = 5.0) -> None:
    """Poll the turn registry until the handler registered the turn."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with ui_chat._turns_lock:
            if session_id in ui_chat._turns:
                return
        time.sleep(0.05)
    raise AssertionError(f"turn for {session_id} never started")


def _parse_sse(text: str):
    """Parse SSE text into [(event, seq, data_dict), ...]."""
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = seq = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("id: "):
                seq = int(line[len("id: "):])
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
            elif line.startswith(": ping"):
                continue
        if event is not None:
            import json as _json

            events.append((event, seq, _json.loads("".join(data_lines))))
    return events


def _create_session_via_api(client) -> str:
    resp = client.post("/api/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    return body["data"]["session_id"]


# ── Sessions ──────────────────────────────────────────────────────────────

def test_session_create_and_list(client):
    sid = _create_session_via_api(client)
    assert sid

    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    sessions = resp.json()["data"]["sessions"]
    assert any(s["session_id"] == sid for s in sessions)
    found = next(s for s in sessions if s["session_id"] == sid)
    assert found["profile"] == "default"
    assert found["message_count"] == 0


def test_session_messages_resume(client):
    db = ui_chat._session_db()
    sid = "deadbeefsession1"
    db.create_session(sid, "webui", model="test-model", profile_name=None)
    db.append_message(sid, "user", content="hello")
    db.append_message(sid, "assistant", content="hi there", finish_reason="end_turn")

    resp = client.get(f"/api/sessions/{sid}/messages")
    assert resp.status_code == 200
    messages = resp.json()["data"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "hi there"
    assert messages[1]["finish_reason"] == "end_turn"
    assert messages[1]["interrupted"] is False


def test_session_messages_unknown_404(client):
    resp = client.get("/api/sessions/doesnotexist/messages")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_tool_rows_are_truncated_and_redacted(client, monkeypatch):
    monkeypatch.setenv(ui_chat.UI_TOOL_PREVIEW_BYTES_ENV, "64")
    db = ui_chat._session_db()
    sid = "tool-session-1"
    db.create_session(sid, "webui")
    long_result = "sk-abcdef0123456789 " + "x" * 500
    db.append_message(sid, "assistant", content="using tools", tool_calls='[{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]')
    db.append_message(sid, "tool", content=long_result, tool_call_id="c1", tool_name="read_file")

    resp = client.get(f"/api/sessions/{sid}/messages")
    messages = resp.json()["data"]["messages"]
    tool_msg = next(m for m in messages if m["role"] == "tool")
    assert "sk-" not in tool_msg["tool_result"]
    assert "[REDACTED]" in tool_msg["tool_result"]
    assert len(tool_msg["tool_result"]) <= 64 + len("\n…[truncated]")


# ── Chat streaming ────────────────────────────────────────────────────────

def test_chat_stream_full_sequence(client, monkeypatch):
    install_stub_agent(monkeypatch)
    with client.stream("POST", "/api/chat", json={"message": "hello"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        text = "".join(resp.iter_text())

    events = _parse_sse(text)
    event_names = [e[0] for e in events]
    assert event_names[0] == "meta"
    assert "token" in event_names
    assert "tool_start" in event_names
    assert "tool_end" in event_names
    assert "message_complete" in event_names
    assert event_names[-1] == "done"

    meta = events[0][2]
    assert meta["session_id"]
    assert meta["turn_id"].startswith("t-")
    done = events[-1][2]
    assert done["finish_reason"] == "end_turn"
    assert done["message_id"] is not None

    # turn persisted: thread re-read shows user + assistant rows
    sid = meta["session_id"]
    resp = client.get(f"/api/sessions/{sid}/messages")
    roles = [m["role"] for m in resp.json()["data"]["messages"]]
    assert "user" in roles and "assistant" in roles

    # ids are monotonically increasing for replay
    seqs = [e[1] for e in events]
    assert seqs == sorted(seqs)
    assert seqs[0] == 1


def test_chat_creates_lazy_session(client, monkeypatch):
    install_stub_agent(monkeypatch)
    with client.stream("POST", "/api/chat", json={"message": "hi"}) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    events = _parse_sse(text)
    assert events[0][0] == "meta"
    sid = events[0][2]["session_id"]
    resp = client.get("/api/sessions")
    assert any(s["session_id"] == sid for s in resp.json()["data"]["sessions"])


def test_chat_unknown_session_404(client, monkeypatch):
    install_stub_agent(monkeypatch)
    resp = client.post("/api/chat", json={"session_id": "nope", "message": "hi"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_chat_busy_session_returns_409(client, app, monkeypatch):
    # TestClient buffers the whole streaming body inside ``__enter__``, so the
    # first (never-ending) stream runs in a background thread while the main
    # thread asserts the lease conflict.
    hold = threading.Event()
    install_stub_agent(monkeypatch, block_event=hold)
    sid = _create_session_via_api(client)
    result: dict = {}

    def _stream():
        with client.stream("POST", "/api/chat", json={"session_id": sid, "message": "first"}) as resp:
            result["status"] = resp.status_code
            result["text"] = "".join(resp.iter_text())

    stream_thread = threading.Thread(target=_stream, daemon=True)
    stream_thread.start()
    try:
        _wait_for_turn(sid)
        # the stub is blocked, so the lease is still held — second send → 409
        resp = client.post("/api/chat", json={"session_id": sid, "message": "second"})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "TURN_IN_PROGRESS"
    finally:
        hold.set()
        stream_thread.join(timeout=15)
    assert result.get("status") == 200
    assert not stream_thread.is_alive()


def test_chat_stop_interrupts_turn(client, monkeypatch):
    hold = threading.Event()
    install_stub_agent(monkeypatch, block_event=hold)
    sid = _create_session_via_api(client)
    result: dict = {}

    def _stream():
        with client.stream("POST", "/api/chat", json={"session_id": sid, "message": "go"}) as resp:
            result["status"] = resp.status_code
            result["text"] = "".join(resp.iter_text())

    stream_thread = threading.Thread(target=_stream, daemon=True)
    stream_thread.start()
    try:
        _wait_for_turn(sid)
        # let the stub reach its block loop, then interrupt
        time.sleep(0.2)
        resp = client.post("/api/chat/stop", json={"session_id": sid})
        assert resp.status_code == 200
        assert resp.json()["data"]["stopped"] is True
    finally:
        hold.set()
        stream_thread.join(timeout=15)
    assert result.get("status") == 200
    events = _parse_sse(result.get("text", ""))
    assert events[-1][0] == "done"
    assert events[-1][2]["finish_reason"] == "interrupted"


def test_reconnect_replays_buffer(client, monkeypatch):
    install_stub_agent(monkeypatch)
    with client.stream("POST", "/api/chat", json={"message": "replay me"}) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    events = _parse_sse(text)
    meta = events[0][2]
    sid, turn_id = meta["session_id"], meta["turn_id"]

    # Reconnect: replay from after=0 → the same buffered events come back.
    resp = client.get(f"/api/chat/stream?session_id={sid}&turn_id={turn_id}&after=0")
    assert resp.status_code == 200
    replay = _parse_sse(resp.text)
    assert [e[1] for e in replay] == [e[1] for e in events]

    # Replay after the final seq → no events (turn already done).
    resp = client.get(f"/api/chat/stream?session_id={sid}&turn_id={turn_id}&after={events[-1][1]}")
    assert resp.status_code == 200
    assert _parse_sse(resp.text) == []


def test_reconnect_unknown_turn_404(client):
    resp = client.get("/api/chat/stream?session_id=abc&turn_id=t-xyz&after=0")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TURN_NOT_FOUND"


def test_stop_unknown_session_noop(client):
    resp = client.post("/api/chat/stop", json={"session_id": "nonexistent"})
    assert resp.status_code == 200
    assert resp.json()["data"]["stopped"] is False


# ── Redaction / boundaries ────────────────────────────────────────────────

def test_tool_brief_masks_prompt_content():
    brief = ui_chat._safe_tool_brief(
        "hermes_search_files",
        {"pattern": "needle", "prompt": "find my secret plan", "path": "/tmp"},
    )
    assert "find my secret plan" not in brief
    assert "prompt=…" in brief
    assert '"needle"' in brief
    assert '"/tmp"' in brief


def test_redact_text_masks_credentials():
    assert "sk-abcdef0123456789" not in ui_chat._redact_text("key sk-abcdef0123456789 here")
    assert "[REDACTED]" in ui_chat._redact_text("key sk-abcdef0123456789 here")
    assert ui_chat._redact_text("Bearer abcdefghijklmnopqrstuvwxyz123456") == "[REDACTED]"
    assert "/home/u/.hermes/secrets/token.json" not in ui_chat._redact_text("path /home/u/.hermes/secrets/token.json")


def test_error_envelope_shape():
    import json as _json

    resp = ui_chat._error(409, "TURN_IN_PROGRESS", "busy")
    assert resp.status_code == 409
    body = _json.loads(resp.body)
    assert body == {"ok": False, "error": {"code": "TURN_IN_PROGRESS", "message": "busy"}}
