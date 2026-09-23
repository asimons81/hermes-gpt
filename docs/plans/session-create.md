# hermes_session_create — Implementation Plan

**Goal:** Add a new MCP tool `hermes_session_create` that creates a real new Hermes session
and runs its first work asynchronously, reusable with the existing
`hermes_session_job_wait` / `hermes_session_job_result` flow.

**Base decision (flagged):** task named `master` as base, but the required machinery
(`hermes_session_job_wait`, `max_job_runtime_seconds` param with 7200 cap) exists only on
branch `feat/job-result-mcp-budget`. Basing on `master` (9f53710) would require porting the
whole session-job layer (risky, tsar scope). Chosen base: `feat/job-result-mcp-budget`
(HEAD 81b04a4). New branch `feat/session-create`, PR targeting `master`. Real repo I can push
to is `vgkeeper/hermes-gpt` (fork of `asimons81/hermes-gpt`); upstream push unavailable.

**Architecture:** `hermes_session_create(prompt, max_job_runtime_seconds, profile, title?)`
creates the session directly in the SessionDB (id shape `{ts}_{uuid6}` like the CLI), then
reuses the exact `hermes_session_continue` job machinery by spawning
`hermes --resume <new_id> --oneshot <prompt>`. Returns `{success, job_id, session_id, profile,
status}` immediately; the first work is followed by `hermes_session_job_wait` then
`hermes_session_job_result`.

## Files
- Modify: `operator_session.py` — add `hermes_session_create`, `_validate_create`,
  `_create_session_in_db`, refactor shared spawn into `_start_job` (reused by continue+create).
- Modify: `server.py` — add `hermes_session_create` server wrapper + `server.add_tool`.
- Modify: `docs/session-control.md` — document the tool.
- Test: `test_operator_session.py` — validation + distinct-session + async-shape tests.
- Test: `test_server.py` — server wrapper gating/validation tests.

## TDD task list
1. RED tests in test_operator_session.py (create validations, distinct session, async shape).
2. Run → fail.
3. Refactor `operator_session.py`: `_validate_create`, `_create_session_in_db`, `_start_job` refactor, `hermes_session_create`.
4. GREEN targeted tests.
5. Server wrapper + add_tool; RED/GREEN in test_server.py.
6. Full relevant suite (test_operator_session.py, test_server.py, test_job_result_pagination.py).
7. Docs.
8. Commit, push, PR to master.

## Security/limits preserved
- `profile` via `op.validate_profile_name` (same as continue) — no arbitrary profile.
- `max_job_runtime_seconds` clamped to [MIN, 7200] — never exceeds 7200.
- prompt bounded to MAX_PROMPT_CHARS; result redacted/budget-capped by existing machinery.
- session-control env gate (`HERMES_GPT_ENABLE_SESSION_CONTROL`) enforced.
- title optional; only set when provided and satisifies length bounds.