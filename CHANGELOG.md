# Changelog

## 0.10.0 - 2026-09-07

vNext slice-1 derived mission views and the supervised mission controller (shadow): deterministic MissionPlan decomposition, read-only capability-manifest and mission-ledger views, a dry-run budget envelope, dry-run placement scoring, semantic failure classification with a smallest-first recovery matrix, and a shadow/observe controller reconciler — all additive and decision-only, preserving the existing read-only/dry-run, read-only, and shadow authority ladder. This slice adds ~27 new MCP tools; none of them can mutate a Mission, dispatch work, or approve anything in this release.

- Added the `hermes_plan_*` MissionPlan surface (`hermes_plan_create/get/list/validate/decompose/review/node_transition/set_status`): a deterministic, bounded decomposition DAG over a `MissionSpec`. Plan create/node-transition/set-status are additive to an isolated plan store and read-only with respect to the Mission lifecycle; get/list/review/validate/decompose are read-only. The DAG is validated through a state machine with dry-run-first mutation defaults.
- Added read-only derived capability-manifest and mission-ledger views (`hermes_capability_manifest`, `hermes_mission_ledger`, `hermes_mission_ledger_replay`): the manifest folds the Fabric node registry, Fleet authority manifest, profile toolsets/skills, and provider manifests into a single queryable capability index; the ledger is a merged, replayable, cursor-paginated per-mission event timeline. Both carry explicit `readOnlyHint`, enforce a per-client source allowlist, audit every call, and expose no mutation path.
- Added the mission budget envelope (`hermes_budget_set/get/check/record`): a per-mission spend envelope (`budget_accounts`) and a read-only `budget_check` evaluation surface, enforced dry-run-first. The D3 hard-block path (pause Mission + `budget_breaker` signal) is designed but flag-default-off and wired for a later slice.
- Added deterministic placement scoring (`hermes_placement_score/candidates/get/list`): filter-and-score over the derived capability-manifest index (hard filters then soft scores), decision output only — `would_assign` is always `False`, no assignment executes, and actual dispatch remains on the existing contract/fleet/delegation authority surfaces.
- Added semantic failure classification and the recovery matrix (`hermes_failure_classify`, `hermes_failure_taxonomy`, `hermes_recovery_matrix`, `hermes_controller_plan_list`): the Ops 8-class taxonomy classifies an authoritative observation envelope and proposes the deterministic smallest-first recovery action; decision output only, `would_execute` always `False`, and the sole durable write is a `controller_plan` row.
- Added the supervised mission controller shadow loop (`hermes_controller_reconcile/status/lease_list/trigger`): a T1–T5 trigger model, per-mission pass lease (TTL-heartbeat-renewed, crash-reclaimed, conflated), and a single shadow `reconcile` pass that observes authoritative mission/plan/delegation/runner state, classifies it, and emits the smallest recovery action as a proposal. It never dispatches, completes, or approves; its only durable writes are `controller_plan`, `controller_telemetry`, and the pass lease.
- Added controller telemetry/health (`controller_telemetry`, 24h-windowed aggregated counters with truthful stale-lease-reclaim accounting) tiered deterministically as GREEN / YELLOW / RED per pass plus a controller rollup; RED spools a fleet-attention `INTERRUPT` envelope to the existing delivery-broker lane (no self-send) and stale heartbeats are loud (INV-10).
- The vNext authority boundary ladder is preserved and documented: read-only surfaces remain read-only, mutating surfaces are dry-run-first, and the controller is a decision-only shadow in this slice (`would_execute`/`would_assign` always false, no deploy path).
- `serverInfo.version` continues to advertise the checkout version (`versioning.VERSION`) so MCP clients can detect stale processes; the connector surface acceptance gate now pins the merged v0.10.0 tool count (137) with no duplicate registration.

## 0.9.0 - 2026-08-24

First-class Missions, unified delegation lineage, durable live events, runner-neutral job supervision, and the bounded Finance bridge.

- Added the first-class durable Mission runtime: `hermes_mission_create/get/list/update/attach/reconcile/transition/approve` with bounded context/skills manifests, attachments, observed-state reconciliation, and Owner-gated final approval.
- Added the unified delegation lifecycle `hermes_delegation_dispatch/get/list/reconcile/cancel`: normalized durable lineage across runner/Fabric execution preserving underlying authority and dry-run gates.
- Added the durable v0.9 live-event surface `hermes_live_events_cursor`/`hermes_live_events_since` plus runner-neutral `hermes_job_status`/`hermes_job_wait` background-job polling contract.
- Fixed `hermes_cron_create` model serialization to match the Hermes Agent scheduler contract: `model_name` now persists as the string `model` field and `model_provider` as the sibling `provider` field (previously an invalid nested `model` dict).
- Fixed the MCP initialize handshake to advertise the app version (`serverInfo.version`) from `versioning.VERSION` instead of the SDK distribution version, so clients can detect stale processes.
- Fixed clustered ChatGPT MCP authentication: access tokens are now HMAC-signed so a bearer issued by one origin of `gpt.tonysimons.dev` is accepted by the other without a shared process-memory token table. Unauthenticated `/mcp` challenges advertise `resource_metadata`. Opaque legacy access tokens remain valid on the issuing origin until expiry.


- Added an opt-in `hermes_finance_analyze` bridge for the local `finance` profile. The bridge accepts bounded `finance.evidence/v1` JSON, rejects credential-like material, passes evidence over stdin to a fixed tool-free child, disables Hermes persistence/memory/background review for the turn, requires a matching `finance.decision/v1` response, and records only metadata hashes/lengths in Operator audit. The tool is disabled unless `HERMES_GPT_ENABLE_FINANCE=1`.

## 0.8.0 - 2026-08-21

Fabric: authenticated cross-machine Swarm execution with capability-aware routing, remote evidence/artifacts, reconciliation, and Flight Deck visibility.

- Added the packaged `hermes-gpt-fabric-peer` A2A/Fabric runtime for authenticated remote execution. Non-loopback serving requires direct TLS.
- Added durable node capabilities, health/freshness tracking, deterministic `execution.backend=auto` routing, explicit route exclusions, and authority-ceiling preservation.
- Added remote evidence collection and immutable artifact admission. Required missing/unavailable evidence fails closed, and admitted artifacts are hash-verified by the coordinator.
- Added restart/timeout/cancel reconciliation that preserves the original distributed attempt plus write-ownership/write-epoch guards for mutation-capable paths.
- Added read-only Flight Deck Fabric nodes, placement, attempts, evidence, and routing views, including truthful selected-route health/freshness/eligibility/authority fields.
- Completed fresh real two-machine G6 acceptance on final Fabric implementation target `4953c5f23db8d356365af8e18148e63d3c80125c`, including induced transport loss, same-attempt recovery, artifact re-hash, fail-closed validation, approval gating, and independent review.
- Known non-blocking presentation limitation: a completed reconciled attempt may retain a historical `FABRIC_TRANSPORT_TIMEOUT` as a current-looking Flight Deck blocker.
- Added `hermes_export_file`, a workspace-authorized MCP-native binary export surface with mandatory allowed-path confinement, denied-secret-path enforcement, symlink escape refusal, a 4 MiB default / 16 MiB hard size cap, optional extension allowlisting, safe audit metadata, and no base64 text fallback. Client attachment rendering remains client-controlled.
- Added a first-class OpenAI Secure MCP Tunnel deployment path for private ChatGPT/Codex/OpenAI access while Hermes GPT stays bound to loopback. Added a canonical guide, supervised Windows launcher, tunnel-aware status example, package wiring, and cross-links from the README, MCP compatibility, Cloudflare, and Windows Codex docs.
- Documented the tunnel security boundary: no public `HERMES_GPT_ALLOWED_HOSTS` entry is required for the private loopback path, static bearer remains optional defense in depth, and built-in OAuth still requires separately reachable browser-facing authorization-server endpoints.
- Pinned the MCP SDK to the FastMCP-compatible 1.x line (`mcp[cli]>=1.0,<2` in `requirements.txt`): mcp 2.x removes `mcp.server.fastmcp`, which breaks the server and test suite.
- Fixed A2A fleet dispatch timeouts losing track of submitted work: `hermes_fleet_dispatch` and `hermes_fleet_dispatch_work_order` now recover the peer-assigned task id by bounded context lookup and return a structured `FLEET_DISPATCH_TIMEOUT` error with `submission_may_have_succeeded: true` and a pollable `task_id`.
- Fixed `hermes-gpt mcp --http` (curated Codex MCP runner) trusting wildcard proxies: it no longer passes `forwarded_allow_ips="*"` to uvicorn, so proxy trust defaults to loopback-only or the operator-set `FORWARDED_ALLOW_IPS`, matching the main server's authenticated-posture proxy handling.
- Fixed OIDC discovery probing with OIDC disabled: `/.well-known/openid-configuration` is now served as a public 404 instead of an auth challenge, so ChatGPT connectors that probe OIDC discovery after OAuth no longer appear disconnected. No OpenID Provider is implemented.
- Fixed `hermes_operator_doctor` reporting gateway health from a heartbeat file alone: a heartbeat with no live gateway PID now fails closed with `GATEWAY_PID_MISSING` instead of `GATEWAY_OK`.
- Hardened Hermes Agent source-root detection: `is_hermes_root` now requires a regular `tools` package (`tools/__init__.py`) or a top-level `hermes_state.py`, so a stray namespace `tools/` directory at the Hermes data root can no longer masquerade as an agent source root and silently degrade the tool surface to read-only fallbacks.

## 0.7.0 - 2026-08-15

Flight Deck: durable, interactive, verifiable autonomy.

- Added the production review-accept writer `hermes_review_accept` (owner +
  direct + confirm) with distinct-reviewer enforcement at write time, bounded
  verdicts, referenced-not-copied evidence, and a durable append-only
  review-evidence store read by `hermes_contract_validate`.
- Added the structured event history surface `hermes_events_query` /
  `hermes_events_tail`: a read-only, redacted, bounded timeline over audit,
  swarm, codex, cron, and kanban stores with per-source allowlist
  (`HERMES_GPT_EVENTS_ALLOWED_SOURCES`) and retention window
  (`HERMES_GPT_EVENTS_MAX_AGE_DAYS`, default 90).
- Added durable encrypted token storage (ADR-001): OAuth access/refresh
  tokens persist through an AES-256-GCM envelope at
  `<hermes_data>/secrets/hermes_gpt_tokens.json` (0600) with keyring → key
  file → env key precedence; server restarts no longer invalidate issued
  credentials. Added `hermes_oauth_status` (read-only) and
  `hermes_oauth_revoke` (owner-gated, pending legal scope decision).
- Added restart reconciliation `hermes_swarm_reconcile`: marks swarm stages
  stuck in `running` as `blocked` (reason `interrupted_by_restart`), never
  auto-advances, and reloads the durable token envelope. `hermes_swarm_stage_advance`
  is now idempotent for already-validated/done stages.
- Added the MCP compatibility manifest (`docs/mcp-compatibility.md`) pinning
  the minimum supported protocol revision 2024-11-05 through the installed
  SDK's latest (2025-11-25), transport matrix, and auth metadata, with
  compatibility tests against the running SDK.
- Added cross-machine seam interfaces (`seams.py`: `DispatchAdapter`,
  `EvidenceProvider` protocols) validated by a two-process-one-host fake;
  no remote implementation is shipped (stretch).
- Promoted OAuth from Unreleased to shipped and documented: static bearer
  compatibility or a single-confidential-client OAuth authorization-code flow
  with optional PKCE S256, stateless signed authorization codes, one-hour
  access tokens, 30-day rotating refresh tokens with replay rejection,
  explicit direct-TLS or loopback trusted-proxy enforcement, and
  ChatGPT-compatible streamable-HTTP request normalization. Added the
  `cryptography` dependency (required) and `keyring` (optional dev).
- Fixed CI hermeticity: `_call_skill_manager` no longer fails when the Hermes
  Agent source tree is absent (optional-import degradation); profile-scoping
  tests skip only when `hermes_constants` is unavailable.
- Fixed explicit local-runner cancellation on Windows by routing durable worker
  PIDs through the same platform-aware process-tree cleanup used by timeout
  paths. A direct-process fallback now covers missing, timed-out, or failing
  `taskkill` invocations.

- Added explicit per-job Codex `execution_mode` with `normal` default and opt-in `nolo`. NOLO now uses Codex 0.147.0's `-a never` approval policy while retaining the requested read-only/workspace-write sandbox, Hermes approved-workspace, direct-mode, confirmation, audit, timeout, and redaction controls. The write gate is required only for `workspace-write`; NOLO expires with the job and does not create persistent global approval-bypass state.
- Added Codex parity for the four session-history capabilities that were previously available only through the full ChatGPT connector, delivered through the separately installed Hermes GPT Session History integration and verified with direct native-tool calls.
- Added a dedicated session-history guide covering client availability, privacy-preserving arguments, bounded Markdown export, optional read-only working-model inspection, and a no-write Codex smoke-test procedure.
- Added separately gated, bounded asynchronous Hermes session continue/send jobs with status and redacted result tools. Session IDs resolve through the existing read-only adapter; prompts are omitted from metadata; subprocesses use fixed arguments without a shell; timeouts and output are capped; restart reconciliation fails closed.
## 0.6.0 - 2026-08-13

- Added the v0.6 Mission Control, Work Contracts, and Swarm Orchestration surfaces with bounded, audited, fail-closed operator controls.
- Added conservative PII stripping for Mission Control free-text failure, audit, cron, and delegation fields; raw bodies remain excluded.
- Made Work Contract retry selection deterministic and scoped forbidden-action audit evidence to the contract task identity.
- Added documented retention and cleanup windows for request dumps, Codex artifacts, and swarm worktrees/workflow records.
- Documented the remaining fail-closed review-evidence limitation: v0.6 has no production review-accept writer.


- Fixed the Codex runner on Windows selecting a protected `WindowsApps`
  executable and failing with `WinError 5`. The runner now supports an explicit
  `HERMES_GPT_CODEX_EXE` override, validates that the chosen executable is a
  launchable regular file outside `WindowsApps` before use, and skips protected
  or unlaunchable PATH candidates. `hermes_codex_status` and `codex doctor`
  report `codex_path`/`codex_source` so availability is never claimed for an
  executable that cannot launch.
- Upgraded A2A fleet control with canonical structured work orders,
  server-controlled profile authority, explicit authorization classes, safe
  completion bundles, hardened bounded parsing, and read-only authority drift
  validation. Existing fleet tools retain registry-only routing, Operator Mode,
  confirmation, dry-run, hashing, redaction, secret-path, and end-of-options
  controls.
- Added bounded read-only session-history pagination that advances by database rows examined while filtering unsafe roles before client exposure.
- Added the gated `hermes_session_list`, `hermes_session_read`, and in-memory `hermes_session_export` tools alongside the existing plain-text `hermes_session_search` tool.
- Added JSON and Markdown export with `MAX_EXPORT_MESSAGES` and `MAX_RESPONSE_BYTES` limits; file creation, file paths, raw exports, and lineage export remain unavailable.
- Added explicit guidance for the `HERMES_GPT_ENABLE_SESSION_SEARCH=1` and `HERMES_GPT_ENABLE_SESSION_INTERNAL_CONTENT=1` gates, FTS availability limitations, default role filtering, and transcript privacy.

## 0.5.0 - 2026-07-10

- Completed the two-way Codex bridge: opt-in Operator toolsets for Codex and gated asynchronous Codex jobs for trusted Hermes GPT clients.
- Added core/operator connector installation, explicit refresh semantics, toolset-aware doctor checks, and shared runtime version discovery.
- Added Windows/Linux CI across Python 3.10-3.12, publish-after-test enforcement, package checks, and final v0.5.0 release notes.

- Added `hermes-gpt update`: check-first, safe fast-forward updates for clean Git checkouts and explicit pip upgrades for installed packages.
- Added update documentation and aligned the README, Codex guide, release notes, package data, and release checklist.

## 0.5.0b1 - 2026-07-09

- Added the first Codex integration batch: a curated MCP stdio server at `hermes-gpt mcp` (also available as `hermes-gpt codex mcp`).
- Added `hermes-gpt codex install`, `uninstall`, `doctor`, and `print-config` with idempotent, backup-first TOML fallback handling.
- Added Codex-focused planning, local vision path validation, web extraction SSRF protections, dry-run cron planning, skill drafting, and gateway diagnostics.
- Added explicit Codex/MCP capability gates plus strict write gates for cron and skill writes.
- Added recursive response redaction for provider keys, GitHub tokens, cookies/session values, bearer tokens, and private keys.

## 0.4.0 - 2026-07-09

- Added env-gated Hermes tool wrappers: `hermes_vision_analyze` (HERMES_GPT_ENABLE_VISION), `hermes_web_search` / `hermes_web_extract` (HERMES_GPT_ENABLE_WEB).
- Added `hermes_cron_create` operator tool with full field support: schedule, prompt, script, skills, deliver, repeat, workdir, no_agent, model, context_from, enabled_toolsets.
- Fixed gateway status PID fallback on macOS — falls back to gateway_state.json when gateway.pid is missing or unparsable.
- Expanded gateway status diagnostics: exposes gateway_state, gateway_kind, gateway_pid_source, gateway_updated_at, gateway_exit_reason, gateway_active_agents.
- Fixed Vercel static site deployment configuration.
- Updated landing page with v0.3.0 and v0.4.0 feature summaries.
- Fixed flaky HTTP smoke test — now skips by default unless HERMES_HTTP_TEST=1 is set.
- Consolidated duplicate exception handling in `_is_pid_alive`.

## 0.3.0 - 2026-06-25

- Added operator diagnostics and recovery tools: `hermes_operator_doctor`, `hermes_operator_snapshot`, `hermes_release_doctor`, and `hermes_operator_recover`.
- Introduced a structured error envelope (`success`, `ok`, `error`, `layer`, `code`, `safe_message`, `suggested_action`, `trace_id`) for all operator-facing failures.
- Converted operator exception handlers in `operator_config`, `operator_cron`, `operator_skills`, `operator_workspace`, and `server` to the new envelope while preserving legacy `success:false` and `error` fields.
- Added PASS/WARN/FAIL/UNSUPPORTED status vocabulary across diagnostic tools.
- Implemented conservative recovery with dry-run default and `apply=true` gating; connector re-registration is explicitly reported as unsupported.
- Added secret-value and absolute-path redaction in structured error messages.
- Added comprehensive tests for diagnostics, recovery, release readiness, and error-envelope safety.
- Updated operator-mode docs, README, release checklist, and release notes for v0.3.0.

## 0.2.0 - 2026-06-21

- Added tiered Operator / Owner Mode tooling for trusted MCP clients.
- Kept the default posture read-only or dry-run, with direct mutation gated by explicit server and per-call opt-in.
- Added operator policy, status, audit, cron, config, env, gateway, workspace, and owner-scope tools.
- Fixed data-root normalization so operator profile operations resolve back to the Hermes data root.
- Updated packaging to include operator modules and release docs.
- Added a new Operator Mode guide, quickstart, and troubleshooting for new users.

## 0.1.0 - 2026-06-18

- Initial local-dev release.
- Added FastMCP stdio and streamable HTTP server.
- Added Hermes file read/search, memory search, skill list/view, and optional gated write/patch/session/terminal capabilities.
- Added release safety gates for write tools, memory writes, session search, terminal execution, and remote no-auth mode.
- Added pytest coverage for default tool surface, auth metadata, safety gates, timeout capping, remote profile blocking, and HTTP initialize.
