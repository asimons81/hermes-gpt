# Hermes GPT v0.7.0 — Flight Deck

Flight Deck makes durable autonomy legible: every long-running task, event,
evidence record, and authority grant is visible, inspectable, and gated —
without giving the operator a bypass.

This release is the **implementation of the v0.7 Flight Deck slices** (S0–S9).
It is additive: no existing tool name, schema, or authority class changes.

## What's new

### 1. Production review evidence (`hermes_review_accept`)

Closes the v0.6 gap where contract validation had no production
review-accept writer.

- Owner-gated write (`owner` + direct + confirm) of review-acceptance records;
- distinct reviewer enforced **in code** at write time and re-checked at
  validate time (`reviewer != assignee`; self-review rejected both ways);
- bounded verdict vocabulary (`SATISFIED` / `NOT_SATISFIED`);
- evidence is referenced (`evidence_refs`), never copied — no raw prompts or
  transcripts in the store;
- durable append-only store at `<hermes_data>/review-evidence/review-acceptances.jsonl`;
- `hermes_contract_validate` reads the store as an additional evidence source
  while keeping the v0.6 audit + human-approval paths.

### 2. Structured event history (`hermes_events_query` / `hermes_events_tail`)

A unified, queryable, redacted timeline across audit, swarm, codex, cron, and
kanban stores.

- read-only by construction (derived read-model, no write path);
- per-source allowlist `HERMES_GPT_EVENTS_ALLOWED_SOURCES` (unset = all /
  list = only listed / empty = none);
- retention window `HERMES_GPT_EVENTS_MAX_AGE_DAYS` (default 90);
- Mission Control redaction invariants reused: no raw messages, memory bodies,
  transcripts, request dumps, credentials, or profile-secret bodies.

### 3. Durable encrypted token storage + trusted-client OAuth promotion

- OAuth access/refresh tokens persist through an AES-256-GCM envelope at
  `<hermes_data>/secrets/hermes_gpt_tokens.json` (0600);
- key management precedence: OS keyring → key file (0600) → env key
  (`HERMES_GPT_TOKEN_MASTER_KEY`, CI/test only);
- server restarts no longer invalidate issued credentials;
- `hermes_oauth_status` reports presence/expiry only — no token material on
  any surface;
- `hermes_oauth_revoke` (owner-gated; pending legal scope decision) deletes
  the envelope and optionally rotates the key;
- OAuth promoted from Unreleased to shipped and documented (`docs/oauth.md`).

### 4. Restart-safe continuity (`hermes_swarm_reconcile`)

- marks swarm stages stuck in `running` as `blocked` with
  `reason: interrupted_by_restart` — **never auto-advances**;
- reloads and verifies the durable token envelope;
- `hermes_swarm_stage_advance` is now idempotent for already-validated/done
  stages (re-advance returns current state as a no-op);
- dry-run by default; apply requires workspace/owner + direct.

### 5. MCP compatibility manifest

- `docs/mcp-compatibility.md` pins the minimum supported MCP protocol revision
  (`2024-11-05`) through the installed SDK's latest (`2025-11-25`), the
  transport matrix (stdio, streamable HTTP, SSE), and trusted-client auth
  metadata; compatibility tests run against the installed SDK.

### 6. Cross-machine seam interfaces (stretch)

- `seams.py` defines `DispatchAdapter` / `EvidenceProvider` protocols
  validated by a two-process-one-host fake over loopback;
- **no remote implementation is shipped**; cross-machine execution is
  intentionally out of scope for v0.7.

### 7. CI hermeticity fix

- `_call_skill_manager` no longer fails when the Hermes Agent source tree is
  absent from `sys.path` (CI): the optional import degrades gracefully, and
  profile-scoping tests skip only when `hermes_constants` is unavailable.

## Gates and limitations

- `hermes_oauth_revoke` surface scope and the durable token store's key
  fallback policy are **subject to legal review before shipping** (risk
  register R4/R9; ADR-001). This release's legal risk review memo is at
  `docs/releases/v0.7-flight-deck-legal-risk-review.md`.
- G4 (independent verification) requires adversarial review tests, no-raw-body
  event tests, and live ChatGPT connector verification; G5 (public release)
  requires the full release checklist plus separate GitHub and PyPI
  publications. No public release action is taken by this package alone.
- Event retention window and review-evidence retention are pending legal
  sign-off before S3/S4 ship as release.

## Migration

v0.7 is additive. No existing store, tool name, or schema migrates. New
stores are created lazily on first use. New environment variables:

- `HERMES_GPT_EVENTS_ALLOWED_SOURCES`
- `HERMES_GPT_EVENTS_MAX_AGE_DAYS` (default 90)
- `HERMES_GPT_TOKEN_MASTER_KEY` (CI/test only)

## Verification

- Full test suite green: `python -m pytest` (646 tests collected — 643
  passed, 3 skipped; all new slices covered: MCP compat, recovery, review,
  events, token store, seams).
- CI green on Python 3.10–3.12 via `.github/workflows/ci.yml`.
