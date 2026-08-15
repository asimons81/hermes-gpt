# Hermes GPT v0.6.0

Hermes GPT v0.6.0 adds Mission Control, observed-state Work Contracts, and bounded Swarm Orchestration on top of the existing local-first, dry-run-first safety model.

## Mission Control (read-only)

The `hermes_mission_*` surface gives a bounded, audited, read-only operational view of the Hermes deployment for trusted MCP clients. It is deny-by-default, never mutates state, and excludes raw messages, prompts, request dumps, transcripts, memory bodies, and secrets.

Free-text failure, audit, cron, and delegation fields receive a conservative PII-strip pass (emails, phone-like numbers, `@` usernames, identity labels, and personal-name patterns) before they can reach a client response.

## Work Contracts (fail-closed)

`hermes_contract_*` defines, dispatches, validates, and reports declarative work contracts from observed state rather than worker self-report. Validation rejects false "done" claims (S2). Retry selection is deterministic and forbidden-action audit checks are scoped to the contract's task identity.

## Swarm Orchestration (bounded)

`hermes_swarm_*` runs bounded, DAG-validated workflows on top of Work Contracts with capped scheduling, isolated worktree plans, explicit ownership gates, and Codex verdict constraints. Codex is never an implementation owner and receives only bounded verdict material.

## Retention

Request dumps, Codex transcripts/artifacts, M2 worktrees, and workflow/verdict records have documented retention windows and a maintainer-operated cleanup procedure (see `docs/retention-policy.md`). Codex job artifacts are auto-cleaned after 30 days.

## Security posture

Hermes GPT remains a standalone local MCP sidecar. Operator access is opt-in, mutations are dry-run-first and explicitly gated, and the Codex runner uses fixed argv, `shell=False`, bounded timeouts, and approved work directories. Danger-full-access, approval bypasses, and arbitrary commands are unsupported. Public unauthenticated hosting remains unsupported.
