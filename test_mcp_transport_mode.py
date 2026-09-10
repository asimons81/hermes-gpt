from __future__ import annotations


def test_http_defaults_to_stateless_json_transport(monkeypatch):
    for name in (
        "HERMES_GPT_OAUTH_ENABLE",
        "HERMES_GPT_OAUTH_ISSUER",
        "HERMES_GPT_OAUTH_CLIENT_ID",
        "HERMES_GPT_OAUTH_CLIENT_SECRET",
        "HERMES_GPT_OAUTH_REDIRECT_URI",
        "HERMES_GPT_OAUTH_SCOPE",
    ):
        monkeypatch.delenv(name, raising=False)

    import server

    mcp = server.build_server(http=True)
    from starlette.testclient import TestClient

    with TestClient(server.build_asgi_app(mcp, http=True), base_url="http://127.0.0.1:7677") as client:
        response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert "mcp-session-id" not in response.headers
        assert response.json()["result"]["tools"]
