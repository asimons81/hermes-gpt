# Release Checklist

Use this checklist before publishing any release artifact.

- `python -m py_compile server.py test_server.py`
- `python -m pytest`
- Run `hermes_release_doctor(full_tests=true)` and confirm status is `PASS` or only `WARN` (no `BLOCKED`).
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
