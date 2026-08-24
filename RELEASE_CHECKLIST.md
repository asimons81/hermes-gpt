# Release Checklist

Use this checklist before publishing a Hermes GPT release artifact.

## 1. Establish release state

- Confirm the intended version in `pyproject.toml`.
- Confirm `CHANGELOG.md` contains that version.
- Confirm the release notes describe a final release rather than a candidate once publication is approved.
- Read `docs/README.md` and confirm current operational docs are the documents being updated, not historical files under `docs/design/` or `docs/releases/`.
- Verify each distribution channel separately. A GitHub release does not prove the same version is already available from PyPI.

## 2. Core verification

- `python -m py_compile server.py test_server.py`
- `python -m pytest`
- `python -m pytest test_operator_mission.py`
  - Mission Control redaction, no-raw-body, read-only, audit, bounds, and allowlist behavior must be green.
- `python -m pytest test_operator_contract.py`
  - Work Contract schema, observed-state validation, false-done rejection, test gating, redaction, read-only behavior, and audit must be green.
- `python -m pytest test_operator_swarm.py`
  - DAG validation, scheduler caps, contract dispatch, observed completion, bounded rework, worktree plans, Codex review posture, approval gate, mutation gates, audit, and redaction must be green.
- `python -m pytest test_operator_codex.py`
  - Codex executable resolution, runner gates, bounded argv, redaction, and retention behavior must be green.
- `python -m pytest test_operator_session.py`
  - Session-control gating, prompt non-persistence, fixed argv, concurrency bounds, timeout/result bounds, redaction, and restart orphan reconciliation must be green.
- `python -m pytest test_operator_mission_runtime.py test_operator_delegations.py test_operator_live_events.py test_operator_finance.py test_operator_job_supervisor.py`
  - First-class Missions, unified delegation lineage, durable live-event delivery, bounded Finance bridge behavior, and restart-safe durable job supervision must all be green.
- Run the Windows/Linux Python 3.10-3.12 CI matrix.

## 3. Release doctor

- Run `hermes_release_doctor(full_tests=true)`.
- Require `PASS` or understood non-blocking `WARN` results.
- Do not publish with `BLOCKED`.
- Confirm the server is not accidentally left in direct/Owner posture as part of release preparation.

## 4. Package build and hygiene

- `python -m build`
- `python -m twine check dist/*`
- Confirm wheel and sdist contain the current public docs expected by `pyproject.toml` / `MANIFEST.in`, including:
  - `README.md`
  - `docs/README.md`
  - `docs/operator-mode.md`
  - `docs/codex.md`
  - `docs/session-history.md`
  - `docs/session-control.md`
  - `docs/windows-chatgpt-codex.md`
  - `docs/updating.md`
  - `docs/retention-policy.md`
  - current release notes
- Run `python tools/check_package_hygiene.py dist/*` and require exit `0` / `CLEAN`.
- Confirm package artifacts contain no absolute private machine paths, RFC1918/Tailscale addresses, machine hostnames, live operational metrics, or private release-planning packets.

## 5. Security invariants

- Confirm default tools exclude write, patch, terminal, session search/history, and session control unless their explicit gates are enabled.
- With `HERMES_GPT_ENABLE_SESSION_SEARCH=1`, confirm exactly `hermes_session_search`, `hermes_session_list`, `hermes_session_read`, and `hermes_session_export` are exposed for session history.
- Confirm session history defaults to `user`/`assistant`; internal roles require `HERMES_GPT_ENABLE_SESSION_INTERNAL_CONTENT=1`.
- Confirm session export stays in memory, enforces message/response bounds, creates no files or paths, and fails closed for lineage.
- Confirm unavailable session FTS/search reports an unavailable limitation rather than falsely reporting no matches.
- Confirm session-control jobs use fixed argv with `shell=False`, persist no raw prompt, enforce concurrency/timeout/result bounds, redact results, and fail closed on restart reconciliation.
- Confirm Mission Control never returns raw messages, memory bodies, transcripts, request dumps, credentials, tokens, or profile-secret bodies.
- Confirm Mission Control allowlist semantics match implementation:
  - unset = all read-only Mission surfaces;
  - explicit list = listed valid surfaces only;
  - empty = none.
- Confirm runner metadata and Operator audit records contain no raw prompts.
- Confirm danger/bypass argv cannot be constructed through protected execution paths.
- Confirm Owner Mode still denies secret paths.
- Confirm `--profile remote` refuses to start without the explicit unsafe bypass.
- Confirm README still states public unauthenticated Operator exposure is unsupported.

## 6. Retention

- Confirm `docs/retention-policy.md` matches current implementation.
- Confirm Codex artifact cleanup still uses the implemented 30-day reconciliation window.
- Confirm request-dump and Swarm cleanup windows are actionable and are not falsely described as a global automatic deletion daemon.

## 7. Update behavior

- `hermes-gpt update --help`
- Run the check-only `hermes-gpt update` path and confirm it does not modify the checkout/package.
- Confirm Git checkout updates remain clean-tree, default-branch, fast-forward-only.
- Confirm installed-package updates check PyPI independently of GitHub release state.

## 8. Private-file scan

Confirm release artifacts do not contain:

- `*.pem`
- `*.key`
- `*.log`
- `*.err.log`
- `.env` / `.env.*`
- auth/token/cookie files
- `__pycache__/`
- `.pytest_cache/`
- internal `docs/design/*`
- internal `docs/releases/*`

## 9. Documentation consistency

Before publication, inspect at minimum:

- `README.md`
- `docs/README.md`
- `docs/operator-mode.md`
- `docs/codex.md`
- `docs/session-history.md`
- `docs/session-control.md`
- `docs/windows-chatgpt-codex.md`
- `docs/updating.md`
- `docs/retention-policy.md`
- current release notes
- `CHANGELOG.md`

Check exact tool names and environment variables against the implementation. In particular, do not confuse main-server `hermes_web_extract` with Codex-focused MCP `hermes_extract_page`.

## 10. Publication

Only after the checks above pass:

- create/push the intended tag;
- publish the GitHub release artifacts;
- publish to PyPI as a separate explicit action when intended;
- verify the public version on each published channel;
- restart/reconnect any live MCP deployments whose tool schema changed;
- for ChatGPT MCP apps/connectors, refresh or rescan the app actions after any tool-schema change and review/enable newly discovered actions before acceptance. ChatGPT may retain a frozen approved tool snapshot even after the MCP server and tunnel restart; if the workspace/plan does not expose an action refresh, recreate/re-publish the app connection;
- verify the refreshed ChatGPT action count/set against the live MCP `tools/list`, then prove representative newly added tools from a new chat;
- run the automated connector acceptance gate `python -m pytest -q test_server.py::test_v09_connector_surface_acceptance` (Finance enabled, optional env gates neutralized): it must report the exact expected tool count (110 for v0.9) with all release additions present, no duplicates, and `serverInfo.version` equal to `versioning.VERSION` for this checkout;
- confirm the advertised `serverInfo/version` seen by a real MCP client handshake (not just the SDK default) matches the version being published before accepting the connector.
