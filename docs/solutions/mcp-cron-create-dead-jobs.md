---
title: "MCP cron_create produces dead jobs that never fire"
category: cron
date_discovered: 2026-08-31
date_resolved: null
status: workaround
related_commits: []
---

# MCP `cron_create` produces dead jobs that never fire

## Symptom

Jobs created via the hermes-gpt **MCP `cron_create` tool** land in
`~/.hermes/cron/jobs.json` with an empty schedule (`schedule: {}`), state
`"scheduled"`, and `next_run_at: null`. They sit in that state forever and
**never fire**.

## Root cause

The root cause lives in the MCP layer's job serialization: the schedule is not
carried through when the MCP tool serializes the job into `jobs.json`.

## Detection

Query the cron list and flag any job that has **`next_run_at: null` together
with state `scheduled`**. That combination is a dead job.

## Fix procedure

Recreate the job natively with the **cronjob tool** (not the MCP tool), then
remove the corpse:

1. Create the job natively with the cronjob tool, providing `schedule`,
   `script`, `workdir`, and `no_agent`.
2. Remove the dead entry left behind by `cron_create`.

## Warning

**Never hand-edit `~/.hermes/cron/jobs.json`.** The live gateway rewrites that
file — manual edits are lost or cause state divergence. Always go through the
cronjob tool.
