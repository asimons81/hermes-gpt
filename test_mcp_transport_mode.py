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
    assert mcp.settings.stateless_http is True
    assert mcp.settings.json_response is True
