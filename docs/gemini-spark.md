# Gemini Spark custom app

Hermes GPT's built-in OAuth boundary can authenticate Google's consumer Gemini Apps **Custom apps for Spark** connector. Google drives that flow as a manually configured confidential client because this server advertises no dynamic client registration; the opt-in **Gemini Spark client profile** registers an additional confidential client that stays isolated from the primary client (for example the ChatGPT connector client).

This guide covers the profile, both configuration patterns, callback discovery, verification, and rollback. The authorization server itself — scopes, PKCE, refresh rotation, and the durable token store — is documented in [OAuth and bearer authentication](oauth.md) and is not repeated here.

## What was verified live

Verified on 2026-09-21 against a real Gemini Spark connection to a Hermes GPT OAuth deployment:

- Google's backend probes: `HEAD /mcp` → 401 with a `WWW-Authenticate` `resource_metadata` challenge, `GET /.well-known/oauth-protected-resource` → 200, `GET /.well-known/oauth-authorization-server` → 200, `POST /mcp` → 401.
- The browser then completed the authorize hop, and the Google back channel exchanged the code at `POST /oauth/token` → 200.
- Authenticated `POST /mcp` traffic from Google backend ranges followed: `initialize`, `tools/list`, `tools/call`.
- Authorize parameters observed: `response_type=code`; scope `hermes openid offline_access` (the configured scope plus `openid` and `offline_access`); PKCE `code_challenge_method=S256`; a `resource` parameter equal to the server resource URL.

## Prerequisites

- A **personal Google Account** that is 18+, in the US, using English, with activity ("Keep Activity") on. Custom apps are added in the Gemini **web app**; see [Google's help page](https://support.google.com/gemini/answer/17209137).
- Hermes GPT OAuth configured and validated as documented in [OAuth and bearer authentication](oauth.md): `HERMES_GPT_OAUTH_ENABLE=1`, an HTTPS issuer, a confidential client, and a client secret of 43–128 URL-safe characters.
- HTTPS exposure Google can reach for both the authorization endpoints and `/mcp`. A [Cloudflare Tunnel deployment](cloudflare-tunnel.md) is one supported public-proxy path; the authenticated remote-mode rules in [oauth.md](oauth.md) still apply (keep the process loopback-bound and terminate HTTPS in a deliberately configured proxy).
- The MCP endpoint URL you will paste into Gemini: `https://<your-mcp-host>/mcp`.
- The built-in OAuth boundary requires streamable HTTP (`--http`); legacy SSE is rejected when OAuth is enabled.

## Security posture

- **Default behavior is read-only.** Gemini Spark does not change that: it is an MCP client, not an Operator authority.
- **Operator/Owner authority is process-wide, not per client.** Every registered OAuth client on an instance reaches the same policy-gated tool surface, so an additional client profile can never *lower* what another client sees — but it is not separately sandboxed either. Adding the Gemini Spark profile to an instance that already runs Operator direct mode or Owner Mode gives the Gemini connector the same authority the primary client has. Prefer a **dedicated read-only instance** for Gemini instead of adding it to an owner-mode instance.
- **Isolated credentials.** The profile registers its own `client_id`, its own secret, and its own exact-match redirect-URI allowlist. A client can only complete an authorization request to its **own** redirect URIs and can only authenticate with its **own** secret at the token endpoint; another client's credentials or redirects fail closed.
- **Exact-match redirects.** Redirect URIs are compared exactly. Wildcards are neither accepted nor supported.
- **No dynamic client registration, by design.** `registration_endpoint` is not advertised, which is exactly what triggers Gemini's documented manual Client ID/Secret fallback.

## Configuration

Two patterns are supported. Pattern (a) is the one that was live-verified end to end.

### (a) Dedicated instance for Gemini (live-verified pattern)

Serve the Gemini Spark connector from its own instance using the standard single-client variables:

```text
HERMES_GPT_OAUTH_ENABLE=1
HERMES_GPT_OAUTH_ISSUER=https://gemini-mcp.example.com
HERMES_GPT_OAUTH_CLIENT_ID=gemini-spark-client
HERMES_GPT_OAUTH_CLIENT_SECRET=<43-to-128-character-generated-secret>
HERMES_GPT_OAUTH_REDIRECT_URI=<the exact Google callback discovered below>
HERMES_GPT_OAUTH_SCOPE=hermes
```

Generate the secret as documented in [oauth.md](oauth.md). Do not reuse a Hermes, gateway, or provider credential.

### (b) Additional client profile on an existing OAuth deployment

The primary client configuration is untouched; add the opt-in Gemini profile beside it:

```text
# Primary client (unchanged — for example the ChatGPT connector client)
HERMES_GPT_OAUTH_CLIENT_ID=chatgpt-client
HERMES_GPT_OAUTH_CLIENT_SECRET=<existing-primary-secret>
HERMES_GPT_OAUTH_REDIRECT_URI=https://chatgpt.com/connector/oauth/<exact-callback-id>

# Opt-in Gemini Spark client profile (default: off)
HERMES_GPT_OAUTH_GEMINI_ENABLE=1
HERMES_GPT_OAUTH_GEMINI_CLIENT_ID=gemini-spark-client
HERMES_GPT_OAUTH_GEMINI_CLIENT_SECRET=<43-to-128-character-generated-secret>
HERMES_GPT_OAUTH_GEMINI_REDIRECT_URI=<the exact Google callback discovered below>
```

- The profile is enabled only when `HERMES_GPT_OAUTH_GEMINI_ENABLE` is exactly `1`; unset or any other value leaves it off.
- The profile is an addition to a complete primary configuration: it is evaluated only when `HERMES_GPT_OAUTH_ENABLE=1` and the required `HERMES_GPT_OAUTH_*` variables are present.
- Enabled but incomplete fails startup validation — the process does not serve, and the uncaught `ValueError` names the missing variables (for example `HERMES_GPT_OAUTH_GEMINI_CLIENT_SECRET`).
- `HERMES_GPT_OAUTH_GEMINI_REDIRECT_URI` accepts one or more exact HTTPS URIs, comma-separated, parsed exactly like the primary redirect URI.
- Both clients share the issuer, the resource (`<issuer>/mcp`), and the one configured `HERMES_GPT_OAUTH_SCOPE` (default `hermes`); the compatibility scopes `openid` and `offline_access` are accepted for both.
- Client IDs must be unique across the primary client and every profile; a duplicate fails startup validation.
- Do not reuse the primary secret for the Gemini profile — separate credentials and separate redirect allowlists are the point of the profile.

Restart the instance after changing any of these values.

## Callback discovery

Google assigns the callback URI per connection and there is no pattern to guess. The first connection attempt is rejected (the authorize response is an `invalid_request` error, `redirect_uri is not registered.`), and the server's HTTP access log records the request line for that `/oauth/authorize` call — including the `redirect_uri` query parameter, which is the exact callback Google used. The access log records client address, method, path with query string, and status; it never records bodies or headers, so no client secret, authorization code, or bearer token appears in it.

Observed callback shape:

```text
https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-<numeric-google-app-id>-<host-with-dots-as-underscores>
```

Example with anonymized values:

```text
https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-123456789012345678901-example_com
```

Procedure:

1. Attempt the connection once in Gemini and let it fail.
2. Read the rejected `/oauth/authorize` request line from the server's HTTP access log and copy the `redirect_uri` value **verbatim** into `HERMES_GPT_OAUTH_GEMINI_REDIRECT_URI` (or `HERMES_GPT_OAUTH_REDIRECT_URI` in pattern (a)).
3. Restart the instance so the allowlist is reloaded.
4. Retry the connection in Gemini.

Never add a wildcard, a suffix, or a "close enough" variant: exact matching means anything but the verbatim value is rejected.

## Connecting in Gemini

In the Gemini web app: **Settings & help → Connected Apps → Custom apps → Add a custom app**, paste the MCP URL (`https://<your-mcp-host>/mcp`), then open **Advanced features → Show more** and enter the Client ID and Client secret manually. This is Google's documented path when the server does not advertise dynamic client registration.

Success looks like: the custom app connects without Google's account-linking error, and the server log shows the authorize hop, then `POST /oauth/token` → 200, then authenticated `POST /mcp` traffic.

## Verification

Sanitized server-side evidence to expect (never paste tokens, codes, or secrets into a report):

- `GET /.well-known/oauth-protected-resource` and `GET /.well-known/oauth-authorization-server` return 200 anonymously;
- an unauthenticated `HEAD /mcp` or `POST /mcp` returns 401 with a `WWW-Authenticate: Bearer realm="hermes-gpt", resource_metadata="<issuer>/.well-known/oauth-protected-resource"` challenge;
- after the browser authorize hop, `POST /oauth/token` returns 200;
- authenticated `POST /mcp` calls arrive for `initialize`, `tools/list`, and `tools/call`.

Functional check: ask Gemini to **list Hermes skills**. That exercises the read-only `hermes_skill_list` surface through the new credential. `hermes_oauth_status` (read-only) reports durable token-store presence and expiry only — never token material.

## Token lifecycle

- The token exchange returns an access token with `expires_in=3600` (one hour).
- When `offline_access` was granted, it also returns a refresh token. Each refresh rotates the refresh token; replaying the old value fails with `invalid_grant`. Refresh tokens live 30 days and survive restarts through the encrypted durable store.
- `hermes_oauth_revoke` (owner + direct + confirm) revokes **every client** on that instance's data root in one transaction and requires `dry_run=false` plus `confirm=true` with Owner Mode active; it also drops the live process's token caches and rotates the authorization-code key. See [OAuth and bearer authentication](oauth.md#token-lifecycle) for the store and key-management details.
- Rotating the deployment's primary client secret invalidates every signed access token, including tokens issued to the Gemini client.

## Limitations

- **No dynamic client registration.** There is no `registration_endpoint`, so the Client ID/Secret must be entered manually in Gemini under **Advanced features → Show more**. This is by design, not a misconfiguration.
- **PKCE S256 works.** Only S256 is advertised and accepted; another `code_challenge_method` is rejected with `invalid_request`.
- **Google-side errors can be opaque or transient.** An observed example is "Account linking is required to use this custom app. Try again." — retrying the connection is often sufficient.
- **The user's browser must resolve the MCP hostname** during the authorize hop; the Google backend reaching the server is not enough.
- **The tool surface still follows the server's operator posture.** OAuth authenticates a client; it does not activate mutations, direct mode, or Owner Mode. On a read-only instance, Gemini is read-only.
- **Consumer feature set.** The account requirements above are Google's; this is not an enterprise/Vertex connector.

## Rollback

- Disable the profile: unset `HERMES_GPT_OAUTH_GEMINI_ENABLE` (and the three `HERMES_GPT_OAUTH_GEMINI_*` values) and restart. The primary client keeps working and its tokens remain valid; tokens issued to the Gemini client stop validating once the profile is gone, because validation requires the token's client to still be registered.
- Dedicated instance: stop that service (for example `systemctl --user stop hermes-gpt-server.service` for a user-service deployment) so the Gemini endpoint is no longer served.
- Gemini side: **Settings & help → Connected Apps → Custom apps** and remove the custom app.
- Optional: `hermes_oauth_revoke(confirm=true, dry_run=false)` with Owner Mode active retires the durable token rows for every client on that instance's data root.
