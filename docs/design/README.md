# Design artifacts

Files in this directory preserve technical design work produced before or during implementation.

## Current v0.7 Flight Deck artifacts

- `v0.7-flight-deck-architecture.md` — v0.7 architecture and technical design (current design for the v0.7 release cycle; pending G2 review).
- `v0.7-flight-deck-adrs.md` — architecture decision records for v0.7 (D1–D4 and supporting decisions).
- `v0.7-flight-deck-interaction-design.md` — Flight Deck interaction and visual design: user flows (launch/monitor, event history, evidence review, authority), wireframes, visual direction, interaction specs, and reusable asset list (kanban t_dd366ab6).
- `v0.7-flight-deck-wireframes.html` — self-contained wireframe gallery for the Flight Deck views.
- `assets/v0.7-flight-deck-wireframes/*.svg` — individual Flight Deck wireframes.
- `v0.7-flight-deck-ui-contract.md` — Flight Deck read-only presentation contract (D2-A): data sources, no-new-authority rule, redaction, allowlist semantics, action-to-tool mapping.

These are **design authority, not runtime authority**: verify any implementation
claim against the current module and its tests before acting.

## Active implementation notes

- `v0.9-budget-hardblock.md` — mission-scoped spend envelope (`budget_accounts` + `budget_check`, Phase 2 dry-run) and the D3 hard-block path (pause Mission + `budget_breaker` signal) designed for Phase 4/5, flag-default-off (kanban t_78e597c6).
- `v0.9-placement-scoring.md` — deterministic filter-and-score placement over the derived capability-manifest index (D7): hard filters (enabled/reachability/identity, features, authorization ceiling, profile/workspace scope, concurrency headroom, policy) then soft scores (capability_fit, authorization_match, affinity, load_headroom, health, cost_priority), `no_capable_target` escalation stub, dry-run only (kanban t_167ac591).
- `v0.9-failure-semantics.md` — Ops 8-class semantic failure taxonomy as a thin classifier over authoritative observations (delegation state/verdict, runner observed_runs, worker-exit vocabulary, refined `_RESPAWN_BLOCKER_RE` token vocabularies, live capability negotiation) plus the deterministic smallest-first recovery matrix (§11.2); decision output only — `would_execute` always false, the sole durable write is a `controller_plan` row (kanban t_49bbc143).
- `v0.9-controller-loop.md` — the supervised mission controller reconciler loop (shadow/observe, §17 item 6 / §7 / D2 / D10): T1–T5 trigger model, per-mission pass lease (TTL = min(max_pass_duration, interval*2), heartbeat-renewed, crash-reclaimed), conflation, idempotency, fail-closed classification, smallest-action recovery proposal; decision output only, the only durable writes are `controller_plan` + `controller_telemetry` + pass lease (kanban t_ad1e6d07).
- `v0.9-controller-telemetry-health.md` — §17 item 8 / §12.2 observability: completed aggregate health (24h-windowed counters, true stale-lease-reclaim accounting via telemetry columns), deterministic GREEN/YELLOW/RED per-pass tiers + controller rollup, RED → fleet-attention INTERRUPT envelope spool for the existing delivery-broker lane (no self-send), INV-10 loudness (stale heartbeat = RED, spool failure raises) (kanban t_e8468723).
- `pluggable-runner-architecture.md` — execution backend boundary for work contracts and swarm stages, including Pi RPC, OMX, Fleet/Codex compatibility adapters, third-party runner registration, observed-state validation, and deployment/source-of-truth policy.

## Historical v0.6 artifacts

The following preserve v0.6.0 design work. They are **historical design
authority, not current runtime authority**.

When a design statement differs from the current implementation or tests, use the implementation/tests as the source of truth and update current operational documentation if needed.

Agents should read [../README.md](../README.md) before using these files.

Use these documents for:

- architecture rationale;
- intended invariants;
- threat/risk assumptions that informed implementation;
- understanding why a current behavior exists.

Do not use them alone to determine:

- current release status;
- current distribution-channel availability;
- exact current tool schemas;
- exact current environment defaults;
- whether a pre-release gate is still pending.

Before turning design text into a code or documentation change, verify the relevant implementation module and tests.
