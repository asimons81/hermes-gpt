import asyncio
import json
import os
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
    monkeypatch.setenv(server.ENABLE_VISION_ENV, "1")
    monkeypatch.setenv(server.ENABLE_WEB_ENV, "1")

    names = tool_names(server.build_server())

    assert "hermes_write_file" in names
    assert "hermes_patch" in names
    assert "hermes_run_command" in names
    assert "hermes_session_search" in names
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
    monkeypatch.setenv(
        "HERMES_HOME", r"C:\Users\asimo\AppData\Local\hermes\profiles\hermes-senior-engineer"
    )
    assert server._default_hermes_root() == Path(r"C:\Users\asimo\AppData\Local\hermes")
    assert server._hermes_root_for_operator() == Path(r"C:\Users\asimo\AppData\Local\hermes")


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
