# Hermes ChatGPT UI — Security & State Boundary Notes

Status: current (implementation card t_7266e74c, 2026-08-15)
Scope: `ui_security.py`, `ui_api.py` (composition), `server.py` mount,
`web/src/shared/**`, `web/src/stores/connection.ts`, `test_ui_security.py`.

These notes document the browser-facing security boundary for the
conversational Hermes GPT UI. The authoritative design is
`architecture.md` / `interface-contracts.md` (kanban t_ab4f3463); this file
records what the boundary actually enforces and how to verify it.

---

## 1. What crosses the boundary

Every browser-bound payload (JSON response body and SSE `data` line) is
serialized through `ui_security.redact_browser`. The two JSON envelope
helpers (`ui_api.ok` / `ui_api.err`, implemented in `ui_security`) apply
redaction automatically, so a handler cannot skip it by accident.

- `ok(data)` — strict redaction (default).
- `ok(data, content_allowed=True)` — used ONLY for the user's own
  conversation text (chat thread `content`, SSE `token`/`reasoning`
  `delta`). Everything else in that payload still gets the strict treatment.
- `err(code, message)` — message is redacted before it leaves the server.

SSE event payloads are redacted per-event by the chat bridge using the same
function; tool events (`tool_start`/`tool_end`) use the strict default.

## 2. What is redacted (strict mode)

- Raw prompts, memory bodies, transcripts, request dumps, credentials, and
  profile-secret bodies: values under secret-key names (`prompt`, `token`,
  `secret`, `password`, `credentials`, `memory_body`, `transcript`,
  `request_dump`, `client_secret`, `access_token`, `refresh_token`,
  `authorization`, `cookie`, `private_key`, `profile_secret`, …) become
  `[REDACTED]`. Never silently empty.
- `content` / `delta` keys in strict mode are treated as raw message bodies
  and redacted entirely (they survive only via `content_allowed=True`).
- Secret substrings: `sk-…` / `sk-proj-…` OpenAI keys, `AKIA…` AWS keys,
  `Bearer <token>`, `token=|secret=|password=|api_key=` values (reuses
  `operator_policy.redact_output`).
- PII in operator-derived text: emails, phone numbers, `@handles`, name
  labels (mirrors `operator_mission._sanitize_error`).
- Absolute filesystem paths (POSIX home paths, Windows drive paths, UNC, `~/...`) →
  `[REDACTED_PATH]`; secret-file paths (`secrets/…`, `.env`, `auth.json`,
  `hermes_gpt_tokens.json`, `hermes_gpt_tokens.db`, `hermes_gpt_token_key`, `.ssh/…`) →
  `[REDACTED_SECRETS_PATH]`. Store paths are never exposed.
- Length cap: every string in strict mode is truncated to
  `HERMES_GPT_UI_TOOL_PREVIEW_BYTES` (default 8192) with a `…[truncated]`
  marker. Chat content is bounded at 1 MiB (conversation text is not a tool
  preview).

`content_allowed=True` skips PII/path mangling and the 8 KiB cap for the
user's own text only; unambiguous secret shapes are still removed.

## 3. Authn / authz

- The UI reuses `oauth_auth.BearerAuthMiddleware` and the existing
  `build_asgi_app` wiring — **loopback default (no auth)**; static bearer or
  confidential-client OAuth when configured; remote profile remains blocked
  by the existing server gates. The UI adds no auth path of its own.
- UI routes mount BEFORE `Mount("/", mcp_app)` when
  `HERMES_GPT_UI_ENABLED=1`, so same-origin `/api/*` and `/ui` calls never
  fall through to the MCP catch-all. With the env unset, the mount code is
  not even imported — installed wheels without the UI modules are unaffected.

## 4. Account status (`GET /api/me`)

`accountStatus` is derived read-only from the durable token store
(`token_store.status`), never from token material:

| State | Meaning |
|---|---|
| `ok` | no auth configured (loopback), or static bearer, or valid store |
| `expired` | durable store present, `expires_at` in the past |
| `revoked` | durable store unreadable/corrupt (tokens unusable) |
| `unauthorized` | OAuth configured but no usable durable store — re-auth needed |

`/api/me` also returns `operatorLevel` (policy snapshot), `allowedSurfaces`
(v0.7 Mission allowlist semantics: unset = all, list = only listed, empty =
none — the unset state is NOT "deny by default"), and `uiCapabilities`
(permission-aware: the mutating `approvals` lane drops when the account is
degraded or the level is below `workspace`; read-only lanes stay).

## 5. Connection / stale-state helpers

- `GET /api/connection` returns a per-process `serverStartupId`; the
  connection store compares it across polls to detect a server restart
  mid-session and surfaces the in-flight turn as interrupted (recoverable via
  persisted messages + turn lease), never as running.
- `is_stale_lease(ts)` treats a turn lease older than
  `HERMES_GPT_UI_STALE_LEASE_S` (default 600) as stale → interrupted UX.
- `web/src/shared/ConnectionStatus.tsx` renders transport health;
  `web/src/shared/AccountStatusBanner.tsx` renders the expired/revoked/
  unauthorized recovery UX (re-auth affordance, mutating controls disabled,
  read-only chat history stays viewable). Wiring the account store into the
  banner is the flight card's job (t_1135e15b owns `web/src/stores/account.ts`).

## 6. Mutations remain auditable and gated

Every mutation the UI offers goes through one endpoint, `POST /api/ops/action`
(`ui_ops.py`), which executes an existing gated `hermes_*` tool. The browser
has no bypass: read-only default, dry-run-first, confirm gate preserved (409
`CONFIRM_REQUIRED`), Owner Mode never bypasses secret-path protections, and
every call is written to the operator audit JSONL. The boundary guarantees the
response is redacted and the envelope (`ok`/`error`, gate codes) is never
weakened (`test_error_envelope_preserves_gate_codes`).

### 6.1 Browser mutation allowlist (current)

`ui_ops._MUTATION_TOOLS` is the complete browser allowlist. A request for any
other tool name returns `404 UNKNOWN_TOOL`. Each entry lists the arguments the
browser may send. Anything else returns `400 INVALID_ARGS`, so `hermes_root`,
runners and other internal kwargs cannot be smuggled in. The root is always
resolved server-side.

| Tool | Browser args (exact) | Required level (403 hint) | Confirm / apply |
| --- | --- | --- | --- |
| `hermes_review_accept` | `contract_sha256`, `task_id`, `assignee`, `reviewer`, `verdict`, `evidence_refs`, `approval_reference`, `dry_run`, `confirm` | owner | `confirm` |
| `hermes_swarm_stage_advance` | `workflow_id`, `stage_id`, `confirm`, `dry_run` | workspace | `confirm` |
| `hermes_swarm_approve` | `workflow_id`, `confirm`, `dry_run` | owner | `confirm` |
| `hermes_cron_run` | `profile`, `job_id`, `dry_run`, `timeout` | cron | long-running (202) |
| `hermes_cron_pause` | `profile`, `job_id`, `reason`, `dry_run` | cron | none |
| `hermes_cron_create` | `profile`, `schedule`, `prompt`, `name`, `skills`, `deliver`, `repeat`, `script`, `workdir`, `no_agent`, `context_from`, `enabled_toolsets`, `model_provider`, `model_name`, `dry_run` | cron | none |
| `hermes_cron_copy` | `source_profile`, `target_profile`, `job_id`, `dry_run` | cron | none |
| `hermes_cron_move` | `source_profile`, `target_profile`, `job_id`, `pause_source`, `test_run_target`, `dry_run` | cron | none |
| `hermes_codex_cancel` | `job_id`, `confirm`, `dry_run` | workspace | `confirm` |
| `hermes_operator_recover` | `profile`, `apply` | workspace | `apply` flag |
| `hermes_swarm_reconcile` | `apply` | workspace | `apply` flag |
| `hermes_oauth_revoke` | `confirm`, `dry_run`, `rotate_key` | owner | `confirm` |
| `hermes_mission_approve` (v0.16) | `mission_id`, `approval_reference`, `confirm`, `dry_run` | owner | `confirm` |
| `hermes_plan_node_transition` (v0.16) | `mission_id`, `node_id`, `target_state` (`failed` only), `reason`, `confirm`, `dry_run` | owner (browser precheck; the tool itself needs workspace) | `confirm` |
| `hermes_plan_approve_gate` (v0.16.1) | `mission_id`, `node_id`, `reason`, `confirm`, `dry_run` (no `expected_plan_version`: the server derives it) | owner (browser precheck; the tool itself also requires Owner Mode) | `confirm` |
| `hermes_autopilot_stop` (v0.16) | `mission_id`, `confirm`, `dry_run` | workspace | `confirm` |
| `hermes_owner_inbox_ack` (v0.16) | `item_ids` (1 to 50), `dry_run` | workspace | none (no `confirm` argument) |

The required level is the hint returned with `403 LEVEL_REQUIRED`. The gate
itself is the tool's own check, except for the v0.16 precheck described next.

The v0.16 entries add gates in front of the tool, under the same two-request
flow:

- **`hermes_plan_node_transition`** is restricted to rejecting an approval
  frontier from the browser. A precheck (`_precheck_approval_transition`) reads
  the plan read-only and refuses unless the node is an `approval` node or has
  `authorization_class == high_impact`, is currently `awaiting_approval`, and
  the target is `failed`. Approve is no longer a browser transition. The plan
  `version` it reads is passed as `expected_plan_version`, so a concurrent plan
  replacement is refused by the tool's compare-and-swap. The browser path also
  requires Owner Mode, which is stricter than the tool's own workspace gate.
- **`hermes_plan_approve_gate`** (v0.16.1) is the browser approve. A precheck
  (`_precheck_approval_gate`) reads the plan read-only and refuses unless the
  node is gated, is on the forward path to `completed`, and has every parent
  `completed`. It sets `expected_plan_version` from that read. The tool walks the
  shortest legal path through `hermes_plan_node_transition`, one compare-and-swap
  per step, and does not roll back. Owner Mode is required. Refusals return
  `400 APPROVAL_GATE_REFUSED` (fixed messages) or `403 LEVEL_REQUIRED`. Details:
  [owner-inbox.md](owner-inbox.md#owner-gate-approval-hermes_plan_approve_gate).
  Refusals return `400 APPROVAL_TRANSITION_REFUSED` (fixed messages) or
  `403 LEVEL_REQUIRED`, and write nothing. Details: [owner-inbox.md](owner-inbox.md#flight-deck-actions).
- **`hermes_mission_approve`** calls the same function as MCP
  (`operator_mission_runtime.hermes_mission_approve`). Owner Mode is enforced
  there, server-side. With Owner Mode off, the browser gets that function's own
  refusal and nothing is approved. No browser-only path exists.
- **`hermes_autopilot_stop`** is never gated behind `HERMES_GPT_AUTOPILOT`.
- **`hermes_owner_inbox_ack`** writes only an acknowledgement. It never
  approves, transitions or changes a Mission, plan, Work Contract or delegation.

Owner approval from the browser still requires Owner Mode on the server
(`HERMES_GPT_OPERATOR_ENABLED=1`, `HERMES_GPT_OPERATOR_LEVEL=owner`,
`HERMES_GPT_OWNER_ACTIVE=1`, and the owner acknowledgement `HERMES_GPT_OWNER_ACK`). The browser has
no way to set these: they are read from the server environment.

### 6.2 Request protections

Browser actions get the same request protections as every other `/api/*`
route, from the outer middleware in `server.build_asgi_app`:

- **Bearer auth.** When a static bearer or OAuth is configured, a request
  without a valid token gets `401`. With neither configured (the loopback
  default), no auth check applies.
- **CORS.** Only `https://chatgpt.com` and the configured OAuth issuer origin
  receive `Access-Control-Allow-Origin`. A foreign `Origin` receives no ACAO
  header, so its page cannot read the response.

- **Cross-site refusal (v0.16).** Before parsing the body,
  `POST /api/ops/action` refuses:
  - any `Content-Type` other than `application/json` (`415 UNSUPPORTED_MEDIA_TYPE`).
    A page on another site cannot send a JSON body cross-site without a CORS
    preflight, and the preflight is not granted;
  - a `Sec-Fetch-Site` header other than `same-origin` or `none`
    (`403 CROSS_SITE_REFUSED`);
  - an `Origin` header whose host and port differ from the request's `Host`,
    including `null` (`403 CROSS_SITE_REFUSED`).

  Clients that are not browsers and send neither header are unaffected. These
  checks protect the loopback no-auth default as well. They are in addition to
  the Operator gates (enabled, level, apply mode, Owner Mode, confirm), never a
  replacement for them.

## 7. Env vars (architecture.md §15)

| Env | Default | Meaning |
|---|---|---|
| `HERMES_GPT_UI_ENABLED` | unset (off) | mount UI routes + static serving |
| `HERMES_GPT_UI_PROFILE` | `default` | profile the UI runs as |
| `HERMES_GPT_UI_DIR` | `web/dist` | static build output override |
| `HERMES_GPT_UI_STALE_LEASE_S` | `600` | stale turn-lease threshold |
| `HERMES_GPT_UI_TOOL_PREVIEW_BYTES` | `8192` | per-string / tool-preview cap |

Existing env behavior is unchanged.

## 8. Verification

```bash
python -m pytest test_ui_security.py      # 36 tests: redaction properties,
                                          # account states, auth boundary,
                                          # allowlist semantics, /api/* sweep
cd web && npm install && npx tsc --noEmit  # frontend shared skeleton typecheck
```

Property sweep: `test_property_all_api_get_routes_redacted` walks every
mounted `GET /api/*` route and asserts the body is free of forbidden
patterns; `test_property_sse_payloads_redacted` runs every SSE event shape
through the boundary. When chat/flight routes land in later cards, the same
sweep covers them automatically.

## 9. Known environment note (not from this card)

`test_mcp_compat.py::test_package_metadata_allows_mcp_1x_floor` fails in
this venv because the installed `hermes-gpt` distribution metadata is stale
(0.5.0, from before the `mcp[cli]>=1.0,<2` floor). It reads installed
metadata, not the working tree; it is unaffected by this card's diff.
