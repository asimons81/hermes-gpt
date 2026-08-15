# MCP Compatibility

- Status: current (v0.7.0, Flight Deck)
- Owner: hermes-dev
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

Client notes:

- **ChatGPT (chatgpt.com connector)**: uses the OAuth metadata to drive the
  ChatGPT connector flow; loopback redirect required.
- **Codex CLI**: uses stdio or streamable HTTP with the configured scheme;
  curated tool names (`hermes_extract_page` vs `hermes_web_extract`) are
  documented in `docs/codex.md`.

## Package floor

`pyproject.toml` keeps `mcp[cli]>=1.0,<2` as the metadata floor (SDK 1.x
family). The exact verified version is a test-time fact, not a hard pin, so
the floor cannot drift silently: `test_mcp_compat.py` fails if the installed
SDK drops `2024-11-05` or `2025-11-25` support.
