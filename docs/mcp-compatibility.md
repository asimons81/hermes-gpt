# MCP Compatibility

- Status: current (v0.7.0, Flight Deck)
- Owner: default (implementation: developer profile)
- Verified: 2026-08-15 against `mcp` 1.28.1 (`mcp.shared.version`)

This manifest pins and verifies the Model Context Protocol surface that
`hermes-gpt` exposes. It is the source of truth for protocol claims; the
compatibility tests in `test_mcp_compat.py` assert it against the installed
SDK at test time.

## Minimum supported protocol revision

**2024-11-05** (`2024-11-05`).

The server negotiates the highest mutually supported revision at `initialize`
time and rejects unsupported revisions. Because the minimum is the oldest
revision the installed SDK supports, every client that speaks at least
2024-11-05 can interoperate.

## Supported revisions (installed SDK)

The verified `mcp` SDK (1.28.x) declares these protocol revisions:

| Revision | Notes |
|---|---|
| `2024-11-05` | Minimum supported; base tools/list + tools/call surface |
| `2025-03-26` | Added pagination (`nextCursor`) |
| `2025-06-18` | Added `roots/list`, tool annotations, structured output schema |
| `2025-11-25` | Latest supported revision (authorization metadata era) |

`mcp.shared.version.LATEST_PROTOCOL_VERSION` is `2025-11-25` at the verified
version. `test_mcp_compat.py` asserts `2024-11-05` and `2025-11-25` are in the
running SDK's `SUPPORTED_PROTOCOL_VERSIONS` so a future SDK floor regression
fails the suite.

## Transport matrix

| Transport | Path | Notes |
|---|---|---|
| stdio | — | Default local mode (`hermes-gpt` or `python server.py`) |
| Streamable HTTP | `/mcp` | Enabled with `--http`; transport security host/origin allowlist |
| Legacy SSE | `/sse` (plus `/messages/`) | Retained for older clients |

OpenAI Secure MCP Tunnel is an external private bridge, not a fourth Hermes GPT server transport. For the recommended Hermes setup, `tunnel-client` reaches `http://127.0.0.1:4750/mcp` locally over the existing Streamable HTTP transport and carries those MCP requests through an outbound-only OpenAI tunnel. See [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md).

Server transport security (`TransportSecuritySettings`) enforces an explicit
host/origin allowlist: loopback by default plus `HERMES_GPT_ALLOWED_HOSTS`
extensions and the OAuth issuer when configured. Public unauthenticated
hosting is unsupported (product invariant).

## Trusted-client authentication metadata

Every tool advertises its security scheme via MCP tool metadata
(`securitySchemes`), driven by `server.tool_meta()`:

| Config | Advertised scheme |
|---|---|
| OAuth configured (`HERMES_GPT_OAUTH_*`) | `oauth2` with the configured scope |
| Static bearer (`HERMES_GPT_BEARER_TOKEN`) | `http` / `bearer` |
| Neither | `noauth` (loopback / trusted-proxy only) |

For Secure MCP Tunnel, the baseline local hop can remain loopback/noauth while OpenAI tunnel identity and Hermes Operator policy protect separate layers. Static bearer can be added as local-hop defense in depth. Built-in OAuth requires separate browser-facing authorization-server reachability because the authorization server itself is not automatically tunneled.

## Binary embedded tool results

`hermes_export_file` returns a direct MCP `CallToolResult` containing safe structured metadata and `EmbeddedResource(BlobResourceContents)` for authorized file bytes. This uses the normal `tools/call` response content union; it is not a new transport and does not require a separate resource-read endpoint.

The file-export surface requires Operator `workspace` authority plus a non-empty `HERMES_GPT_OPERATOR_ALLOWED_PATHS`; see [Binary file export](file-export.md) for the complete confinement, size, extension, denied-path, and audit contract.

The MCP specification leaves rendering of embedded resources to the client. Hermes GPT guarantees the protocol-native blob representation and does not claim that ChatGPT, Codex, or another client will always render it as a downloadable attachment. No text/base64 fallback is emitted.

## Version advertisement

The `initialize` handshake advertises the hermes-gpt app version in
`serverInfo.version` (from `versioning.VERSION`, currently `0.7.0`) — not the
MCP SDK version. This lets a client detect a stale process that is still
exposing an old schema. `test_mcp_compat.py::test_initialize_advertises_server_version`
asserts the handshake reports `versioning.VERSION` and that the pinned floor
(`2024-11-05`) remains negotiable on the running SDK.

Client notes:

- **ChatGPT (chatgpt.com connector)**: uses the OAuth metadata to drive the
  ChatGPT connector flow; loopback redirect required. For private developer-mode access without a public Hermes GPT hostname, see [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md).
- **Codex CLI**: uses stdio or streamable HTTP with the configured scheme;
  curated tool names (`hermes_extract_page` vs `hermes_web_extract`) are
  documented in `docs/codex.md`.
- **Any client showing an old or incomplete tool list**: compare
  `serverInfo.version` against the expected release, then refresh the client's
  cached tool list. See [docs/updating.md](updating.md) for the check-first
  update and cache-refresh behavior; it is the canonical guide and is not
  duplicated here.

## Package floor

`pyproject.toml` keeps `mcp[cli]>=1.0,<2` as the metadata floor (SDK 1.x
family). The exact verified version is a test-time fact, not a hard pin, so
the floor cannot drift silently: `test_mcp_compat.py` fails if the installed
SDK drops `2024-11-05` or `2025-11-25` support.
