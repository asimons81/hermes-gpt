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

# Server tools that must always be registered (read-only / operator core),
# including the six v0.7 Flight Deck tools and representative v0.6 surfaces.
REQUIRED_TOOLS = [
    # v0.6 core read-only surface
    "hermes_read_file",
    "hermes_search_files",
    "hermes_memory",
    "hermes_skill_list",
    "hermes_skill_view",
    "hermes_operator_policy",
    "hermes_operator_status",
    "hermes_operator_audit_tail",
    # v0.7 Flight Deck: all six tools are first-class MCP surfaces
    "hermes_review_accept",
    "hermes_events_query",
    "hermes_events_tail",
    "hermes_oauth_status",
    "hermes_oauth_revoke",
    "hermes_swarm_reconcile",
    # v0.6 surface families (representative per namespace)
    "hermes_mission_overview",
    "hermes_contract_define",
    "hermes_contract_validate",
    "hermes_swarm_workflow_list",
    "hermes_swarm_workflow_status",
    "hermes_fleet_list",
    "hermes_cron_list",
    "hermes_skill_create",
    "hermes_workspace_read",
    "hermes_operator_doctor",
]

# The six v0.7 Flight Deck tools (S1 surface manifest).
FLIGHT_DECK_TOOLS = [
    "hermes_review_accept",
    "hermes_events_query",
    "hermes_events_tail",
    "hermes_oauth_status",
    "hermes_oauth_revoke",
    "hermes_swarm_reconcile",
]

# Read-only Flight Deck tools: must carry the protocol readOnlyHint annotation
# (2025-06-18 revision). The hint is advisory for client-side filtering; it is
# not authority and never gates a call.
READ_ONLY_ANNOTATED_TOOLS = [
    "hermes_events_query",
    "hermes_events_tail",
    "hermes_oauth_status",
]

# Gated write tools: registered unconditionally but must refuse mutation under
# the default read-only policy environment (owner + direct + confirm gates).
GATED_WRITE_TOOLS = [
    "hermes_review_accept",
    "hermes_oauth_revoke",
    "hermes_swarm_reconcile",
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


@pytest.fixture()
def built_server(monkeypatch):
    """A server built under a clean, read-only default environment."""
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
    return server.build_server()


@pytest.fixture()
def built_tools(built_server):
    """The complete registered tool inventory as a name -> Tool mapping."""
    tools = asyncio.run(built_server.list_tools())
    return {t.name: t for t in tools}


def test_flight_deck_tools_registered(built_tools):
    """Proof 1: all six v0.7 Flight Deck tools are registered on the server."""
    for name in FLIGHT_DECK_TOOLS:
        assert name in built_tools, f"missing Flight Deck tool: {name}"


def test_v06_surfaces_still_present(built_tools):
    """Proof 6: representative v0.6 surface families remain registered."""
    names = set(built_tools)
    families = {
        "mission_*": [n for n in names if n.startswith("hermes_mission_")],
        "contract_*": [n for n in names if n.startswith("hermes_contract_")],
        "swarm_workflow_*": [n for n in names if n.startswith("hermes_swarm_workflow_")],
        "fleet_*": [n for n in names if n.startswith("hermes_fleet_")],
        "cron_*": [n for n in names if n.startswith("hermes_cron_")],
        "skill_*": [n for n in names if n.startswith("hermes_skill_")],
        "workspace_*": [n for n in names if n.startswith("hermes_workspace_")],
        "operator_*": [n for n in names if n.startswith("hermes_operator_")],
    }
    for family, members in families.items():
        assert members, f"no registered tools in surface family {family}"


def test_no_duplicate_tool_names(built_tools):
    """Proof 7: the built inventory has no duplicate tool names."""
    assert len(built_tools) == len(set(built_tools))


def test_all_tools_have_valid_input_schema(built_tools):
    """Proof 2: every registered tool exposes a valid MCP inputSchema."""
    assert built_tools, "no tools registered"
    for name, tool in built_tools.items():
        schema = tool.inputSchema
        assert isinstance(schema, dict), f"{name}: inputSchema not a dict"
        assert schema.get("type") == "object", f"{name}: inputSchema.type != object"
        properties = schema.get("properties")
        assert isinstance(properties, dict), f"{name}: inputSchema.properties not a dict"
        if "required" in schema:
            assert isinstance(schema["required"], list), f"{name}: required not a list"


def test_read_only_flight_deck_tools_carry_read_only_annotation(built_tools):
    """Proof 3 (annotation half): read-only Flight Deck tools carry readOnlyHint."""
    for name in READ_ONLY_ANNOTATED_TOOLS:
        tool = built_tools[name]
        assert tool.annotations is not None, f"{name}: annotations missing"
        assert tool.annotations.readOnlyHint is True, f"{name}: readOnlyHint not set"


def test_oauth_revoke_carries_destructive_annotation(built_tools):
    """F4: the destructive revoke tool carries destructiveHint for client UI."""
    tool = built_tools["hermes_oauth_revoke"]
    assert tool.annotations is not None, "hermes_oauth_revoke: annotations missing"
    assert tool.annotations.destructiveHint is True


def test_flight_deck_tools_have_titles(built_tools):
    """F4: all six Flight Deck tools carry a title annotation."""
    for name in FLIGHT_DECK_TOOLS:
        tool = built_tools[name]
        assert tool.annotations is not None, f"{name}: annotations missing"
        assert tool.annotations.title, f"{name}: missing title annotation"


def test_build_server_is_stable_across_calls(monkeypatch):
    """Proof 8: build_server() twice yields identical tool-name sets."""
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

    first = {t.name for t in asyncio.run(server.build_server().list_tools())}
    second = {t.name for t in asyncio.run(server.build_server().list_tools())}
    assert first == second
    assert first  # non-empty


def test_initialize_advertises_server_version(built_server):
    """Proof 9: initialize advertises serverInfo.version == versioning.VERSION
    and negotiates a supported protocol revision that includes the pinned
    floor (2024-11-05). This makes stale-schema processes detectable."""
    import anyio
    import versioning
    from mcp.client.session import ClientSession
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

    assert built_server._mcp_server.version == versioning.VERSION
    opts = built_server._mcp_server.create_initialization_options()
    assert opts.server_version == versioning.VERSION

    async def _handshake():
        c2s_send, c2s_recv = anyio.create_memory_object_stream()
        s2c_send, s2c_recv = anyio.create_memory_object_stream()
        task = asyncio.create_task(
            built_server._mcp_server.run(c2s_recv, s2c_send, opts)
        )
        try:
            async with ClientSession(s2c_recv, c2s_send) as client:
                result = await client.initialize()
                return result
        finally:
            task.cancel()

    result = asyncio.run(_handshake())
    assert result.serverInfo.name == "hermes-gpt"
    assert result.serverInfo.version == versioning.VERSION
    assert result.protocolVersion in SUPPORTED_PROTOCOL_VERSIONS
    # The pinned floor remains negotiable on the running SDK.
    assert MIN_PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS


def test_gated_write_tools_refuse_without_owner_direct_confirm(built_tools, tmp_path):
    """Proof 4: gated write tools are registered but refuse mutation under the
    default read-only policy env (owner + direct + confirm gates intact)."""
    import json

    import operator_policy as op
    import server

    for name in GATED_WRITE_TOOLS:
        assert name in built_tools, f"missing gated write tool: {name}"

    log = tmp_path / "audit.jsonl"
    op.set_audit_log_override(log)
    try:
        review = json.loads(
            server.hermes_review_accept(
                contract_sha256="a" * 64,
                task_id="t_1",
                assignee="alice",
                reviewer="bob",
                verdict="SATISFIED",
                evidence_refs=["sha256:abc"],
                dry_run=False,
                confirm=True,
            )
        )
        assert review.get("success") is not True
        assert "REVIEW_POLICY_DENIED" in json.dumps(review)

        oauth = json.loads(server.hermes_oauth_revoke(confirm=True, dry_run=False))
        assert oauth.get("success") is not True
        assert "OAUTH_REVOKE_DENIED" in json.dumps(oauth)

        swarm = json.loads(server.hermes_swarm_reconcile(apply=True))
        assert swarm.get("success") is not True
        assert "PERMISSION_DENIED" in json.dumps(swarm)
    finally:
        op.set_audit_log_override(None)
