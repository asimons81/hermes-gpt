# Flight Deck v0.8 coverage checklist

This checklist records the browser-facing Flight Deck adapter and UI coverage
implemented in `ui_ops.py`, `ui_fabric.py`, `operator_fabric_view.py`, and
`web/src/flight/`. It is an implementation handoff, not a new authority model.
The current policy contract remains [Operator Mode](operator-mode.md).

## Reachable read surfaces

- [x] v0.9 first-class Missions through `GET /api/ops/missions`, Mission detail,
  Mission-filtered cursor/long-poll wakeups, and delegation detail. The browser
  re-reads durable Mission state after a wakeup and exposes no Mission mutation
  path.
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
- [x] Fabric node roster through `GET /api/ops/fabric/nodes`, showing enrolled
  identity, coordinator-owned capability freshness, bounded capability summary,
  active/capacity observations, authority ceiling, and explicit
  observed/stale/unknown/disabled state without a peer RPC.
- [x] Fabric remote attempt list/detail through `GET /api/ops/fabric/attempts`
  and `GET /api/ops/fabric/attempts/:attempt_id`, including node/backend,
  explicit-vs-auto placement, retry lineage, blocker/error state, write-epoch
  authority summary, bounded durable/reconciled peer observations of write-claim
  and execution-unit state, admitted evidence provenance, admitted artifact
  metadata, and bounded Fabric audit history. Peer observations are labeled as
  observations rather than coordinator authority or completion verdicts;
  `LOST_AMBIGUOUS` is always presented as a blocker.
- [x] Durable auto-placement receipts through `GET /api/ops/fabric/routing`.
  G4-D receipts persist hard requirements, bounded candidate exclusions,
  selected target, and deterministic rank. Older G4-B/G4-C receipts remain
  readable and explicitly report that the detailed explanation was not
  persisted rather than reconstructing or guessing it.

## Fabric safety and trust-boundary handling

- [x] `operator_fabric_view.py` is observational by construction: it does not
  call peer RPC, poll, reconcile, cancel, retry, collect evidence, collect
  artifacts, dispatch work, or create the coordinator journal.
- [x] Fabric browser routes live in a dedicated GET-only sibling adapter.
  `ui_ops.py` and its mutation allowlist are unchanged by G4-D.
- [x] Fabric views never surface A2A URLs, bearer credentials, coordinator
  principal secrets, local workspace mappings, artifact snapshot paths, or
  coordinator admission paths.
- [x] Active remote HTML/SVG/JavaScript is never rendered in the trusted Flight
  Deck origin. The UI receives only bounded metadata plus an
  `isolated_metadata_only` policy marker.
- [x] Remote worker observations remain evidence inputs, never a completion
  verdict. The UI states that coordinator validation remains authoritative.
- [x] Stale/unavailable/ambiguous/reconciling/evidence-pending states are shown
  explicitly and never upgraded to optimistic green by presentation logic.
- [x] Router explanation fields are coordinator-generated, closed/bounded, and
  contain no raw peer logs or caller filesystem/network targets.
- [x] Any future Fabric intervention must continue through an existing gated
  Hermes operator tool using the same dry-run/confirm semantics. G4-D adds no
  browser-only peer mutation endpoint.

## General safety and state handling

- [x] Browser payloads use existing bounded/redacted operator read models and
  the shared `ui_security` redaction envelope.
- [x] Mission allowlist semantics are retained: unset permits all read-only
  surfaces, a list restricts to listed surfaces, and an empty value denies all.
- [x] UI panels render loading, unavailable/empty, stale, and error/retry
  states.
- [x] Event History is explicitly poll-driven because the current backend has
  no generic push event stream.
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
- Fabric node health on the G4-D roster is coordinator-observation freshness,
  not a hidden live probe. Live verification remains part of dispatch/routing
  correctness rather than presentation polling.
- The coordinator's v0.8 journal records whether a write epoch was granted but
  not the exact original write authorization subclass. Flight Deck therefore
  labels this authority summary as coarse instead of inventing
  reversible-write vs high-impact detail.

## Verification

- `test_ui_ops.py`: mission/status envelopes, allowlist behavior, event query,
  route composition, and adversarial mutation-gate tests.
- `test_operator_fabric_view.py`: stale/unknown node semantics, non-mutating
  journal reads, routing-receipt compatibility, evidence redaction, artifact
  path isolation, and active-content policy.
- `test_ui_fabric.py`: GET-only Fabric routes, shared browser redaction,
  invalid-id handling, route composition, and no private artifact path leak.
- `web` Vitest: Fabric stale/blocked rendering, routing explanations, active
  artifact isolation, absence of direct mutation controls, plus existing
  surface/operator/event/approval tests.
- `web` production build: TypeScript and Vite bundle validation.
