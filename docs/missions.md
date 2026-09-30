# Missions (v0.9)

Hermes GPT v0.9 adds a first-class durable Mission object for grouping work, context, skills, workflows, delegated execution, evidence, and final approval under one bounded lifecycle.

## What a Mission is

A Mission is the durable parent record for a larger objective. It stores bounded metadata and references only; it is not a transcript or raw-source archive.

A Mission can contain:

- a title and objective;
- acceptance criteria;
- bounded context references with optional SHA-256 digests;
- an explicit skills manifest;
- attachments to Swarm workflows and delegation/run identifiers;
- lifecycle state and audit history;
- a final Owner approval requirement that defaults on; disabling it is an Owner-only creation-time decision and cannot be weakened later.

The implementation lives in `operator_mission_runtime.py`. Existing `hermes_mission_*` Mission Control overview tools remain available and backward compatible; the new lifecycle tools are additional surfaces.

## Lifecycle

Mission states are durable and restart-safe. The normal progression is:

`draft -> running -> awaiting_approval -> completed`

A Mission can also be paused or blocked when attached execution state requires it. Reconciliation observes attached Swarm workflow state and only accepts non-workflow success from lifecycle adapters that mark the attachment as verified with an evidence reference. Public attachment calls cannot assert `succeeded`.

`final_approval_required` defaults to true and is immutable after creation. Creating a Mission with that boundary disabled requires Owner authority. Approval-required Missions enter `awaiting_approval` through reconciliation and can complete only through `hermes_mission_approve`. A Mission cannot bypass the existing Operator/Owner authority model.

## Tool surface

The v0.9 Mission lifecycle tools are:

- `hermes_mission_create`
- `hermes_mission_get`
- `hermes_mission_list`
- `hermes_mission_update`
- `hermes_mission_attach`
- `hermes_mission_transition`
- `hermes_mission_reconcile`
- `hermes_mission_approve`

Read operations are read-only. Mutating operations preserve Hermes GPT's normal workspace/direct/confirm gates. Direct completion is Owner-gated, and approval-required Missions complete only through explicit Owner approval. Workflow references use the canonical `sw-*` identifier grammar and are confined to the durable workflow directory.

## Context and skills

Mission context is represented as references rather than copied source bodies. References are bounded and may include a digest for immutable identification. Mission skill entries are explicit manifests containing a skill name and optional version/ref/digest metadata.

This prevents Missions from becoming an unbounded prompt store and keeps sensitive raw content in the source system that owns it.

## Persistence and audit

Mission state is stored durably under the Hermes data root in a SQLite database. Mission lifecycle changes produce durable Mission events and Operator audit records. Reads do not create Mission state when no Mission database exists.

## Relationship to Swarm and Fabric

Missions do not replace Work Contracts, Swarms, runners, or Fabric. They provide the durable parent lifecycle above those existing execution and evidence layers:

`Mission -> Swarm/work -> Work Contract -> runner/Fabric -> observed evidence -> Mission reconciliation`

Completion remains based on coordinator-observed evidence and explicit approval boundaries, preserving the v0.8 Fabric safety model. A terminal backend self-report is not sufficient: Mission-facing success must come from a verified lifecycle adapter, and unverified legacy success is reconciled fail-closed to `blocked`.

## Delegation dispatch and reconciliation

Direct delegation dispatch reserves durable lineage before invoking a backend. The delegation store records the phases `reserved`, `invoking`, and `dispatched`; the `reserved -> invoking` claim is atomic. An exact retry with the same delegation id, task id, Mission, backend, contract digest, and validation context is idempotent. Any collision with different lineage is rejected. A definite backend rejection with no possible side effect returns the row to `reserved`; an ambiguous invocation remains `invoking` and is never automatically redispatched.

For Mission-bound work, Hermes commits a private `pending` delegation attachment before backend invocation and rechecks the Mission and attachment immediately before the call. This reservation is idempotent and cannot downgrade an existing attachment. Public `hermes_mission_attach` calls cannot assert either `succeeded` or `cancelled` for any attachment kind. A Mission cannot be cancelled while a delegation child is reserved, invoking, pending, running, or ambiguous.

Delegation reconciliation does not require callers to resupply the Work Contract. Dispatch stores a private, prompt-free validation manifest containing the manifest schema, immutable contract and validation-context digests, validation-only fields, and timestamps. It never stores objectives, prompts, transcripts, raw inputs, constraints, secrets, or backend request/response bodies; secret-like durable values are rejected before dispatch. Reconciliation uses the same observed-state validation algorithm as `hermes_contract_validate`, with or without a matching `contract_json` supplied for parity checking.

Missing, corrupt, mismatched, or secret-like manifests fail closed. Missing current backend observation also fails closed, even if an earlier cached lifecycle or validation value claimed success. Mission reconciliation and Owner approval freshly re-observe delegation backend state, artifacts, review evidence, authorization, and the manifest on every decision. Verified Mission evidence is exactly `contract:<contract_sha256>`; cached attachment state and cached delegation verdicts are not completion authority.

## Superseded delegation attempts (v0.13)

By default one failed delegation child fails the whole Mission on reconciliation, so a retry could never rescue it. A bounded Autopilot retry or replan therefore marks the failed attempt as replaced: its attachment `relationship` becomes `superseded_by:<successor delegation id>`. Reconciliation, Owner approval, and the completion/cancellation guards then skip that attempt and judge the live end of the chain instead.

The marker is deliberately hard to misuse:

- it is written only by the internal `supersede_delegation_attachment` bridge, never by a tool; public `hermes_mission_attach` refuses any `superseded_by:` relationship;
- the old attempt must be authoritatively `failed` or `cancelled` in the delegation store when marked, and again every time it is observed; a live, running, ambiguous, or succeeded attempt is never skipped;
- the successor must be a different delegation attachment of the same Mission, the chain must not loop, and it must end at an attachment that is not itself superseded;
- a marker that fails any of these checks (forged, stale, dangling, self-referencing, cyclic) has no effect and the attempt is observed normally, so the Mission fails closed;
- the marker changes no approval rule: `final_approval_required` still stops at `awaiting_approval`, and Owner approval still needs at least one freshly verified success.

Until the successor delegation exists, the failed attempt still fails the Mission if it is reconciled; the marker can only be applied once the successor has been dispatched.
