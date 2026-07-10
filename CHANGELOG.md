# Changelog

## 0.5.0b2 - Unreleased

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
