# Changelog

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
