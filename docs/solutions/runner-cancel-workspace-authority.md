---
title: "Delegation runner-cancel fails with RUNNER_CANCEL_ERROR 'Workspace writes are disabled'"
category: operator-delegation
date_discovered: 2026-08-31
date_resolved: 2026-08-31
status: resolved
related_commits: []
---

# Delegation runner-cancel lacks workspace-write authority

## Symptom

Delegation **runner-cancel always failed** with:

```
RUNNER_CANCEL_ERROR "Workspace writes are disabled"
```

## Root cause

Gateway-spawned MCP sessions had **no workspace-write authority**: the
operator policy environment lacked `HERMES_GPT_OPERATOR_ALLOWED_PATHS`, so the
operator policy's workspace-write gate denied the cancel path.

## Fix procedure (applied 2026-08-31)

Add to `~/.config/hermes-gpt/hermes-gpt.env`:

```
HERMES_GPT_OPERATOR_ALLOWED_PATHS=/
```

Notes on that file:

- `~/.config/hermes-gpt/hermes-gpt.env` is the **canonical** operator policy
  env file.
- It is sourced by `~/.local/bin/hermes-gpt-cliproxy-only` for gateway MCP
  stdio sessions **and** by the systemd sidecars — one edit converges both.

## Convergence

The variable takes effect **only for MCP sessions spawned after a gateway
restart**. After editing the env file, a **gateway restart is required** for
new MCP sessions to pick up the authority.
