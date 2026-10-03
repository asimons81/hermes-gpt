# Hermes GPT v0.13.0 — Autopilot

> Publication is tracked independently through the [GitHub release](https://github.com/asimons81/hermes-gpt/releases/tag/v0.13.0) and [PyPI version record](https://pypi.org/project/hermes-gpt/0.13.0/). Verify each channel separately; a GitHub release does not prove PyPI has the same version.

Hermes GPT v0.13.0 adds **Autopilot**: a durable runtime that drives one Mission through its MissionPlan (place nodes on capable peers, run independent nodes in parallel, observe and validate results, retry or replan within hard bounds, and stop at every approval boundary) without a human re-triggering every node. It is **default off**, and it is a caller of the existing Mission, plan, placement, Work Contract, delegation, budget, and live-event surfaces rather than a new authority.

Operational guide: [`docs/autopilot.md`](autopilot.md). Design and the findings behind it: [`docs/design/v0.13-autopilot.md`](design/v0.13-autopilot.md) (historical intent; the code and tests win where they differ).

## Authority and defaults

- Three tools, `hermes_autopilot_start` / `_status` / `_stop`, are registered only when `HERMES_GPT_AUTOPILOT=1`. With it unset the connector surface is unchanged at **137 tools**; with it set there are three more. A test pins both counts.
- Start is dry-run first and needs `workspace` level, direct apply mode, `dry_run=false`, and `confirm=true`. Stop is never gated behind the machine gate.
- Autopilot never dispatches or advances an approval node or a `high_impact` node, never approves a Mission, and never completes one. It stops at `awaiting_approval`; only `hermes_mission_approve` in Owner mode completes it. The worker process holds no Owner-mode authority (the acceptance test reads its environment to prove it).

## What is new

- **Durable runtime.** A detached worker (via the durable job supervisor) owns one Mission and survives an MCP server restart. Status re-verifies the worker on every read.
- **Parallel scheduling** up to `max_concurrency`, through the existing placement, contract, and delegation chain, with idempotent dispatch, crash-safe adoption, and compare-and-swap plan writes.
- **Evidence-based advancement.** A node completes only when its Work Contract validates `SATISFIED` against observed state; a worker's own success claim is never accepted.
- **Approval frontier** with a `waiting_for_owner` state.
- **Bounded recovery** decided by the existing failure classifier: transient failures retried on an alternate peer with backoff, semantic failures replanned through a rework clone, everything else failed for a human.
- **Mission limits** (`max_concurrency`, `max_replans`, `max_attempts_per_node`, `max_runtime_seconds`) and a **budget gate** that holds new dispatch whenever the envelope is not verifiably within its limit, whether or not the hard-block gates are armed.
- **Event-driven wakeups** with a periodic backstop; events are never proof.
- **`hermes_autopilot_status` summary** (additive) and a read-only **Flight Deck** view.

## Changes outside Autopilot

- **Mission model (behavior change to review).** A failed or cancelled delegation attempt can be marked superseded by its successor (`superseded_by:<id>` in the attachment `relationship`). Without this, one failed attempt failed the whole Mission on reconcile and no retry could rescue it. The marker is written only by an internal bridge, refused by public `hermes_mission_attach`, honored only while the attempt is authoritatively failed or cancelled, and ignored when forged, dangling, or cyclic. No approval rule changes. See `docs/missions.md`.
- `hermes_plan_node_transition` gains optional `expected_plan_version` (compare-and-swap) and `bump_retries`; defaults preserve existing behavior.
- An internal, narrow `apply_rework_patch` in the plan module (not a tool).
- `operator_controller._l2_dispatch` gains an optional `delegation_id` (default unchanged).
- Packaging: `operator_autopilot` and its operational guide are shipped; release wheels also include the compiled Flight Deck.
- Gateway health uses a read-only native Windows process query when psutil is unavailable; the POSIX signal-zero probe is never used on Windows. Worker observation distinguishes a missing PID from denied inspection and preserves uncertainty; cancellation still requires verified process identity.
- An unsupported confinement backend fails its capability probe and refuses launch before starting a worker.
- Pi RPC reads subprocess output through a portable bounded queue. Windows never selects on a pipe, and a partial JSONL line cannot block the worker timeout.
- Windows job storage encodes colon-bearing and reserved job IDs without changing existing POSIX paths. Mission, delegation, and live-event database operations close their handles immediately, preventing deferred checkpoints and locked-file cleanup failures.

## Upgrade notes

- Nothing changes unless `HERMES_GPT_AUTOPILOT=1` is set. There is no schema migration. Autopilot stores its own run state under the Hermes data root in `autopilot/`.
- New environment variables: `HERMES_GPT_AUTOPILOT` (machine gate) and `HERMES_GPT_AUTOPILOT_IDLE_SECONDS` (optional backstop poll, default 2.0, clamped 0.5-60).

## Release verification

- The macOS full suite passes with 1,643 passed and 9 platform-specific skips (1,652 collected test IDs). Chat transport tests explicitly load the packaged SessionDB shim and stub Agent configuration so collection cannot bootstrap an installed Agent runtime.
- CI runs full suites on Windows and Linux with Python 3.10–3.12 and both supported MCP SDK families, plus pinned SDK minimums on Linux. The Agent-loader integration and frontend tests remain separate checks.
- The end-to-end acceptance suite runs on Linux with a real detached worker, bounded synthetic peers, a killed peer, recovery, an approval stop/resume, and an MCP server restarted mid-Mission. The worker environment proves it has no Owner authority. This is process-level acceptance with test peers, not a claim of workload acceptance on a production fleet.
- The new deliverable tests reject missing/empty artifacts and stale attempt output; they verify recovery deferral before scheduler observation and during retry backoff, fail-closed limits/lineage, and active-plan replacement refusal.
- All 30 frontend tests pass. A browser check confirms the compiled Flight Deck loads under `/ui/ops/missions`, displays progress/budget/recovery/approval holds, and keeps navigation within `/ui`.
- Wheel and sdist builds pass `twine check` and package hygiene. A clean wheel installation resolves the bundled Flight Deck index and assets.

## Known limitations

- **Browser activation is opt-in.** Release packages now include the Flight Deck build; serving it still requires `HERMES_GPT_UI_ENABLED=1` and the existing authentication boundary.
- **Linux process acceptance.** The environment-authority acceptance test reads `/proc` and runs on Linux. The full Windows suite covers scheduling, durable worker lifecycle, liveness, cancellation, recovery, and UI routes; it skips the Linux `/proc` acceptance cases.
- **Unclassifiable failures are not retried.** A bare "worker crashed" fails the node for a human, because the classifier refuses to guess a class. A run that completes but fails its Work Contract stays `reconciling` in the delegation layer, so it is observed and reported but not treated as a semantic failure.
- **Independent recovery reconciliation is bounded.** Recoverable attempts retain failed observations and report `recovery_pending`; stopped/dead workers, expired runtime, exhausted limits, and unknown failures still fail closed.
- **A budget-paused Mission** stays paused until the owner resumes it. Autopilot reports the reason but has no separate state for it.
- **Plan replacement requires drained work.** `PLAN_IN_FLIGHT` refuses replacement during scheduler passes or unfinished work. Autopilot does not require plan status `approved`; starting with `confirm=true` is the owner's authorization.
- Node objectives are stored only as hashes, so contracts carry a pointer objective. Declared artifact basenames are now required and nonempty, but semantic quality still needs review or a stronger existing Work Contract.

## Release process (not yet performed)

Follow `RELEASE_CHECKLIST.md`. In particular: run `hermes_release_doctor(full_tests=true)`, run the Windows/Linux Python 3.10-3.12 CI matrix, build and hygiene-check the artifacts, tag only after approval, and verify GitHub and PyPI separately.
