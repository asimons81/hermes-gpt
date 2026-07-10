import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import codex_core
import codex_mcp


def test_codex_mcp_registry_is_curated_and_complete():
    core = codex_core.CodexToolCore(
        version="test",
        imports_ready=lambda: True,
        gateway_snapshot=lambda: {"gateway": {"running": False}},
        gateway_diagnostics_callback=lambda: {},
        vision_analyze=lambda path, prompt: {},
        web_search=lambda query, limit: {},
        web_extract=lambda urls, limit: {},
        cron_create_callback=lambda schedule, prompt, dry_run: {},
        skill_create_callback=lambda name, content, dry_run: {},
    )
    server = codex_mcp.build_codex_server(core)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {
        "hermes_status", "hermes_capabilities", "hermes_vision_analyze",
        "hermes_web_search", "hermes_extract_page", "hermes_plan",
        "hermes_author_skill", "hermes_cron_plan", "hermes_cron_create",
        "hermes_gateway_diagnostics",
    }
    for tool in asyncio.run(server.list_tools()):
        assert tool.meta == {"securitySchemes": [{"type": "noauth"}]}


def _readline_with_timeout(stream, seconds: float = 8.0) -> str:
    result: list[str] = []
    worker = threading.Thread(target=lambda: result.append(stream.readline()), daemon=True)
    worker.start()
    worker.join(seconds)
    if not result:
        raise TimeoutError("No MCP stdio response arrived in time.")
    return result[0]


def test_codex_stdio_initialize_list_and_safe_tool_call():
    root = Path(__file__).resolve().parent
    proc = subprocess.Popen(
        [sys.executable, "server.py", "mcp"],
        cwd=root,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "HERMES_GPT_ENABLE_CODEX": "1", "HERMES_GPT_ENABLE_MCP": "1"},
    )

    def send(payload):
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}})
        initialized = json.loads(_readline_with_timeout(proc.stdout))
        assert initialized["result"]["serverInfo"]["name"] == "hermes-gpt"
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = json.loads(_readline_with_timeout(proc.stdout))
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert {"hermes_status", "hermes_capabilities", "hermes_plan"}.issubset(names)
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hermes_capabilities", "arguments": {}}})
        called = json.loads(_readline_with_timeout(proc.stdout))
        assert called["result"]["isError"] is False
        assert called["result"]["structuredContent"]["ok"] is True
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "hermes_status", "arguments": {}}})
        status = json.loads(_readline_with_timeout(proc.stdout))
        assert status["result"]["isError"] is False
        assert "gateway" in status["result"]["structuredContent"]
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
