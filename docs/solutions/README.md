# Solutions runbooks

This directory is the durable record of **verified problems and their fixes** for
hermes-gpt deployments — one runbook per problem (the "mothership pattern"). A
runbook is written only after the fix has been verified against the live system,
and it records the facts exactly as verified: real paths, real error strings,
real dates.

When the same problem recurs, these runbooks are the fastest path from symptom
to fix. When a root cause is later fixed in code, update the runbook's
frontmatter and status rather than deleting it.

## Frontmatter convention

Every runbook starts with a YAML frontmatter block:

```yaml
---
title: Short imperative summary of the problem
category: short-freeform-tag
date_discovered: YYYY-MM-DD
date_resolved: YYYY-MM-DD | null
status: resolved | workaround | open
related_commits: [list of repo SHAs/refs, [] if none]
---
```

| Field | Meaning |
| --- | --- |
| `title` | One-line problem statement, exact symptom included where possible. |
| `category` | Short free-form tag for grouping (e.g. `cron`, `gateway-health`, `operator-delegation`). |
| `date_discovered` | Date the problem was first verified in the live environment. |
| `date_resolved` | Date a fix was verified, or `null` if only a workaround exists. |
| `status` | `resolved` (root cause fixed), `workaround` (documented operational fix; root cause still present), `open` (no verified fix yet). |
| `related_commits` | Repo commits that implement or relate to the fix; `[]` when the fix was purely environmental/operational. |

## Writing rules

- One problem per file; filename is kebab-case and describes the problem.
- Keep facts exact: real paths, exact error strings, exact environment variable
  and tool names, verified dates. No approximations.
- Structure each runbook as: **Symptom**, **Root cause**, **Detection**,
  **Fix procedure**, and any **Warnings / prevention**.
- When adding a runbook, add it to the index table below.

## Index

| Runbook | Status | Discovered |
| --- | --- | --- |
| [mcp-cron-create-dead-jobs.md](mcp-cron-create-dead-jobs.md) | workaround | 2026-08-31 |
| [gateway-state-clobbered-by-pytest.md](gateway-state-clobbered-by-pytest.md) | resolved | 2026-08-31 |
| [runner-cancel-workspace-authority.md](runner-cancel-workspace-authority.md) | resolved | 2026-08-31 |
