# Reuse / Do-not-rebuild Boundary

This package ships a **mission control plane** that is a supervised shell around
existing primitives. The boundary below is frozen from the
`hermes-gpt-vnext-architecture-proposal` (§2.4 Do-not-rebuild). Before adding a
component or capability, confirm it is not already a reuse target here. The rule is:
reuse first, build only the thin additive controller surface, and never rebuild an
existing primitive.

## Do not rebuild

- Host Kanban v1 (persistence, dispatch, workspaces, attachments, review, goal-mode,
  block kinds, workflow templates).
- Provider / inference routing, cron, audit, secret-path policy, workspace confinement.
- Sidecar: Missions durable lifecycle, Work Contracts, Swarm DAG, Fabric cross-machine
  execution, Fleet A2A + authority manifest, Delegations lineage, Events / live-events,
  review evidence, recovery / diagnostics, token store, Flight Deck presentation.
- The Operator authority model (levels, apply mode, confirm, owner ack, secret deny)
  and the Mission Control redaction / data-boundary invariants.
- A new execution engine or new agent intelligence; a second judge stack; a separate
  evidence database; proof-of-work / signed attestation beyond content-addressing +
  control-plane provenance; a second gateway / command plane / credential authority /
  fleet commander (SOUL boundary); a new host `verifying` status in slice 1.

## Standing rule

Reuse the existing Mission, Work Contract, Swarm, Fabric, Fleet, Delegation, Event /
live-event, review, recovery, and policy primitives as peers. The controller layer is a
caller, not a competing owner of any of these primitives.
