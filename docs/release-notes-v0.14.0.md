# Hermes GPT v0.14.0 — Acceptance and recovery

Release date: October 3, 2026.

v0.14.0 makes deliverable acceptance explicit and prevents successful executions with confirmed artifact failures from staying in reconciliation indefinitely. It builds on the default-off v0.13 Autopilot runtime and preserves all existing dispatch, budget, approval, and recovery limits.

## Declared acceptance

MissionPlan nodes retain their `expected_artifacts` basename list. Optional `artifact_requirements` can strengthen individual entries with positive `min_bytes`, optional `max_bytes`, and optional lowercase `sha256`. The declared requirements are included in the node signature and each dispatched Work Contract; rework copies them unchanged into an isolated successor workspace.

Work Contract artifacts support the same optional upper size bound and expected digest. The validator compares observed local files or existing contract-bound, coordinator-verified remote artifact metadata. A worker's digest claim is not proof. Local hashing is binary and bounded to 8 MiB; unreadable, changing, or larger hash-required local files remain unverified. No file bodies enter validation summaries, audit records, or Flight Deck.

These checks prove declared size/integrity requirements. They do not judge meaning or replace required human review.

## Recovery after successful execution

An observed successful backend with bad required artifacts receives a durable 30-second delivery grace. Late artifacts that satisfy the contract within that interval complete normally without duplicate dispatch. Once grace expires, bad artifacts become a delegation failure with outcome `validation_failed`, provided every other required check passes. Autopilot then uses its existing semantic classifier and bounded replan path. An exhausted or zero replan budget fails for human attention.

Missing run observations, unreadable evidence, unresolved cancellation, denied authorization, and unsatisfied required review do not enter this path and are never optimistically redispatched. Validation metadata survives process restarts. Read-only preview calls do not persist timers or validation receipts.

## Supervision and compatibility

Flight Deck shows completion check results and fixed artifact failure reasons, including missing files, size mismatches, hash mismatches, and unverified reads. Delivery grace and pending validation are visible. The browser remains read-only.

No MCP tool is added or removed: the connector tool count remains 137 with Autopilot disabled and 140 when enabled. Old contract/node hashes remain unchanged when optional requirements are absent. Two metadata columns are added to the delegation store on an authorized write; read-only calls do not migrate the store. Existing Owner gates, default-off machine gates, and final Mission approval remain intact.

## Verification and upgrade

Follow [the maintainer release checklist](../RELEASE_CHECKLIST.md): focused acceptance/recovery regressions, the full Python suite, frontend tests/build, full Windows/Linux Python 3.10–3.12 and MCP 1/2 CI, full-test release doctor, clean-wheel verification, and package hygiene. Verify GitHub and PyPI separately before claiming publication. Verify the installed service's actual MCP version and UI after upgrade.

Current guides: [Autopilot](autopilot.md), [delegations](delegations.md), and [Operator mode](operator-mode.md).
