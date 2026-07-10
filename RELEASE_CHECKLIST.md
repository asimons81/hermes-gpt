# Release Checklist

Use this checklist before publishing any release artifact.

- `python -m py_compile server.py test_server.py`
- `python -m pytest`
- `hermes-gpt update --help` and the check-only `hermes-gpt update` path work without modifying the checkout.
- Run `hermes_release_doctor(full_tests=true)` and confirm status is `PASS` or only `WARN` (no `BLOCKED`).
- Run the Windows/Linux Python 3.10-3.12 CI matrix before publishing.
- Confirm `python -m build` and `python -m twine check dist/*` pass and artifacts include the runner, version helper, public docs, and final release notes.
- Confirm runner metadata and audit records contain no raw prompts and no danger/bypass argv can be constructed.
- Confirm default tools exclude write, patch, terminal, and session search.
- Confirm `--profile remote` refuses to start without the explicit unsafe bypass.
- Confirm no private files are present:
  - `*.pem`
  - `*.log`
  - `*.err.log`
  - `.env`
  - `__pycache__/`
  - `.pytest_cache/`
- Confirm README still states that unauthenticated public exposure is not release-safe.
- Confirm CHANGELOG.md mentions the new version.
- Confirm docs/operator-mode.md documents the new diagnostic/recovery tools.
- Confirm README.md, docs/codex.md, and docs/updating.md describe any changed install, update, or safety behavior.
