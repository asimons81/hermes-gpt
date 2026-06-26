# hermes-gpt v0.3.0 — Operator Reliability Release

## Summary

v0.3.0 makes Hermes GPT self-diagnosing, safely recoverable, and release-checkable. It adds four new operator tools, a structured error envelope for all operator-facing failures, and PASS/WARN/FAIL/UNSUPPORTED status reporting.

## Highlights

- `hermes_operator_doctor` — read-only deep health check across operator surfaces.
- `hermes_operator_snapshot` — single current-state summary.
- `hermes_release_doctor` — release readiness with PASS / WARN / BLOCKED classification.
- `hermes_operator_recover` — conservative dry-run-first recovery sequence.
- Structured error envelope: `{success, ok, error, layer, code, safe_message, suggested_action, trace_id}`.
- All operator module exception handlers converted to the envelope while preserving legacy fields.
- PASS / WARN / FAIL / UNSUPPORTED status vocabulary.
- Connector re-registration explicitly reported as unsupported.
- Secret values and absolute paths redacted from error messages.
- Version bumped to 0.3.0.

## Safety model

- `hermes_operator_recover` defaults to dry-run.
- Actual mutation requires `apply=true`, direct apply mode, and operator level `workspace` or higher.
- Connector re-registration is not invented; it is reported as UNSUPPORTED with a manual action.
- No secret values are exposed by doctor, snapshot, release-doctor, or recover tools.

## Verification

- `pytest`: pass
- `py_compile`: pass
- New tools registered and visible via MCP list-tools.
- Error envelope tests verify required fields and secret/path redaction.
- Release doctor tests verify PASS / WARN / BLOCKED classification.

## Breaking changes

- None known. Legacy `success` and `error` fields are preserved in error responses.

## Known limitations

- Connector re-registration is not implemented; use manual MCP client reconnection.
- `hermes_release_doctor(full_tests=true)` runs the local pytest suite and may be slow.
- Release doctor classifies a dirty working tree as WARN, not BLOCKED.
