# Unified Delegation Lifecycle (v0.9)

Hermes GPT v0.9 adds a durable delegation lifecycle above Work Contracts and the existing execution backends. The delegation record is **lineage and state metadata**, not a second execution authority: runner, Fabric, Work Contract validation, and Operator policy remain authoritative for dispatch, observation, cancellation, and completion evidence.

## Tools

- `hermes_delegation_dispatch` — dry-run-first dispatch of a canonical Work Contract with optional Mission linkage.
- `hermes_delegation_get` — read one durable delegation and its bounded lifecycle events.
- `hermes_delegation_list` — list/filter delegation records by Mission or normalized state.
- `hermes_delegation_reconcile` — derive normalized state from authoritative runner/Fabric observations; applying the derived state requires workspace/direct authority.
- `hermes_delegation_cancel` — route cancellation to the selected backend while preserving existing backend cancellation and Operator gates.

Normalized states are `queued`, `running`, `reconciling`, `blocked`, `succeeded`, `failed`, and `cancelled`.

## Data and authority boundaries

The delegation database stores only bounded lineage and lifecycle metadata: delegation id, Mission id, Work Contract task id and SHA-256, selected backend, normalized/backend state, bounded backend references, timestamps, and event hashes. It does **not** store Work Contract objectives/prompts or model responses.

A delegation never marks work successful because a worker says it succeeded. Reconciliation reads the existing runner/Fabric observations for the Work Contract task id. When a Work Contract is supplied to reconciliation, its canonical SHA and task id must match the stored lineage before validation can contribute evidence.

Mission linkage uses the existing `delegation` attachment kind. Dispatch creates/updates a pending/running attachment, reconciliation updates that attachment to the normalized terminal or active state, and cancellation records `cancelled` only after the backend cancellation path returns success.

## OpenCode backend

v0.9 also adds `opencode` as a first-class local runner backend. Hermes invokes the installed non-interactive OpenCode CLI using `opencode run --format json --pure --dir <workspace>`. The objective is piped over stdin rather than placed on process argv. `--auto` is never enabled by Hermes.

Filesystem authority is enforced externally by the same Hermes confinement layer used for local coding runners. Read-only contracts receive read-only workspace confinement; write-authorized contracts require `workspace-write` posture. Optional `model`, `agent`, and `variant` execution options are bounded, and model selection remains subject to the existing runner model allowlist.

## Live events

Delegation lifecycle changes publish bounded wake-up events on the v0.9 live-event bus. These notifications are non-authoritative pointers back to durable delegation/runner/Fabric state; notification failure cannot advance or complete work.
