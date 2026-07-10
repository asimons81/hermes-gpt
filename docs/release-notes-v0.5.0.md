# Hermes GPT v0.5.0 — Two-Way Codex Bridge

Hermes GPT v0.5.0 connects Hermes Agent and Codex in both directions while keeping the local-first, dry-run-first safety model.

## Codex uses Hermes

The existing `core` connector remains the default and retains its established schemas. Trusted users can opt into the Operator control plane:

```powershell
hermes-gpt codex install --toolset core
hermes-gpt codex doctor

hermes-gpt codex install --toolset operator --refresh
hermes-gpt codex doctor
```

The Operator toolset exposes diagnostics, cron, skills, non-secret config/environment operations, and gateway controls. It deliberately excludes workspace, git, raw command, Owner patch, and Owner write tools. Registration never grants mutation permission; existing Operator level and apply gates remain authoritative.

## Hermes delegates to Codex

Trusted Hermes GPT clients can plan, start, list, poll, read, and cancel asynchronous Codex tasks and reviews with `hermes_codex_*` tools. Required execution posture:

```text
HERMES_GPT_OPERATOR_ENABLED=1
HERMES_GPT_OPERATOR_LEVEL=workspace
HERMES_GPT_OPERATOR_APPLY_MODE=direct
HERMES_GPT_OPERATOR_ALLOWED_PATHS=<approved roots>
HERMES_GPT_ENABLE_CODEX_RUNNER=1
HERMES_GPT_ALLOW_CODEX_WRITE=1
```

`HERMES_GPT_ALLOW_CODEX_WRITE` is required only for `workspace-write`; read-only jobs do not need it. Owner Mode is not required for routine jobs. If Operator level is explicitly `owner`, its normal break-glass acknowledgement still applies.

Jobs use fixed argv, `shell=False`, bounded timeouts, approved working directories, and only `read-only` or `workspace-write`. Danger-full-access, approval bypasses, arbitrary commands/configuration, executable paths, and additional directories are unsupported. Raw prompts are never stored in job metadata or audit records; only length and SHA-256 are retained. Returned output is recursively redacted and bounded.

Operator Mode is not a sandbox. Codex runner tools do not bypass Codex permissions. Unauthenticated public exposure remains unsupported.

## Release safety

Updates remain check-first. Every persistent action remains dry-run-first and requires both server-side posture and call-level opt-in. CI covers Windows and Ubuntu on Python 3.10, 3.11, and 3.12, and publishing depends on tests and artifact validation.
