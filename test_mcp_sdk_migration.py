"""Exercise both real server surfaces at the SDK/wire boundary."""

import importlib
import json

import pytest
from starlette.testclient import TestClient

from versioning import VERSION


@pytest.mark.parametrize("surface", ["main", "codex"])
@pytest.mark.parametrize("protocol", ["2024-11-05", "2025-11-25"])
def test_http_sdk_compatibility(surface, protocol, monkeypatch, tmp_path):
    # Import within the test so the original v1-only import is an explicit
    # regression failure under SDK 2, rather than a collection failure.
    try:
        server = importlib.import_module("server")
    except ImportError as exc:
        pytest.fail(f"Hermes GPT must load with the installed MCP SDK: {exc}")
    monkeypatch.setenv("HERMES_GPT_ENABLE_CODEX", "1")
    monkeypatch.setenv("HERMES_GPT_ENABLE_MCP", "1")
    monkeypatch.setenv("HERMES_GPT_CODEX_TOOLSET", "operator")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if surface == "main":
        built = server.build_server(http=True)
        app = server.build_asgi_app(built, http=True)
        policy_tool = "hermes_operator_policy"
    else:
        built = server.build_codex_mcp_server(http=True)
        app = built.streamable_http_app()
        policy_tool = "hermes_operator_policy"

    with TestClient(app, base_url="http://127.0.0.1:7677") as client:
        headers = {"Accept": "application/json, text/event-stream"}
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": protocol, "capabilities": {},
                       "clientInfo": {"name": "sdk-regression", "version": "1"}},
        })
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert "mcp-session-id" not in response.headers
        initialized = response.json()["result"]
        assert initialized["serverInfo"] == {"name": "hermes-gpt", "version": VERSION}
        assert initialized["protocolVersion"] == protocol
        headers["MCP-Protocol-Version"] = protocol
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        assert response.status_code == 200, response.text
        tools = response.json()["result"]["tools"]
        assert policy_tool in {t["name"] for t in tools}
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": policy_tool, "arguments": {}},
        })
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError", False), result
        assert result["content"]
        # The real RPC handler must still refuse a direct write in the
        # default read-only posture, including through the Codex alias.
        write_tool = "hermes_skill_create" if surface == "main" else "hermes_operator_skill_create"
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": write_tool, "arguments": {
                "name": "sdk-must-not-create", "content": "# test", "dry_run": False,
            }},
        })
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError", False), result
        refusal = json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))
        assert refusal["success"] is False
        assert refusal["error"].startswith("Operator mode is disabled.")
        assert not (tmp_path / "skills" / "sdk-must-not-create").exists()
        # An explicit Host outside the default loopback boundary stays denied.
        response = client.post("/mcp", headers={**headers, "Host": "untrusted.example"}, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {},
        })
        assert response.status_code == 421
        response = client.post("/mcp", headers={**headers, "Origin": "https://untrusted.example"}, json={
            "jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {},
        })
        assert response.status_code == 403


@pytest.mark.parametrize("surface", ["main", "codex"])
def test_sdk2_stateless_request_without_initialize(surface, monkeypatch):
    from mcp_compat import SDK_V2

    if not SDK_V2:
        pytest.skip("2026-07-28 protocol requires SDK 2")
    import server

    monkeypatch.setenv("HERMES_GPT_ENABLE_CODEX", "1")
    monkeypatch.setenv("HERMES_GPT_ENABLE_MCP", "1")
    built = server.build_server(http=True) if surface == "main" else server.build_codex_mcp_server(http=True)
    app = server.build_asgi_app(built, http=True) if surface == "main" else built.streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:7677") as client:
        response = client.post("/mcp", headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "MCP-Method": "tools/list",
        }, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }},
        })
        assert response.status_code == 200, response.text
        assert "mcp-session-id" not in response.headers
        tools = response.json()["result"]["tools"]
        expected = "hermes_operator_policy" if surface == "main" else "hermes_status"
        assert expected in {t["name"] for t in tools}


@pytest.mark.parametrize("surface", ["main", "codex"])
def test_sse_rejects_untrusted_host_and_origin(surface):
    import server

    built = server.build_server() if surface == "main" else server.build_codex_mcp_server()
    app = server.build_asgi_app(built, http=False) if surface == "main" else built.sse_app()
    # SSE writes the denial response, then raises to close the stream.
    # Assert what the HTTP client receives rather than re-raising server errors.
    with TestClient(app, base_url="http://127.0.0.1:7677", raise_server_exceptions=False) as client:
        response = client.get("/sse", headers={"Host": "untrusted.example"})
        assert response.status_code == 421
        response = client.get("/sse", headers={"Origin": "https://untrusted.example"})
        assert response.status_code == 403
