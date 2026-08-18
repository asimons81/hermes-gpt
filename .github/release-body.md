# Hermes GPT v0.7.0 — Flight Deck

Hermes GPT v0.7.0 "Flight Deck" makes durable autonomy legible: every long-running task, event, evidence record, and authority grant is visible, inspectable, and gated — without giving the operator a bypass. The release is additive; no existing tool name, schema, or authority class changes.

## Production review evidence (`hermes_review_accept`)

Owner-gated write of review-acceptance records with distinct-reviewer enforcement **in code** at write time and re-checked at validate time (`reviewer != assignee`; self-review rejected both ways). Bounded verdict vocabulary (`SATISFIED` / `NOT_SATISFIED`), referenced-not-copied evidence (no raw prompts or transcripts in the store), and a durable append-only store read by `hermes_contract_validate` alongside the v0.6 audit + human-approval paths.

## Structured event history (`hermes_events_query` / `hermes_events_tail`)

A unified, queryable, redacted timeline across audit, swarm, codex, cron, and kanban stores. Read-only by construction, with a per-source allowlist (`HERMES_GPT_EVENTS_ALLOWED_SOURCES`), retention window (`HERMES_GPT_EVENTS_MAX_AGE_DAYS`, default 90), and the same Mission Control redaction invariants: no raw messages, memory bodies, transcripts, request dumps, credentials, or profile-secret bodies.

## Durable encrypted token storage + trusted-client OAuth

OAuth access/refresh tokens persist through an AES-256-GCM envelope at `<hermes_data>/secrets/hermes_gpt_tokens.json` (0600) with keyring → key file → env key precedence, so server restarts no longer invalidate issued credentials. Added `hermes_oauth_status` (presence/expiry only — no token material on any surface) and `hermes_oauth_revoke` (owner-gated). OAuth is promoted from Unreleased to shipped and documented in `docs/oauth.md`.

## Restart-safe continuity (`hermes_swarm_reconcile`)

Marks swarm stages stuck in `running` as `blocked` with `reason: interrupted_by_restart` — never auto-advances — and reloads the durable token envelope. `hermes_swarm_stage_advance` is now idempotent for already-validated/done stages. Dry-run by default; apply requires workspace/owner + direct.

## MCP compatibility manifest

`docs/mcp-compatibility.md` pins the minimum supported MCP protocol revision (2024-11-05) through the installed SDK's latest (2025-11-25), the transport matrix (stdio, streamable HTTP, SSE), and trusted-client auth metadata, with compatibility tests against the installed SDK.

## Also in this release

- Cross-machine seam interfaces (`seams.py`: `DispatchAdapter` / `EvidenceProvider` protocols) validated by a two-process-one-host fake over loopback — interfaces only, no remote implementation shipped.
- CI hermeticity fix: `_call_skill_manager` no longer fails when the Hermes Agent source tree is absent from `sys.path`; profile-scoping tests skip only when `hermes_constants` is unavailable.

## Verification

Full test suite green: 646 tests collected — 643 passed, 3 skipped — covering MCP compat, recovery, review, events, token store, and seams. CI green on Python 3.10–3.12. See the [v0.7.0 release notes](docs/release-notes-v0.7.0.md) and [retention policy](docs/retention-policy.md).
