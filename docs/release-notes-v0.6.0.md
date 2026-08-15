# Hermes GPT v0.6.0 release notes

## Status

**Released.** `v0.6.0` is a final GitHub release, not a release candidate.

Distribution channels are independent. A GitHub release does not prove that the same version is already available from PyPI. Check the PyPI version badge/page before telling a user or agent that `pip install hermes-gpt` installs v0.6.0.

These notes describe the shipped v0.6.0 behavior. Pre-release design, risk, counsel, and planning artifacts under `docs/design/` and `docs/releases/` are retained for provenance but do not override the implementation, tests, or current operational docs.

## Mission Control (M0)

v0.6.0 adds the read-only `hermes_mission_*` operational surface for bounded, audited fleet status. It excludes raw messages, prompts, request dumps, transcripts, memory bodies, profile secrets, and vault secrets.

Mission Control surface authorization is controlled by `HERMES_GPT_MISSION_ALLOWED_SURFACES`:

- unset: all read-only Mission Control surfaces are available;
- comma-separated list: only listed valid surfaces are available;
- empty value: all Mission Control surfaces are denied.

Free-text failure, audit, cron, and delegation fields receive a conservative PII-strip pass before surfacing. Email addresses, phone-like contact numbers, `@` usernames, explicit identity labels, and common personal-name patterns are removed. Redaction can reduce diagnostic detail by design.

## Work Contracts (M1)

`hermes_contract_*` defines, dispatches, validates, and reports declarative work contracts using observed state rather than worker self-report. Validation is fail-closed.

Retry run selection is deterministic: the latest observed retry is authoritative. Forbidden-action audit checks are scoped to the contract task identity so unrelated concurrent work cannot fail a contract.

Review evidence also remains fail-closed. A distinct reviewer audit acceptance or a human approval reference is required when the contract requires review.

### Known v0.6 limitation

v0.6.0 does **not** ship a production review-accept writer. Audit-based review evidence must therefore be produced by an already-authorized external review path. If required review evidence is unavailable, validation returns `NOT_SATISFIED` rather than inventing acceptance.

## Swarm Orchestration (M2)

`hermes_swarm_*` provides bounded workflow orchestration on top of Work Contracts. It includes:

- canonical DAG validation;
- capped scheduling;
- isolated-worktree plans;
- explicit stage ownership;
- dry-run/direct mutation gates;
- bounded rework;
- Codex verdict constraints;
- final human approval.

Codex is never an implementation owner and receives only bounded verdict material, never raw transcript or prompt bodies.

## Retention

See [retention-policy.md](retention-policy.md).

- Request dumps have a 7-day maximum retention window.
- Codex job metadata/transcript artifacts use a 30-day retention window; `operator_codex` performs age-based cleanup during reconciliation.
- Terminal Swarm worktrees have a 7-day maximum window once declared artifacts are preserved.
- Terminal workflow/verdict records have a 30-day maximum window.

There is no general background deletion daemon for every artifact class. Non-Codex cleanup remains a bounded maintainer operation unless later implementation explicitly adds and tests automation.

## Security posture

Hermes GPT remains a local-first MCP sidecar for a trusted machine.

- Operator access is opt-in.
- Mutations are dry-run-first and explicitly gated.
- Owner Mode remains break-glass and does not bypass secret-path restrictions.
- Protected subprocess execution uses fixed argv and `shell=False`.
- Public unauthenticated Operator hosting remains unsupported.

For current setup and authority rules, use [Operator Mode](operator-mode.md). For the documentation authority map, use [docs/README.md](README.md).
