# hermes-gpt v0.4.0 — Tool Surface Expansion

## Summary

v0.4.0 adds env-gated Hermes tool wrappers (vision, web search, web extract) and a cron creation operator tool. It also ships the macOS gateway status PID fallback from an external contributor, expands gateway diagnostics, and fixes infrastructure (Vercel deploy, flaky test).

## Highlights

- **New env-gated tools:**
  - `hermes_vision_analyze` — analyze images through Hermes Agent (`HERMES_GPT_ENABLE_VISION=1`)
  - `hermes_web_search` — search the web (`HERMES_GPT_ENABLE_WEB=1`)
  - `hermes_web_extract` — extract page content (same env gate)
- **New operator tool:**
  - `hermes_cron_create` — create cron jobs from scratch with full field support (schedule, prompt, script, skills, workdir, no_agent, model, etc.)
- **Gateway diagnostics expansion:**
  - PID fallback from `gateway_state.json` when `gateway.pid` is missing (fixes macOS detection)
  - New fields: `gateway_state`, `gateway_kind`, `gateway_pid_source`, `gateway_updated_at`, `gateway_exit_reason`, `gateway_active_agents`
- **Infrastructure:**
  - Vercel static site now deploys correctly from `site/`
  - Landing page updated with v0.3.0 and v0.4.0 features
  - HTTP smoke test no longer fails CI (skipped by default)

## What's new in this release

```text
- 4 new MCP tools (vision, web search, web extract, cron create)
- 5 new test functions (26 cron tests, 188 total)
- Gateway PID fallback from external contributor
- Vercel deploy fixed + site updated
- ~372 lines added across 4 files
```

## Safety model

- Vision and web tools are env-gated (`HERMES_GPT_ENABLE_VISION=1` / `HERMES_GPT_ENABLE_WEB=1`) — hidden by default.
- `hermes_cron_create` follows the existing operator safety model: dry-run default, level gating, audit logging.
- All existing safety gates unchanged: default read-only, two-gate mutation, no shell=True.

## Breaking changes

- None. All additions are additive (new tools) or internal (gateway diagnostics fields, infrastructure fixes).

## Known limitations

- Remote profile still requires explicit unsafe bypass (no auth layer yet).
- Vision and web tools require a running Hermes Agent install with those tools available.
