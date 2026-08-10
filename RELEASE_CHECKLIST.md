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
- With `HERMES_GPT_ENABLE_SESSION_SEARCH=1`, confirm exactly four session-history tools are visible: `hermes_session_search`, `hermes_session_list`, `hermes_session_read`, and `hermes_session_export`.
- With the Hermes GPT Session History integration installed in Codex, confirm the four capabilities previously limited to the full ChatGPT connector appear under their exact native names.
- From Codex, run the clean smoke test in `docs/session-history.md`: list and search with small limits, then read and Markdown-export the same valid session ID with inactive/system/tool/lineage content disabled. Record only tool names and PASS/FAIL; redact transcript content and make no writes.
- Confirm session history defaults to `user`/`assistant`, and `HERMES_GPT_ENABLE_SESSION_INTERNAL_CONTENT=1` is required for `system`, `tool`, and `function` content.
- Confirm list/read/export pagination advances by rows examined, remains bounded, and cannot duplicate or loop when roles are filtered.
- Confirm JSON and Markdown export stay in memory, enforce `MAX_EXPORT_MESSAGES` and `MAX_RESPONSE_BYTES`, create no files, expose no paths, and fail closed for lineage.
- Confirm unavailable read-only FTS reports an unavailable/FTS limitation rather than “no matches,” with no FTS activation or rebuild.
- Review session-history output as private transcript data before sharing or exposing any MCP endpoint.
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
- Confirm README.md, docs/codex.md, docs/session-history.md, docs/session-control.md, and docs/updating.md describe any changed install, update, or safety behavior.
