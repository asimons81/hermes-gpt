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
- an optional final Owner approval requirement.

The implementation lives in `operator_mission_runtime.py`. Existing `hermes_mission_*` Mission Control overview tools remain available and backward compatible; the new lifecycle tools are additional surfaces.

## Lifecycle

Mission states are durable and restart-safe. The normal progression is:

`draft -> running -> awaiting_approval -> completed`

A Mission can also be paused or blocked when attached execution state requires it. Reconciliation observes attached Swarm workflow state; it does not trust worker self-report as proof of completion.

If `final_approval_required` is true, completion is Owner-gated. A Mission cannot bypass the existing Operator/Owner authority model.

## Tool surface

The v0.9 Mission lifecycle tools are:

- `hermes_mission_create`
- `hermes_mission_get`
- `hermes_mission_list`
- `hermes_mission_update`
- `hermes_mission_attach`
- `hermes_mission_start`
- `hermes_mission_pause`
- `hermes_mission_reconcile`
- `hermes_mission_approve`

Read operations are read-only. Mutating operations preserve Hermes GPT's normal workspace/direct/confirm gates, and final approval uses Owner authority when required.

## Context and skills

Mission context is represented as references rather than copied source bodies. References are bounded and may include a digest for immutable identification. Mission skill entries are explicit manifests containing a skill name and optional version/ref/digest metadata.

This prevents Missions from becoming an unbounded prompt store and keeps sensitive raw content in the source system that owns it.

## Persistence and audit

Mission state is stored durably under the Hermes data root in a SQLite database. Mission lifecycle changes produce durable Mission events and Operator audit records. Reads do not create Mission state when no Mission database exists.

## Relationship to Swarm and Fabric

Missions do not replace Work Contracts, Swarms, runners, or Fabric. They provide the durable parent lifecycle above those existing execution and evidence layers:

`Mission -> Swarm/work -> Work Contract -> runner/Fabric -> observed evidence -> Mission reconciliation`

Completion remains based on coordinator-observed evidence and explicit approval boundaries, preserving the v0.8 Fabric safety model.
