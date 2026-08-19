# OAuth and bearer authentication

Hermes GPT remains local-first. Remote Operator or Owner access is supported only when the HTTP endpoint is carried over HTTPS and protected with either a static bearer token or the built-in single-client OAuth boundary described here.

## Security model

The built-in authorization server is intentionally narrow:

- one statically configured confidential client;
- exact HTTPS redirect-URI allowlisting;
- client authentication on every authorization-code and refresh exchange;
- optional PKCE S256 validation when a client supplies a challenge;
- one configured Hermes resource scope (required on every issued token) plus the connector compatibility scopes `openid` and `offline_access`;
- one-hour access tokens;
- 30-day refresh tokens with rotation and replay rejection;
- signed, five-minute stateless authorization codes plus bounded process-memory replay, access-token, and refresh-token stores;
- no dynamic client registration, user accounts, persistent plaintext token database, or OpenID Provider claims.

`openid` is accepted because ChatGPT may add it even with OIDC disabled. Hermes GPT does not advertise OpenID Provider metadata and does not issue ID tokens; `/.well-known/openid-configuration` is served as a public 404 (not an auth challenge) so clients that probe OIDC discovery with OIDC disabled do not mistake the connector for disconnected. Configure the ChatGPT connector with OIDC disabled.

The client secret is the credential that prevents an arbitrary network caller from exchanging an authorization code. Public clients using token endpoint authentication method `none` are not supported. Do not expose an OAuth-enabled endpoint until a strong client secret is configured.

## Generate a client secret

Generate a fresh URL-safe secret. Do not reuse a Hermes, GitHub, gateway, or provider credential.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The result must contain 43 to 128 URL-safe characters. Store it in a service-owned secret environment source, not in the repository, shell history, documentation, or command-line arguments.

## Required OAuth configuration

```text
HERMES_GPT_OAUTH_ENABLE=1
HERMES_GPT_OAUTH_ISSUER=https://mcp.example.com
HERMES_GPT_OAUTH_CLIENT_ID=chatgpt-client
HERMES_GPT_OAUTH_CLIENT_SECRET=<43-to-128-character-generated-secret>
HERMES_GPT_OAUTH_REDIRECT_URI=https://chatgpt.com/connector/oauth/<exact-callback-id>
HERMES_GPT_OAUTH_SCOPE=hermes
```

Multiple exact redirect URIs may be comma-separated. Wildcards are not accepted. The issuer must use HTTPS except for an explicitly loopback-only test server.

The built-in OAuth boundary is available only with streamable HTTP (`--http`);
legacy SSE is rejected when OAuth is enabled so discovery and resource binding
cannot disagree.

Authenticated `remote` mode also requires one of these transport boundaries:

- direct TLS using both `--cert` and `--key`; or
- a loopback bind behind a trusted HTTPS reverse proxy, with every local proxy
  address explicitly listed in `HERMES_GPT_TRUSTED_PROXY_IPS`.

Wildcard, non-IP, and non-loopback trusted-proxy entries are rejected. Forwarded
headers are ignored unless this explicit loopback-proxy mode is active.

For a local HTTPS-terminating proxy, set:

```text
HERMES_GPT_TRUSTED_PROXY_IPS=127.0.0.1,::1
```

Then start the remote safety profile only after the configuration validates:

```bash
hermes-gpt --http --host 127.0.0.1 --port 4750 --profile remote
```

Keep the process loopback-bound and terminate HTTPS in a deliberately configured trusted proxy or private tunnel. The public issuer must resolve to that exact server.

## ChatGPT connector values

Configure the connector using values derived from the issuer:

```text
MCP URL:               https://mcp.example.com/mcp
Authorization endpoint:https://mcp.example.com/oauth/authorize
Token endpoint:        https://mcp.example.com/oauth/token
Client ID:             chatgpt-client
Client secret:         the generated confidential-client secret
Token auth method:     client_secret_post or client_secret_basic
Default scope:         hermes offline_access
OIDC:                  disabled
```

ChatGPT may add `openid` to the authorization request. `offline_access` is required for refresh-token issuance. A connector authorized before refresh discovery was available must be disconnected and connected once so discovery and authorization run again.

## Token lifecycle

An authorization-code exchange returns an access token with `expires_in=3600`. If `offline_access` was granted, it also returns a refresh token. A successful refresh returns a new access token and rotates the refresh token; replaying the old refresh token fails with `invalid_grant`.

Authorization codes are short-lived signed values. Only used-code replay state,
access tokens, and refresh tokens are held in process memory. Since v0.7,
issued access and refresh tokens are also persisted to an **encrypted durable
token store** so a server restart does not invalidate credentials:

- envelope: `<hermes_data>/secrets/hermes_gpt_tokens.json` (0600), AES-256-GCM;
- key management precedence: OS keyring (`keyring` lib) → key file
  `<hermes_data>/secrets/hermes_gpt_token_key` (0600, created on first use) →
  env `HERMES_GPT_TOKEN_MASTER_KEY` (CI/test only, weakest — documented);
- no token material is ever written to the audit log or any MCP response;
  `hermes_oauth_status` reports presence/expiry only;
- explicit revocation: `hermes_oauth_revoke` (owner + direct + confirm)
  deletes the envelope and optionally rotates the master key.

The durable store is **subject to legal review before shipping** (ADR-001,
risk R4). On hosts without a keyring service the key-file fallback keeps the
key beside the ciphertext under the same 0600 directory; treat that directory
as secret-bearing.

## Static bearer alternative

For clients that directly support a preconfigured bearer credential, set:

```text
HERMES_GPT_BEARER_TOKEN=<strong-random-token>
```

Static bearer authentication remains compatible with OAuth access tokens. Never put the bearer value in a URL, repository, log, prompt, or Operator audit record.

## Failure behavior

Hermes GPT fails closed when:

- OAuth is enabled but required configuration is missing;
- the client secret is absent, malformed, or incorrect;
- the redirect URI, resource, grant type, or scope is unsupported;
- PKCE is supplied with a method other than S256 or the verifier does not match;
- an authorization code or refresh token is unknown, expired, used, or replayed;
- a refresh request attempts to increase scope;
- a bounded credential store is full.

Operator and Owner policy remains independent from transport authentication. Authenticating a connector does not activate mutations, direct mode, or Owner Mode.
