# Autopilot (v0.14)

Autopilot runs one Mission through its MissionPlan without a human re-triggering every node: it places nodes on capable peers, runs independent nodes in parallel, observes and validates their results, retries or replans within hard bounds, and stops at every approval boundary. It is **default off** and it adds no authority. Hermes GPT remains the only authority boundary; Autopilot is a caller of the existing Mission, plan, placement, Work Contract, delegation, budget, and live-event surfaces.

In v0.14.0, nodes can declare artifact size/digest requirements, and confirmed artifact failures after successful execution can enter bounded recovery after a durable delivery grace. See [Declared deliverable acceptance](#declared-deliverable-acceptance-v014) below.

This is the current operational guide. `docs/design/v0.13-autopilot.md` records design intent and the findings behind it; where they disagree, the code and tests win.

## Enabling it

Autopilot needs both of these, in addition to the normal Operator policy:

- the machine gate `HERMES_GPT_AUTOPILOT=1` (read live; unset means off). While it is unset the three tools below are **not registered**, so the default connector surface is unchanged at 137 tools; with it set there are three more.
- per call: Operator `workspace` level, direct apply mode, `dry_run=false`, and `confirm=true`. A dry run previews the start and writes nothing.

## Tools

| Tool | Access | What it does |
| --- | --- | --- |
| `hermes_autopilot_start` | dry-run first; direct needs `confirm=true` and the machine gate | Validates the Mission, then starts one detached worker process for it. Idempotent while a run is live. |
| `hermes_autopilot_status` | read-only | The run, its worker, and a derived `summary` (below). Reconciles worker liveness first. |
| `hermes_autopilot_stop` | dry-run first; direct needs `confirm=true`; the machine gate is not required | Stops the worker. Stopping is never gated behind starting. |

`hermes_autopilot_start` refuses, before writing anything, unless the Mission exists and is not terminal, a MissionPlan exists with at least one node, and every node's required skills resolve through the canonical skill resolver.

### Start parameters

| Parameter | Default | Range | Meaning |
| --- | --- | --- | --- |
| `max_concurrency` | 3 | 1-16 | Nodes running at once. |
| `max_replans` | 2 | 0-10 | Rework replans allowed for the run. |
| `max_attempts_per_node` | 3 | 1-10 | Attempts per node, first attempt included. |
| `max_runtime_seconds` | 86400 | 60-604800 | After this the run starts no new work, drains what is in flight, and ends. |

## What the worker does

The worker survives an MCP server restart or disconnect: it is a separate process, and every status read re-verifies it rather than trusting a cached record. Each pass (a "tick") it holds the controller's per-mission lease and:

1. **Observes** every in-flight node through `hermes_delegation_reconcile`, which reads runner and Fabric records and never the worker's own claim of success. A node completes only if its Work Contract validates `SATISFIED` against observed state and the evidence is bound to the contract hash. Anything missing or partial completes nothing.
2. **Schedules** ready nodes, in node-id order, up to `max_concurrency`, through the existing chain: placement scoring, contract validation, then the delegation dispatch. There is no separate dispatch path. Dispatch is idempotent: an exact retry resolves to the same delegation, and a crash between dispatch and the node update is adopted on the next pass rather than dispatched again.
3. **Reports** its state and a derived summary.

Ticks are event-driven where possible (it long-polls the Mission's live events) with a periodic backstop, `HERMES_GPT_AUTOPILOT_IDLE_SECONDS` (default 2.0, clamped to 0.5-60). Events only shorten the wait: no event is ever treated as proof, and every wakeup re-reads durable state.

## What it will never do

- **Cross an approval boundary.** Approval nodes and `high_impact` nodes are never dispatched or advanced. Autopilot reports `waiting_for_owner` and dispatches nothing past that node; independent branches keep running. You resolve it with the existing tools, and Autopilot resumes.
- **Approve or complete the Mission.** Once every node is done it calls the existing Mission reconcile, which stops at `awaiting_approval` while final approval is required. Only `hermes_mission_approve`, in Owner mode, completes the Mission. The worker holds no Owner authority.
- **Dispatch past a budget.** Before each new dispatch (retries included) it requires the budget envelope to be verifiably within its limit, and holds otherwise, whether or not the budget hard-block gates are armed. An unreadable or invalid envelope also holds. An account-less Mission is unrestricted. Observation of running work continues while held.
- **Write against a replaced plan.** Its plan writes are compare-and-swap on the plan version; a replaced plan aborts the pass.
- **Run two schedulers for one Mission.** One run per Mission, and the controller's per-mission lease is held for each pass.

## Recovery

Every failure is classified by the existing failure classifier; Autopilot adds no opinion of its own.

- A **transient** failure (for example a rate limit or a reset connection) is retried, with backoff (base 30s, cap 15 minutes), on another capable peer when one exists, up to `max_attempts_per_node`.
- A **semantic** failure with an eligible replan proposal is replanned, up to `max_replans`: the failed node is kept as history and a rework clone with the same definition takes its place. A replan never touches a completed node, lowers an authorization class, or removes an approval node.
- Everything else (authority, capability, environment, ambiguous, or an unclassifiable failure such as a bare "worker crashed") fails the node and is reported as needing a human. It is never retried.

A failed attempt normally fails the whole Mission when reconciled, so a replaced attempt is marked superseded once its successor exists. See [missions.md](missions.md) for how that marker is protected against misuse.

## States and status

Run states: `starting`, `running`, `waiting_for_owner`, `stopping`, `stopped`, `completed`, `failed`. `waiting_for_owner` means the approval frontier (or the Mission's own approval gate) is reached and nothing else can proceed.

`hermes_autopilot_status` returns the run and worker as before, plus a derived, read-only `summary`: progress, in-flight workers, the approval frontier, the budget view, recovery counters, limits and runtime remaining, wake-up counters, and an `attention` list with `needs_owner`. Building the summary never enforces a budget or writes anything, and if it cannot be built it degrades to `{"available": false}`.

An unreadable durable run is an error, not a claim that no run exists. It also prevents plan replacement until the run can be inspected. Transient Windows file-sharing contention is retried within a bounded interval; process observation does not hold the job's terminal writer lock.

Attention items marked `owner` are the ones only a human can resolve: the Mission awaiting approval, a gated node, a budget that is crossed, invalid or unreadable, and an unrecovered failed node. An expired runtime or a silent worker is informational.

## Flight Deck

`GET /api/ops/missions/{mission_id}/autopilot` shows the run read-only in the browser. It writes nothing, reports a dead worker as stale rather than as running, and returns only an allow-listed set of fields. See [flight-deck-missions.md](flight-deck-missions.md).

## Data and persistence

Run state lives under the Hermes data root in `autopilot/<mission_id>.json` (mode 0600, written atomically under a lock) and holds orchestration metadata only: limits, counters, worker liveness, and recovery bookkeeping. Mission, plan, delegation, and evidence stores stay the source of truth, and raw prompts, transcripts, and credentials are never stored.

## Verification

- `python -m pytest test_v014_acceptance.py` checks declared requirements, durable delivery grace, bounded recovery, changing/unreadable evidence, and Windows file observations.
- `python -m pytest test_operator_autopilot.py test_operator_autopilot_scheduler.py test_operator_autopilot_advance.py test_operator_autopilot_frontier.py test_operator_autopilot_recovery.py test_operator_autopilot_limits.py test_operator_autopilot_wakeup.py test_operator_autopilot_status.py`
- `python -m pytest test_operator_mission_supersede.py test_operator_plan_rework.py test_ui_autopilot.py`
- `python -m pytest test_operator_autopilot_acceptance.py` runs the end-to-end scenario with a real detached worker, a killed peer, an approval stop and resume, and an MCP server restarted mid-Mission.

## Declared deliverable acceptance (v0.14)

Nodes can add optional `artifact_requirements` for basenames already listed in `expected_artifacts`:

```json
"expected_artifacts": ["report.md"],
"artifact_requirements": [{"path": "report.md", "min_bytes": 100, "max_bytes": 100000}]
```

An optional `sha256` must be a lowercase 64-character expected content digest. Unknown keys, duplicate requirement paths, paths absent from `expected_artifacts`, nonpositive minimums, and inconsistent size bounds are refused before dispatch. Requirements participate in the node signature and the Work Contract digest, and rework preserves them. Omitting the new field preserves legacy node signatures. Every declared artifact remains required and nonempty.

The Work Contract validates size and optional expected digest from observed local files or contract-bound coordinator-verified remote artifact metadata. Local hash reads are bounded to 8 MiB and reject changing, unreadable, or nonregular files. Larger local files needing hashing remain unverified. No raw artifact content crosses the browser boundary. These checks prove declared size/integrity, not semantic quality. Use `sha256` only when the expected bytes are known before dispatch; for a newly generated report, use size bounds and required review instead of treating a worker-provided digest as acceptance.

Once a backend is observed successful, confirmed artifact failures get a **durable 30-second delivery grace**. The interval starts on an authorized applied reconciliation and survives process restarts; read-only previews never start a durable timer. Late valid artifacts complete normally. If the interval expires with artifacts still failing and all other required checks passing, reconciliation records a failed delegation with `outcome="validation_failed"`. Autopilot can classify this as semantic failure and use its existing bounded replan allowance. Zero/exhausted allowance fails for human attention.

Unreadable artifacts, missing backend observations, unresolved cancellation, denied authority, and unsatisfied required review never trigger this recovery. Validation verdict/check enums and fixed artifact failure codes are visible through delegation reads and Flight Deck; validation details, paths, bodies, and prompts are excluded from these receipts. Flight Deck remains read-only.

## Deliverable isolation and plan replacement

Each node's `expected_artifacts` basenames are carried into its immutable Work Contract as required, nonempty files. Each attempt has a separate workspace under the Hermes data root at `missions/artifacts/<task_id>/`; earlier attempts and other nodes cannot satisfy its local artifact check. Remote artifacts still require the existing coordinator-verified admission and contract binding. A completed execution with missing or empty artifacts remains unverified and cannot advance the node. File presence and size are acceptance checks; they do not prove the semantic quality of a deliverable.

Replacing a plan is refused with `PLAN_IN_FLIGHT` while a scheduler pass, unfinished nodes/delegations, or a nonterminal Autopilot run exists. The replacement uses the same per-Mission scheduler lease, including the dispatch-before-node-write window. Stop Autopilot and resolve unfinished work before replacing a plan. Dry runs report the unfinished-work refusal without writing.

Independent Mission reconciliation preserves the current Mission status while all failed attachments belong to classifier-approved, bounded Autopilot recovery. It reports their references in `recovery_pending` and retains the failed observations. This covers the interval before scheduler observation and retry backoff; it never claims success. Stopped/dead workers, expired runtime, exhausted attempts, unknown failures, terminal failed nodes, and missing or inconsistent lineage do not defer failure.
