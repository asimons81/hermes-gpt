import asyncio
import json
import os
import sqlite3
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import server


GATE_ENVS = [
    server.ENABLE_WRITE_ENV,
    server.ENABLE_MEMORY_WRITE_ENV,
    server.ENABLE_SESSION_SEARCH_ENV,
    server.ENABLE_SESSION_CONTROL_ENV,
    server.ENABLE_TERMINAL_ENV,
    server.ENABLE_VISION_ENV,
    server.ENABLE_WEB_ENV,
    server.UNSAFE_REMOTE_ENV,
]


def clear_gate_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in GATE_ENVS:
        monkeypatch.delenv(name, raising=False)


def tool_names(mcp_server) -> list[str]:
    tools = asyncio.run(mcp_server.list_tools())
    return sorted(tool.name for tool in tools)


def tools_by_name(mcp_server):
    tools = asyncio.run(mcp_server.list_tools())
    return {tool.name: tool for tool in tools}


def test_default_tool_surface_is_read_or_local_metadata_only(monkeypatch):
    clear_gate_envs(monkeypatch)

    built = server.build_server()
    names = tool_names(built)

    # Original read-only / local-metadata tools must still be present.
    for required in [
        "hermes_memory",
        "hermes_read_file",
        "hermes_search_files",
        "hermes_skill_list",
        "hermes_skill_view",
    ]:
        assert required in names

    # Broad mutating tools must NOT be exposed without their env flags.
    for forbidden in [
        "hermes_write_file",
        "hermes_patch",
        "hermes_run_command",
        "hermes_session_search",
        "hermes_session_continue",
        "hermes_session_send",
        "hermes_session_job_status",
        "hermes_session_job_result",
        "hermes_vision_analyze",
        "hermes_web_search",
        "hermes_web_extract",
    ]:
        assert forbidden not in names

    # Operator / Owner Mode tools are always registered (with refusal when
    # the policy is disabled). Verify the core read-only + representative
    # mutating tools are present.
    for operator_tool in [
        "hermes_operator_policy",
        "hermes_operator_status",
        "hermes_operator_audit_tail",
        "hermes_operator_doctor",
        "hermes_operator_snapshot",
        "hermes_release_doctor",
        "hermes_operator_recover",
        "hermes_cron_list",
        "hermes_cron_status",
        "hermes_skill_diff",
        "hermes_config_get",
        "hermes_env_status",
        "hermes_gateway_status",
        "hermes_git_status",
        "hermes_git_diff",
        "hermes_cron_run",
        "hermes_cron_create",
        "hermes_skill_create",
        "hermes_owner_run_command",
    ]:
        assert operator_tool in names

    for tool in tools_by_name(built).values():
        assert tool.meta == {"securitySchemes": [{"type": "noauth"}]}


def test_env_gates_expose_high_risk_tools(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(server.ENABLE_WRITE_ENV, "1")
    monkeypatch.setenv(server.ENABLE_TERMINAL_ENV, "1")
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(server.ENABLE_VISION_ENV, "1")
    monkeypatch.setenv(server.ENABLE_WEB_ENV, "1")

    names = tool_names(server.build_server())

    assert "hermes_write_file" in names
    assert "hermes_patch" in names
    assert "hermes_run_command" in names
    assert "hermes_session_search" in names
    assert "hermes_session_continue" in names
    assert "hermes_session_send" in names
    assert "hermes_session_job_status" in names
    assert "hermes_session_job_result" in names
    assert "hermes_vision_analyze" in names
    assert "hermes_web_search" in names
    assert "hermes_web_extract" in names


def test_memory_write_actions_are_disabled_by_default(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(
        server,
        "memory_tool",
        SimpleNamespace(memory_tool=lambda **kwargs: "should not be called"),
    )

    with pytest.raises(RuntimeError, match=server.ENABLE_MEMORY_WRITE_ENV):
        server.hermes_memory(action="add", target="memory", content="x")


def test_memory_search_remains_available(monkeypatch):
    clear_gate_envs(monkeypatch)
    captured = {}

    def fake_memory_tool(**kwargs):
        captured.update(kwargs)
        return "memory search ok"

    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "memory_tool", SimpleNamespace(memory_tool=fake_memory_tool))

    assert server.hermes_memory(action="search", target="memory") == "memory search ok"
    assert captured["action"] == "search"


def test_terminal_direct_call_is_disabled_by_default(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(
        server,
        "terminal_tool",
        SimpleNamespace(terminal_tool=lambda **kwargs: "should not be called"),
    )

    with pytest.raises(RuntimeError, match=server.ENABLE_TERMINAL_ENV):
        server.hermes_run_command("echo nope")


def test_terminal_timeout_is_capped_when_enabled(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(server.ENABLE_TERMINAL_ENV, "1")
    captured = {}

    def fake_terminal_tool(command, timeout=None, workdir=None):
        captured.update({"command": command, "timeout": timeout, "workdir": workdir})
        return "ok"

    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "terminal_tool", SimpleNamespace(terminal_tool=fake_terminal_tool))

    assert server.hermes_run_command("echo ok", timeout=999) == "ok"
    assert captured["timeout"] == 120


def test_vision_analyze_is_disabled_by_default(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "vision_tool", SimpleNamespace(
        vision_analyze_tool=lambda **kwargs: "should not be called",
    ))

    with pytest.raises(RuntimeError, match=server.ENABLE_VISION_ENV):
        server.hermes_vision_analyze(image_url="https://example.com/img.jpg")


def test_web_search_is_disabled_by_default(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "web_tool", SimpleNamespace(
        web_search_tool=lambda **kwargs: "should not be called",
    ))

    with pytest.raises(RuntimeError, match=server.ENABLE_WEB_ENV):
        server.hermes_web_search(query="test")


def test_web_extract_is_disabled_by_default(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "web_tool", SimpleNamespace(
        web_extract_tool=lambda **kwargs: "should not be called",
    ))

    with pytest.raises(RuntimeError, match=server.ENABLE_WEB_ENV):
        server.hermes_web_extract(urls=["https://example.com"])


def test_web_search_proxies_to_web_tool_when_enabled(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(server.ENABLE_WEB_ENV, "1")
    captured = {}

    def fake_web_search(**kwargs):
        captured.update(kwargs)
        return "search results"

    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "web_tool", SimpleNamespace(
        web_search_tool=fake_web_search,
    ))

    result = server.hermes_web_search(query="hello world", limit=10)
    assert result == "search results"
    assert captured["query"] == "hello world"
    assert captured["limit"] == 10


def test_web_extract_proxies_to_web_tool_when_enabled(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(server.ENABLE_WEB_ENV, "1")
    captured = {}
    import asyncio

    async def fake_web_extract(**kwargs):
        captured.update(kwargs)
        return "extracted content"

    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "web_tool", SimpleNamespace(
        web_extract_tool=fake_web_extract,
    ))

    result = server.hermes_web_extract(urls=["https://example.com"])
    assert result == "extracted content"
    assert captured["urls"] == ["https://example.com"]


def test_vision_analyze_proxies_to_vision_tool_when_enabled(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(server.ENABLE_VISION_ENV, "1")
    captured = {}
    import asyncio

    async def fake_vision(**kwargs):
        captured.update(kwargs)
        return '{"analysis": "a cat"}'

    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "vision_tool", SimpleNamespace(
        vision_analyze_tool=fake_vision,
    ))

    result = server.hermes_vision_analyze(
        image_url="https://example.com/cat.jpg",
        question="What is this?",
    )
    assert result == '{"analysis": "a cat"}'
    assert captured["image_url"] == "https://example.com/cat.jpg"
    assert captured["user_prompt"] == "What is this?"


def test_vision_analyze_defaults_prompt_when_question_empty(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(server.ENABLE_VISION_ENV, "1")
    captured = {}
    import asyncio

    async def fake_vision(**kwargs):
        captured.update(kwargs)
        return '{"analysis": "a landscape"}'

    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "vision_tool", SimpleNamespace(
        vision_analyze_tool=fake_vision,
    ))

    result = server.hermes_vision_analyze(image_url="https://example.com/landscape.jpg")
    assert result == '{"analysis": "a landscape"}'
    assert "Describe this image in detail." in captured["user_prompt"]


def test_remote_profile_requires_explicit_unsafe_ack(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["server.py", "--http", "--profile", "remote"])

    with pytest.raises(SystemExit, match="Remote profile requires real authentication"):
        server.main()


def test_default_hermes_root_normalizes_profile_scoped_env(monkeypatch):
    if sys.platform == "win32":
        hermes_home = r"C:\Users\asimo\AppData\Local\hermes\profiles\hermes-senior-engineer"
        expected = Path(r"C:\Users\asimo\AppData\Local\hermes")
    else:
        hermes_home = "/home/asimo/.hermes/profiles/hermes-senior-engineer"
        expected = Path("/home/asimo/.hermes")
    monkeypatch.setenv("HERMES_HOME", hermes_home)
    assert server._default_hermes_root() == expected
    assert server._hermes_root_for_operator() == expected


class _Phase1FakeConnection:
    def __init__(self):
        self.close_calls = 0
        self.executed = []

    def close(self):
        self.close_calls += 1

    def execute(self, *args, **kwargs):
        self.executed.append((args, kwargs))
        raise AssertionError("dispose_safely must not execute SQL or PRAGMA")


class _Phase1FakeSessionDB:
    def __init__(self, connection, *, message_rows=None, session_rows=None, fts_enabled=False):
        self._conn = connection
        self._fts_enabled = fts_enabled
        self._trigram_available = False
        self.close_calls = 0
        self.calls = []
        self.message_rows = list(message_rows or [])
        self.session_rows = list(session_rows or [])

    def close(self):
        self.close_calls += 1
        raise AssertionError("SessionDB.close() must not be called")

    def list_sessions_rich(self, **kwargs):
        self.calls.append(("list_sessions_rich", kwargs))
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", len(self.session_rows))
        return list(self.session_rows[offset:offset + limit])

    def resolve_session_id(self, value):
        self.calls.append(("resolve_session_id", value))
        if value == "prefix":
            return "session-1"
        return None if value in {"missing", "ambiguous"} else value

    def get_messages(self, session_id, **kwargs):
        self.calls.append(("get_messages", {"session_id": session_id, **kwargs}))
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", len(self.message_rows))
        return list(self.message_rows[offset:offset + limit])

    def search_messages(self, **kwargs):
        self.calls.append(("search_messages", kwargs))
        return list(self.message_rows)

    def export_session(self, value):
        self.calls.append(("export_session", value))
        return None

    def export_session_lineage(self, value):
        self.calls.append(("export_session_lineage", value))
        return None


def _phase1_adapter_factory(fake_db):
    captured = {}

    def factory(**kwargs):
        captured["kwargs"] = kwargs
        return fake_db

    return factory, captured


def test_phase1_adapter_opens_read_only_and_disposes_raw_connection_once():
    connection = _Phase1FakeConnection()
    fake_db = _Phase1FakeSessionDB(connection)
    factory, captured = _phase1_adapter_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=_Phase1FakeConnection,
    ).open()

    assert captured["kwargs"] == {"read_only": True}
    adapter.dispose_safely()
    adapter.dispose_safely()
    assert connection.close_calls == 1
    assert fake_db.close_calls == 0
    assert fake_db._conn is connection
    assert fake_db._fts_enabled is False
    assert fake_db._trigram_available is False
    assert connection.executed == []


def test_phase1_adapter_disposes_on_exception_path():
    connection = _Phase1FakeConnection()
    fake_db = _Phase1FakeSessionDB(connection)
    factory, _ = _phase1_adapter_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=_Phase1FakeConnection,
    ).open()

    try:
        raise ValueError("C:\\Users\\example\\secret-token=sk-test-value")
    except ValueError as exc:
        adapter.dispose_safely()
        assert "[REDACTED_PATH]" in server._redact_error(exc)
    assert connection.close_calls == 1


def test_phase1_adapter_context_manager_disposes_on_success_and_exception():
    success_connection = _Phase1FakeConnection()
    success_db = _Phase1FakeSessionDB(success_connection)
    success_factory, _ = _phase1_adapter_factory(success_db)
    with server.ReadOnlySessionAdapter(
        db_factory=success_factory,
        connection_type=_Phase1FakeConnection,
    ) as adapter:
        assert adapter is not None
    assert success_connection.close_calls == 1

    error_connection = _Phase1FakeConnection()
    error_db = _Phase1FakeSessionDB(error_connection)
    error_factory, _ = _phase1_adapter_factory(error_db)
    with pytest.raises(RuntimeError, match="expected"):
        with server.ReadOnlySessionAdapter(
            db_factory=error_factory,
            connection_type=_Phase1FakeConnection,
        ):
            raise RuntimeError("expected")
    assert error_connection.close_calls == 1


def test_phase1_adapter_uses_only_verified_public_data_methods():
    connection = _Phase1FakeConnection()
    fake_db = _Phase1FakeSessionDB(connection)
    factory, _ = _phase1_adapter_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=_Phase1FakeConnection,
    ).open()

    assert adapter.list_sessions(limit=3, offset=4) == []
    assert adapter.resolve_session_id("session-1") == "session-1"
    assert adapter.get_messages("session-1", limit=5, offset=6) == []
    assert adapter.export_session("session-1") is None
    assert adapter.export_session_lineage("session-1") is None
    assert [name for name, _ in fake_db.calls] == [
        "list_sessions_rich",
        "resolve_session_id",
        "get_messages",
        "export_session",
        "export_session_lineage",
    ]
    assert fake_db.calls[0][1]["compact_rows"] is True


@pytest.mark.parametrize("value", [-1, -10])
def test_phase1_bounds_reject_negative_values(value):
    with pytest.raises(ValueError):
        server._validate_limit(value, "limit", server.MAX_PAGE_SIZE)
    with pytest.raises(ValueError):
        server._validate_offset(value)


def test_phase1_bounds_enforce_maxima_and_ids():
    assert server._validate_limit(server.MAX_PAGE_SIZE, "limit", server.MAX_PAGE_SIZE) == server.MAX_PAGE_SIZE
    assert server._validate_offset(server.MAX_OFFSET) == server.MAX_OFFSET
    with pytest.raises(ValueError):
        server._validate_limit(server.MAX_PAGE_SIZE + 1, "limit", server.MAX_PAGE_SIZE)
    with pytest.raises(ValueError):
        server._validate_offset(server.MAX_OFFSET + 1)
    with pytest.raises(ValueError):
        server._validate_session_id("")
    with pytest.raises(ValueError):
        server._validate_session_id("x" * (server.MAX_ID_LENGTH + 1))
    assert server._validate_query("  query  ") == "query"
    with pytest.raises(ValueError):
        server._validate_query("")
    with pytest.raises(ValueError):
        server._validate_query("q" * (server.MAX_QUERY_LENGTH + 1))


def test_phase1_elevated_content_gate_and_default_roles(monkeypatch):
    monkeypatch.delenv(server.ENABLE_SESSION_INTERNAL_CONTENT_ENV, raising=False)
    assert server._allowed_message_roles() == {"user", "assistant"}
    with pytest.raises(RuntimeError, match=server.ENABLE_SESSION_INTERNAL_CONTENT_ENV):
        server._allowed_message_roles(include_system_messages=True)
    with pytest.raises(RuntimeError, match=server.ENABLE_SESSION_INTERNAL_CONTENT_ENV):
        server._allowed_message_roles(include_tool_messages=True)
    monkeypatch.setenv(server.ENABLE_SESSION_INTERNAL_CONTENT_ENV, "1")
    assert server._allowed_message_roles(
        include_system_messages=True,
        include_tool_messages=True,
    ) == {"user", "assistant", "system", "tool", "function"}


def test_phase1_projection_and_redaction_helpers():
    metadata = server._safe_session_metadata({
        "id": "session-1",
        "source": "cli",
        "started_at": 1.0,
        "ended_at": 2.0,
        "message_count": 3,
        "title": "private title",
        "system_prompt": "do not expose",
        "cwd": r"C:\Users\example\private",
    })
    assert metadata["id"] == "session-1"
    assert "system_prompt" not in metadata
    assert "cwd" not in metadata
    assert metadata["has_title"] is True

    assert server._safe_message(
        {"id": 1, "session_id": "session-1", "role": "system", "content": "secret"},
        {"user", "assistant"},
    ) is None
    message = server._safe_message(
        {"id": 2, "session_id": "session-1", "role": "user", "content": "token=sk-test-value"},
        {"user", "assistant"},
    )
    assert message["session_id"] == "session-1"
    assert "***" not in message["content"]


def test_phase1_safe_message_cannot_bypass_internal_gate(monkeypatch):
    monkeypatch.delenv(server.ENABLE_SESSION_INTERNAL_CONTENT_ENV, raising=False)
    with pytest.raises(RuntimeError, match=server.ENABLE_SESSION_INTERNAL_CONTENT_ENV):
        server._safe_message(
            {"role": "system", "session_id": "session-1", "content": "hidden"},
            {"system"},
        )


def test_phase1_adapter_projects_raw_rows_before_returning(monkeypatch):
    connection = _Phase1FakeConnection()
    fake_db = _Phase1FakeSessionDB(
        connection,
        message_rows=[
            {
                "id": 1,
                "session_id": "session-1",
                "role": "system",
                "content": "hidden",
                "system_prompt": "secret",
            },
            {
                "id": 2,
                "session_id": "session-1",
                "role": "user",
                "content": "token=sk-test-value",
                "tool_calls": "private",
            },
        ],
    )
    factory, _ = _phase1_adapter_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=_Phase1FakeConnection,
    ).open()

    rows = adapter.get_messages("session-1", limit=10, offset=0)
    assert rows == [{
        "id": 2,
        "session_id": "session-1",
        "role": "user",
        "content": "token=[REDACTED]",
    }]
    assert "system_prompt" not in rows[0]
    assert "tool_calls" not in rows[0]

    monkeypatch.setenv(server.ENABLE_SESSION_INTERNAL_CONTENT_ENV, "1")
    rows = adapter.get_messages(
        "session-1",
        limit=10,
        offset=0,
        include_system_messages=True,
    )
    assert {row["role"] for row in rows} == {"system", "user"}


def test_phase1_adapter_lifecycle_edge_cases():
    class NoConnectionDB:
        pass

    class NonSqliteConnectionDB:
        _conn = object()

    for fake_db in (NoConnectionDB(), NonSqliteConnectionDB()):
        factory, _ = _phase1_adapter_factory(fake_db)
        adapter = server.ReadOnlySessionAdapter(db_factory=factory).open()
        adapter.dispose_safely()
        adapter.dispose_safely()

    class RaisingConnection(_Phase1FakeConnection):
        def close(self):
            self.close_calls += 1
            raise OSError("C:\\Users\\example\\private-token=sk-test-value")

    raising_connection = RaisingConnection()
    raising_db = _Phase1FakeSessionDB(raising_connection)
    factory, _ = _phase1_adapter_factory(raising_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=RaisingConnection,
    ).open()
    adapter.dispose_safely()
    adapter.dispose_safely()
    assert raising_connection.close_calls == 1
    assert raising_db.close_calls == 0


def test_phase1_adapter_open_failure_is_redacted():
    def failing_factory(**kwargs):
        raise OSError("C:\\Users\\example\\private-token=sk-test-value")

    adapter = server.ReadOnlySessionAdapter(db_factory=failing_factory)
    with pytest.raises(RuntimeError) as exc_info:
        adapter.open()
    assert "[REDACTED_PATH]" in str(exc_info.value)
    assert "sk-test-value" not in str(exc_info.value)
    adapter.dispose_safely()
    adapter.dispose_safely()


def test_phase1_recursive_redaction_covers_nested_values_and_safe_ids():
    value = {
        "session_id": "session-1",
        "nested": {
            "api_key": "provider-secret-value",
            "items": [
                "C:\\Users\\example\\private.txt",
                "/home/example/private.txt",
                ("https://example.test/?api_key=provider-secret-value", "sk-test-provider-key-value-123456"),
            ],
        },
    }
    redacted = server._redact_value(value)
    assert redacted["session_id"] == "session-1"
    assert redacted["nested"]["api_key"] == "[REDACTED]"
    assert redacted["nested"]["items"][0] == "[REDACTED_PATH]"
    assert redacted["nested"]["items"][1] == "[REDACTED_PATH]"
    assert "provider-secret-value" not in json.dumps(redacted)
    assert "sk-test-provider-key" not in json.dumps(redacted)


def test_phase1_bool_validation_and_archived_forwarding():
    with pytest.raises(ValueError):
        server._validate_bool(1, "include_archived")
    connection = _Phase1FakeConnection()
    fake_db = _Phase1FakeSessionDB(connection)
    factory, _ = _phase1_adapter_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=_Phase1FakeConnection,
    ).open()
    adapter.list_sessions(limit=20, offset=0, include_archived=True)
    assert fake_db.calls[0][1]["include_archived"] is True


def test_phase2_tools_are_gated_and_registered(monkeypatch):
    clear_gate_envs(monkeypatch)
    names = tool_names(server.build_server())
    assert "hermes_session_search" not in names
    assert "hermes_session_list" not in names
    assert "hermes_session_read" not in names

    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    names = tool_names(server.build_server())
    assert "hermes_session_search" in names
    assert "hermes_session_list" in names
    assert "hermes_session_read" in names
    assert "hermes_session_export" in names
    assert "hermes_session_lineage_export" not in names


def test_session_continue_resolves_id_before_runner_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(server, "require_imports", lambda: None)
    connection = sqlite3.connect(":memory:")
    fake_db = _Phase1FakeSessionDB(connection)
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "_default_hermes_root", lambda: tmp_path)
    dispatched = {}

    def fake_continue(session_id, prompt, timeout, **kwargs):
        dispatched.update(
            session_id=session_id, prompt=prompt, timeout=timeout, **kwargs
        )
        return {"success": True, "job_id": "a" * 32, "status": "running"}

    monkeypatch.setattr(server.op_session, "hermes_session_continue", fake_continue)
    result = server.hermes_session_continue("prefix", "continue safely", timeout=123)

    assert result["success"] is True
    assert dispatched["session_id"] == "session-1"
    assert dispatched["prompt"] == "continue safely"
    assert dispatched["timeout"] == 123
    assert dispatched["hermes_root"] == tmp_path
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")


def test_phase2_session_list_projects_metadata_and_paginates(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    connection = sqlite3.connect(":memory:")
    fake_db = _Phase1FakeSessionDB(
        connection,
        session_rows=[
            {
                "id": "session-1",
                "source": "cli",
                "started_at": 1.0,
                "ended_at": 2.0,
                "last_active": 2.0,
                "message_count": 3,
                "tool_call_count": 1,
                "title": "private",
                "preview": "private content",
                "system_prompt": "hidden",
                "cwd": r"C:\\Users\\example\\private",
            },
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)

    result = json.loads(server.hermes_session_list(limit=20, offset=0))
    assert result["success"] is True
    assert result["offset"] == 0
    assert result["returned_count"] == 1
    assert result["has_more"] is False
    assert result["sessions"][0]["id"] == "session-1"
    assert result["sessions"][0]["has_title"] is True
    assert "title" not in result["sessions"][0]
    assert "preview" not in result["sessions"][0]
    assert "system_prompt" not in result["sessions"][0]
    assert "cwd" not in result["sessions"][0]
    assert fake_db.calls[0][1]["include_archived"] is False
    assert fake_db.close_calls == 0
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")


def test_phase2_session_read_filters_and_resolves_ids(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    connection = sqlite3.connect(":memory:")
    fake_db = _Phase1FakeSessionDB(
        connection,
        message_rows=[
            {"id": 1, "session_id": "session-1", "role": "user", "timestamp": 1, "content": "hello"},
            {"id": 2, "session_id": "session-1", "role": "assistant", "timestamp": 2, "content": "world"},
            {"id": 3, "session_id": "session-1", "role": "tool", "timestamp": 3, "content": "hidden"},
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)

    result = json.loads(server.hermes_session_read("session-1", limit=2, offset=0))
    assert result["success"] is True
    assert result["session_id"] == "session-1"
    assert {row["role"] for row in result["messages"]} == {"user", "assistant"}
    assert result["returned_count"] == 2
    assert result["next_offset"] == 2
    assert result["has_more"] is True
    assert result["truncated"] is False
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")

    prefix_result = json.loads(server.hermes_session_read("prefix", limit=1))
    assert prefix_result["success"] is True
    assert prefix_result["session_id"] == "session-1"

    monkeypatch.setenv(server.ENABLE_SESSION_INTERNAL_CONTENT_ENV, "1")
    elevated = json.loads(server.hermes_session_read("session-1", include_tool_messages=True))
    assert {row["role"] for row in elevated["messages"]} == {"user", "assistant", "tool"}


def test_phase2_session_read_denies_internal_roles_without_gate(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.delenv(server.ENABLE_SESSION_INTERNAL_CONTENT_ENV, raising=False)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    result = json.loads(server.hermes_session_read("session-1", include_tool_messages=True))
    assert result["success"] is False
    assert result["error"]["code"] == "SESSION_READ_FAILED"
    assert server.ENABLE_SESSION_INTERNAL_CONTENT_ENV in result["error"]["message"]


def test_phase2_session_read_rejects_missing_and_ambiguous_ids(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.setattr(server, "require_imports", lambda: None)
    connection = _Phase1FakeConnection()
    fake_db = _Phase1FakeSessionDB(connection)
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    for value in ("missing", "ambiguous"):
        result = json.loads(server.hermes_session_read(value))
        assert result["success"] is False
        assert result["error"]["code"] == "SESSION_ID_NOT_FOUND_OR_AMBIGUOUS"


def test_phase2_response_size_is_enforced():
    huge = [{"id": 1, "session_id": "s", "role": "user", "content": "x" * (server.MAX_RESPONSE_BYTES + 1)}]
    result = json.loads(server._session_page_response("messages", huge, offset=0, requested_limit=1))
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= server.MAX_RESPONSE_BYTES
    assert result["success"] is False or result["truncated"] is True


@pytest.mark.parametrize(
    "call",
    [
        lambda: server.hermes_session_list(limit=101),
        lambda: server.hermes_session_list(offset=server.MAX_OFFSET + 1),
        lambda: server.hermes_session_read("session-1", limit=101),
        lambda: server.hermes_session_read("session-1", offset=server.MAX_OFFSET + 1),
    ],
)
def test_phase2_tool_bounds_fail_closed(monkeypatch, call):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.setattr(server, "require_imports", lambda: None)
    result = json.loads(call())
    assert result["success"] is False


def test_phase3_filtered_role_pagination_advances_by_examined_rows():
    connection = _Phase1FakeConnection()
    fake_db = _Phase1FakeSessionDB(
        connection,
        message_rows=[
            {"id": 1, "session_id": "s", "role": "system", "content": "hidden"},
            {"id": 2, "session_id": "s", "role": "user", "content": "one"},
            {"id": 3, "session_id": "s", "role": "assistant", "content": "two"},
            {"id": 4, "session_id": "s", "role": "user", "content": "three"},
        ],
    )
    factory, _ = _phase1_adapter_factory(fake_db)
    adapter = server.ReadOnlySessionAdapter(
        db_factory=factory,
        connection_type=_Phase1FakeConnection,
    ).open()

    first = adapter.get_messages_page("s", limit=2, offset=0)
    second = adapter.get_messages_page("s", limit=2, offset=first["next_offset"])
    assert [row["id"] for row in first["messages"]] == [2, 3]
    assert first["rows_examined"] == 3
    assert first["next_offset"] == 3
    assert [row["id"] for row in second["messages"]] == [4]
    assert second["next_offset"] == 4
    assert second["has_more"] is False


def test_phase3_session_export_json_and_markdown_without_files(monkeypatch, tmp_path):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    connection = sqlite3.connect(":memory:")
    fake_db = _Phase1FakeSessionDB(
        connection,
        message_rows=[
            {"id": 1, "session_id": "session-1", "role": "user", "timestamp": 1, "content": "hello"},
            {"id": 2, "session_id": "session-1", "role": "assistant", "timestamp": 2, "content": "world"},
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    before = list(tmp_path.iterdir())

    exported_json = json.loads(server.hermes_session_export("session-1", format="json", limit=2))
    assert exported_json["success"] is True
    assert exported_json["format"] == "json"
    assert [row["content"] for row in exported_json["messages"]] == ["hello", "world"]

    exported_markdown = server.hermes_session_export("session-1", format="markdown", limit=2)
    assert exported_markdown.startswith("# Hermes session export")
    assert "hello" in exported_markdown
    assert "world" in exported_markdown
    assert list(tmp_path.iterdir()) == before


def test_phase3_export_limits_truncation_and_lineage_fail_closed(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.setattr(server, "require_imports", lambda: None)
    too_many = json.loads(server.hermes_session_export("session-1", limit=server.MAX_EXPORT_MESSAGES + 1))
    assert too_many["success"] is False

    connection = sqlite3.connect(":memory:")
    fake_db = _Phase1FakeSessionDB(
        connection,
        message_rows=[
            {"id": 1, "session_id": "session-1", "role": "user", "content": "x" * server.MAX_RESPONSE_BYTES},
        ],
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    truncated = json.loads(server.hermes_session_export("session-1", format="json", limit=1))
    assert len(json.dumps(truncated, ensure_ascii=False).encode("utf-8")) <= server.MAX_RESPONSE_BYTES
    assert truncated["success"] is False or truncated["truncated"] is True

    calls_before_lineage = list(fake_db.calls)
    lineage = json.loads(server.hermes_session_export("session-1", include_lineage=True))
    assert lineage["success"] is False
    assert lineage["error"]["code"] == "SESSION_LINEAGE_EXPORT_UNAVAILABLE"
    assert fake_db.calls == calls_before_lineage


def test_phase3_export_elevated_content_denial_and_approval(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_SEARCH_ENV, "1")
    monkeypatch.delenv(server.ENABLE_SESSION_INTERNAL_CONTENT_ENV, raising=False)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    denied = json.loads(server.hermes_session_export("session-1", include_tool_messages=True))
    assert denied["success"] is False

    monkeypatch.setenv(server.ENABLE_SESSION_INTERNAL_CONTENT_ENV, "1")
    connection = _Phase1FakeConnection()
    fake_db = _Phase1FakeSessionDB(
        connection,
        message_rows=[{"id": 1, "session_id": "session-1", "role": "tool", "content": "tool result"}],
    )
    factory, _ = _phase1_adapter_factory(fake_db)
    monkeypatch.setattr(server, "SessionDB", factory)
    approved = json.loads(server.hermes_session_export("session-1", include_tool_messages=True))
    assert approved["success"] is True
    assert approved["messages"][0]["role"] == "tool"


def test_phase3_search_plain_text_compatibility_and_cleanup(monkeypatch):
    connection = sqlite3.connect(":memory:")
    fake_db = _Phase1FakeSessionDB(
        connection,
        message_rows=[{"id": 1, "session_id": "session-1", "role": "user", "snippet": "hello\nworld"}],
        fts_enabled=True,
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    result = server.hermes_session_search("hello")
    assert result == "- session-1 [user] hello world"
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")


def test_phase3_search_legacy_content_fallback(monkeypatch):
    connection = sqlite3.connect(":memory:")
    fake_db = _Phase1FakeSessionDB(
        connection,
        message_rows=[{"id": 1, "session_id": "session-1", "role": "user", "content": "legacy content"}],
        fts_enabled=True,
    )
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: fake_db)
    monkeypatch.setattr(server, "require_imports", lambda: None)
    assert server.hermes_session_search("legacy") == "- session-1 [user] legacy content"


def test_phase3_search_fts_unavailable_guidance(monkeypatch):
    class NoSearchDB:
        def __init__(self, connection):
            self._conn = connection

    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(server, "SessionDB", lambda **kwargs: NoSearchDB(connection))
    monkeypatch.setattr(server, "require_imports", lambda: None)
    result = server.hermes_session_search("hello")
    assert "unavailable" in result.lower()
    assert "fts" in result.lower()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("select 1")


def test_phase1_utf8_response_bytes():
    assert server._utf8_response_bytes("abc") == 3
    assert server._utf8_response_bytes("é") == 2
    assert server._utf8_response_bytes("🙂") == 4


def test_phase1_existing_tool_surface_remains_unchanged(monkeypatch):
    clear_gate_envs(monkeypatch)
    names = tool_names(server.build_server())
    assert "hermes_session_search" not in names
    assert "hermes_read_file" in names
    assert "hermes_search_files" in names


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(
    not os.environ.get("HERMES_HTTP_TEST"),
    reason="HTTP smoke test requires a running HTTP server; "
    "set HERMES_HTTP_TEST=1 to run against a real server",
)
def test_http_initialize_smoke(monkeypatch):
    port = free_port()
    env = os.environ.copy()
    for name in GATE_ENVS:
        env.pop(name, None)

    proc = subprocess.Popen(
        [sys.executable, "server.py", "--http", "--host", "127.0.0.1", "--port", str(port)],
        cwd=os.path.dirname(__file__),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.time() + 10
        last_error = None
        response_text = None
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        }
        data = json.dumps(payload).encode("utf-8")
        while time.time() < deadline:
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/mcp",
                    data=data,
                    method="POST",
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    response_text = response.read().decode("utf-8")
                    break
            except Exception as exc:
                last_error = exc
                time.sleep(0.25)
        if response_text is None:
            raise AssertionError(f"HTTP MCP server did not respond: {last_error}")

        parsed = json.loads(response_text)
        assert parsed["result"]["serverInfo"]["name"] == "hermes-gpt"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
