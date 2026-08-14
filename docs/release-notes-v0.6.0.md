# Hermes GPT v0.6.0 release notes

## Status

Release candidate only. This document does not authorize a tag, publication, connector re-registration, or live ChatGPT data transmission.

## Mission Control (M0)

v0.6.0 adds the read-only `hermes_mission_*` operational surface for bounded, audited fleet status. It uses per-surface authorization and excludes raw messages, prompts, request dumps, transcripts, memory bodies, profile secrets, and vault secrets.

Free-text failure, audit, cron, and delegation fields now receive a conservative PII-strip pass before surfacing: email addresses, phone-like contact numbers, `@` usernames, explicit identity labels, and common personal-name patterns are removed. Redaction can reduce diagnostic detail by design.

## Work Contracts (M1)

`hermes_contract_*` defines, dispatches, validates, and reports declarative work contracts using observed state rather than worker self-report. Validation remains fail-closed. Retry run selection is deterministic: the latest observed retry is authoritative. Forbidden-action audit checks are scoped to the specific contract task identity, preventing unrelated concurrent work from failing a contract.

Review evidence remains fail-closed: a distinct reviewer audit record or a human approval reference is required. There is no production accept/review-write tool in v0.6.0, so audit-based review evidence must be produced by an already-authorized external review path; otherwise validation returns `NOT_SATISFIED`.

## Swarm Orchestration (M2)

`hermes_swarm_*` provides bounded workflow orchestration on top of Work Contracts, including canonical DAG validation, capped scheduling, isolated-worktree plans, explicit ownership, dry-run/direct gates, and Codex verdict constraints. Codex is never an implementation owner and receives only bounded verdict material, never transcript or prompt bodies.

## Retention

See [retention-policy.md](retention-policy.md). Request dumps, Codex transcripts, M2 worktrees, and workflow/verdict records have concrete local retention windows and cleanup procedures. v0.6.0 does not install automatic deletion; cleanup is an audited maintainer operation.

## Remaining release gates

Before any live client transmission or public release, Tony must confirm the OpenAI account tier; if consumer ChatGPT is used, confirm training opt-out and/or Temporary Chat; approve the exact surface manifest and current data-usage terms; and approve the final release/tag action. See the counsel packet and release checklist.
