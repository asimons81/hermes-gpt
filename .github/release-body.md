## Two-Way Codex Bridge

Hermes GPT v0.5.0 connects Hermes Agent and Codex in both directions while keeping the local-first, dry-run-first safety model.

### Codex → Hermes (Operator toolset)

Codex can operate Hermes through an opt-in Operator control plane alongside the default `core` toolset:

```powershell
hermes-gpt codex install --toolset core
hermes-gpt codex doctor
```

```powershell
hermes-gpt codex install --toolset operator --refresh
hermes-gpt codex doctor
```

The Operator toolset exposes diagnostics, cron, skills, non-secret config/environment operations, and gateway controls. It deliberately excludes workspace, git, raw command, Owner patch, and Owner write tools. Existing Operator level and apply gates remain authoritative.

### Hermes GPT → Codex (Async jobs)

Trusted Hermes GPT clients can plan, start, list, poll, read, and cancel asynchronous Codex tasks and reviews with `hermes_codex_*` tools. Required execution posture:

```text
HERMES_GPT_OPERATOR_ENABLED=1
HERMES_GPT_OPERATOR_LEVEL=workspace
HERMES_GPT_OPERATOR_APPLY_MODE=direct
HERMES_GPT_OPERATOR_ALLOWED_PATHS=<approved roots>
HERMES_GPT_ENABLE_CODEX_RUNNER=1
HERMES_GPT_ALLOW_CODEX_WRITE=1  # only for workspace-write
```

### Runner safety

- Fixed argv, `shell=False`, bounded timeouts, approved work directories
- Only `read-only` or `workspace-write` sandboxes
- Danger-full-access, approval bypasses, arbitrary commands unsupported
- Raw prompts never stored — length and SHA-256 only
- Output recursively redacted and bounded
- Restart reconciliation — orphaned PIDs are marked, not signalled
- 30-day retention cleanup

### Core toolset remains backward compatible

All existing core tools (`hermes_status`, `hermes_capabilities`, `hermes_plan`, `hermes_vision_analyze`, `hermes_web_search`, `hermes_extract_page`, `hermes_cron_plan`, `hermes_cron_create`, `hermes_author_skill`, `hermes_gateway_diagnostics`) retain their established schemas.

### Other improvements

- `hermes-gpt update` — check-first, safe fast-forward updates for clean Git checkouts
- Version resolution consolidated in `versioning.py`
- Toolset-aware Codex connector install with backup-first `--refresh`
- Windows/Linux CI across Python 3.10, 3.11, and 3.12
- Trusted PyPI publishing after successful CI
- Updated Codex, Operator, and update documentation

### Security posture

Hermes GPT remains a standalone local MCP sidecar. Operator access is opt-in. Mutations are dry-run-first and explicitly gated. Codex runner jobs do not bypass Codex permissions. Workspace-write requires its dedicated write gate. Danger-full-access and bypass flags are impossible through the runner. Public unauthenticated hosting remains unsupported.
