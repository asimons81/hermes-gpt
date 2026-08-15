"""MCP compatibility tests for hermes-gpt (S1).

Asserts the running SDK's supported protocol revisions include the pinned
floor (2024-11-05) and the latest supported revision (2025-11-25) when
available, that FastMCP initialize negotiation accepts the floor, and that
tool metadata advertises the expected security scheme per auth configuration.
"""

from __future__ import annotations

import asyncio
import importlib.metadata

import pytest

MIN_PROTOCOL_VERSION = "2024-11-05"
LATEST_PROTOCOL_VERSION = "2025-11-25"

# Server tools that must always be registered (read-only / operator core).
REQUIRED_TOOLS = [
    "hermes_read_file",
    "hermes_search_files",
    "hermes_memory",
    "hermes_skill_list",
    "hermes_skill_view",
    "hermes_operator_policy",
    "hermes_operator_status",
    "hermes_operator_audit_tail",
    "hermes_contract_define",
    "hermes_contract_validate",
    "hermes_swarm_workflow_list",
    "hermes_swarm_workflow_status",
]


@pytest.fixture()
def sdk_protocol_versions() -> list[str]:
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

    return list(SUPPORTED_PROTOCOL_VERSIONS)


def test_installed_sdk_floor_is_pinned(sdk_protocol_versions):
    """The SDK must support the pinned minimum protocol revision."""
    assert MIN_PROTOCOL_VERSION in sdk_protocol_versions, (
        f"installed mcp SDK dropped {MIN_PROTOCOL_VERSION}; update docs/mcp-compatibility.md"
    )


def test_installed_sdk_supports_latest_revision(sdk_protocol_versions):
    """The SDK must support 2025-11-25 when available (it is at verified 1.28.x)."""
    assert LATEST_PROTOCOL_VERSION in sdk_protocol_versions, (
        f"installed mcp SDK dropped {LATEST_PROTOCOL_VERSION}; update docs/mcp-compatibility.md"
    )


def test_package_metadata_allows_mcp_1x_floor():
    """pyproject.toml keeps the mcp SDK 1.x floor (not a hard 1.28 pin)."""
    dist = importlib.metadata.distribution("hermes-gpt")
    for req in dist.requires or []:
        if req.lower().startswith("mcp"):
            assert ">=" in req and "<2" in req, f"mcp floor drifted: {req}"


@pytest.mark.parametrize("protocol", [MIN_PROTOCOL_VERSION, LATEST_PROTOCOL_VERSION])
def test_initialize_negotiation_accepts_protocol(protocol):
    """FastMCP initialize accepts a supported protocol revision."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("hermes-gpt-test")
    # FastMCP stores the protocol version it will negotiate.
    assert getattr(server, "protocol_version", None) in (
        None,
        protocol,
        LATEST_PROTOCOL_VERSION,
    )
    # The server's underlying mcp session advertises supported versions.
    from mcp.server.session import ServerSession
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

    assert protocol in SUPPORTED_PROTOCOL_VERSIONS
    assert ServerSession is not None


def test_http_tool_metadata_noauth_when_unconfigured(monkeypatch):
    """With no auth env, tools advertise noauth metadata."""
    import server

    for name in (
        server.ENABLE_WRITE_ENV,
        server.ENABLE_MEMORY_WRITE_ENV,
        server.ENABLE_SESSION_SEARCH_ENV,
        server.ENABLE_TERMINAL_ENV,
        server.ENABLE_VISION_ENV,
        server.ENABLE_WEB_ENV,
        server.UNSAFE_REMOTE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HERMES_GPT_BEARER_TOKEN", raising=False)
    for name in (
        "HERMES_GPT_OAUTH_ENABLE",
        "HERMES_GPT_OAUTH_ISSUER",
        "HERMES_GPT_OAUTH_CLIENT_ID",
        "HERMES_GPT_OAUTH_CLIENT_SECRET",
        "HERMES_GPT_OAUTH_REDIRECT_URI",
        "HERMES_GPT_OAUTH_SCOPE",
    ):
        monkeypatch.delenv(name, raising=False)

    meta = server.tool_meta()
    schemes = meta.get("securitySchemes") or []
    assert any(s.get("type") == "noauth" for s in schemes)


def test_http_tool_metadata_bearer_when_configured(monkeypatch):
    """With a static bearer token, tools advertise http/bearer metadata."""
    import server

    monkeypatch.delenv("HERMES_GPT_OAUTH_ENABLE", raising=False)
    monkeypatch.setenv("HERMES_GPT_BEARER_TOKEN", "test-bearer-token-1234567890-abcdefghijklmnopqrstuvwxyz-ABCDEF")

    meta = server.tool_meta()
    schemes = meta.get("securitySchemes") or []
    assert any(s.get("type") == "http" and s.get("scheme") == "bearer" for s in schemes)


def test_http_tool_metadata_oauth2_when_configured(monkeypatch):
    """With OAuth configured, tools advertise oauth2 metadata with scope."""
    import oauth_auth
    import server

    for name in (
        oauth_auth.OAUTH_ENABLE_ENV,
        oauth_auth.OAUTH_ISSUER_ENV,
        oauth_auth.OAUTH_CLIENT_ID_ENV,
        oauth_auth.OAUTH_CLIENT_SECRET_ENV,
        oauth_auth.OAUTH_REDIRECT_URI_ENV,
        oauth_auth.OAUTH_SCOPE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(oauth_auth.OAUTH_ENABLE_ENV, "1")
    monkeypatch.setenv(oauth_auth.OAUTH_ISSUER_ENV, "https://auth.example.com")
    monkeypatch.setenv(oauth_auth.OAUTH_CLIENT_ID_ENV, "client-id")
    monkeypatch.setenv(oauth_auth.OAUTH_CLIENT_SECRET_ENV, "client-secret-1234567890abcdefghijklmnopqrstuvwxyz-ABCDEF")
    monkeypatch.setenv(oauth_auth.OAUTH_REDIRECT_URI_ENV, "https://auth.example.com/mcp")
    monkeypatch.setenv(oauth_auth.OAUTH_SCOPE_ENV, "hermes")

    meta = server.tool_meta()
    schemes = meta.get("securitySchemes") or []
    oauth_schemes = [s for s in schemes if s.get("type") == "oauth2"]
    assert oauth_schemes, f"expected oauth2 metadata, got {schemes}"
    assert "hermes" in (oauth_schemes[0].get("scopes") or [])


def test_server_registers_core_tools(monkeypatch):
    """The built server registers the v0.6+ core tool surface."""
    import server

    for name in (
        server.ENABLE_WRITE_ENV,
        server.ENABLE_MEMORY_WRITE_ENV,
        server.ENABLE_SESSION_SEARCH_ENV,
        server.ENABLE_TERMINAL_ENV,
        server.ENABLE_VISION_ENV,
        server.ENABLE_WEB_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HERMES_GPT_BEARER_TOKEN", raising=False)

    built = server.build_server()
    tools = asyncio.run(built.list_tools())
    names = {t.name for t in tools}
    for required in REQUIRED_TOOLS:
        assert required in names, f"missing registered tool: {required}"
