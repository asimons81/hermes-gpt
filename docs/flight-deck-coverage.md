# Flight Deck v0.7 coverage checklist

This checklist records the browser-facing Flight Deck adapter and UI coverage
implemented in `ui_ops.py` and `web/src/flight/`. It is an implementation
handoff, not a new authority model. The current policy contract remains
[Operator Mode](operator-mode.md).

## Reachable read surfaces

- [x] Mission Control: overview, health, profiles, fleet, Codex, cron,
  delegations, failures, approvals, vault, usage, and audit through
  `GET /api/ops/:surface`.
- [x] Event History query and bounded tail through `GET /api/events`.
- [x] Contracts and review-acceptance evidence through
  `GET /api/ops/contracts`, `GET /api/ops/contracts/:contract_sha256`, and
  `GET /api/ops/review/:contract_sha256`.
- [x] Swarm workflow list and detail through `GET /api/ops/swarm` and
  `GET /api/ops/swarm/:workflow_id`.
- [x] Codex job and cron-job detail routes.
- [x] Fleet status, policy summary, and OAuth-store presence/expiry status
  without token material.

## Safety and state handling

- [x] Browser payloads use existing bounded/redacted operator read models.
- [x] Mission allowlist semantics are retained: unset permits all read-only
  surfaces, a list restricts to listed surfaces, and an empty value denies all.
- [x] UI panels render loading, unavailable/empty, stale, and error/retry
  states.
- [x] Event History is explicitly poll-driven because the current backend has
  no push event stream.
- [x] Every supported mutation uses `POST /api/ops/action`, with a strict
  per-tool argument allowlist and server-side root resolution.
- [x] Mutation flow is dry-run first; confirmation is a second explicit user
  gesture and is passed only as the existing tool's `confirm` argument.
- [x] Existing operator level, direct-mode, confirmation, audit, and secret-path
  protections remain authoritative. The Flight Deck adds no authority bypass.
- [x] Blocking cron execution returns `202 Accepted` and runs off the request
  path; the UI refreshes the existing cron read model for status.

## Intentional adapter boundaries

- No independent contract registry exists; contract list/detail are a bounded
  composition of existing review evidence and swarm workflow references.
- No generic approve/reject writer exists in the current backend; the UI only
  exposes existing gated tool actions.
- No read-only review-acceptance list tool exists; the adapter reads the
  existing bounded review-evidence store.

## Verification

- `test_ui_ops.py`: mission/status envelopes, allowlist behavior, event query,
  route composition, and adversarial mutation-gate tests.
- `web` Vitest: surface rendering states, operator/event/approval stores, and
  dry-run-to-confirm flows.
- `web` production build: TypeScript and Vite bundle validation.
