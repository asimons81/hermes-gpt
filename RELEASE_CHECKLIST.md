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
  - `docs/windows-chatgpt-codex.md`
  - `docs/updating.md`
  - `docs/retention-policy.md`
  - current release notes
- Run `python tools/check_package_hygiene.py dist/*` and require exit `0` / `CLEAN`.
- Confirm package artifacts contain no absolute private machine paths, RFC1918/Tailscale addresses, machine hostnames, live operational metrics, or private release-planning packets.

## 5. Security invariants

- Confirm default tools exclude write, patch, terminal, and session search unless their explicit gates are enabled.
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
- restart/reconnect any live MCP deployments whose tool schema changed.
