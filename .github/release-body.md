# Hermes GPT v0.8.0 - Fabric

Hermes GPT v0.8.0 turns the v0.7 control plane into a local-first distributed execution fabric. A bounded Swarm stage can now execute on another authenticated Hermes machine while the coordinator keeps authority, evidence admission, Work Contract validation, independent review, and final human approval on the trusted side of the boundary.

Remote worker self-report is still transport data, never proof of completion.

## Cross-machine execution

- Packaged `hermes-gpt-fabric-peer` A2A/Fabric runtime.
- `execution.backend=auto` can choose eligible local or remote runtimes from current node capabilities.
- Non-loopback Fabric peers require direct TLS with `--cert` and `--key`.
- Placement preserves profile, logical workspace, backend, and server-controlled authority ceilings.

## Evidence, artifacts, and recovery

- Remote evidence feeds the existing observed-state Work Contract validator.
- Required missing/unavailable Fabric evidence fails closed.
- Remote artifacts are admitted through immutable manifests and coordinator-side content hashing.
- Restart/timeout/cancel reconciliation preserves the original attempt when recovery is possible.
- Write-ownership/write-epoch guards prevent ambiguous or duplicate remote writers.

## Fabric Flight Deck

Flight Deck now exposes read-only Fabric nodes, placement, attempts, evidence, and routing history. The final v0.8 repair preserves the authoritative selected-route health, capability-freshness, eligibility, transport-backend, and authority-ceiling fields instead of fabricating negative state for older/missing receipt fields.

## Two-machine acceptance

The final Fabric implementation target `4953c5f23db8d356365af8e18148e63d3c80125c` passed fresh real two-machine G6 acceptance. The authoritative acceptance evidence and independent-review result are recorded on [issue #37](https://github.com/asimons81/hermes-gpt/issues/37). The acceptance exercised authenticated remote Fabric -> Pi RPC execution, `execution.backend=auto`, an induced live transport interruption, same-attempt reconciliation, artifact admission plus independent re-hash, fail-closed missing evidence, Work Contract `SATISFIED`, non-owner approval denial, explicit Owner approval, and an independent final review.

Independent review: **PASS**. G6 closure blocked: **NO**. G7 Owner ship authorization is recorded on [issue #27](https://github.com/asimons81/hermes-gpt/issues/27).

## Also included

- `hermes_export_file` bounded binary export.
- OpenAI Secure MCP Tunnel deployment path.
- FastMCP-compatible MCP 1.x dependency pin.
- Fleet timeout recovery with pollable task identity.
- Safer proxy trust and OIDC discovery behavior.
- Stricter Operator doctor gateway health and Hermes source-root detection.

## Known non-blocking presentation limitation

A reconciled attempt that ultimately reaches `COMPLETED` can still retain historical `FABRIC_TRANSPORT_TIMEOUT` data in the Flight Deck blocker/error presentation. Independent G6 review classified this as non-blocking because terminal peer evidence, artifact admission, coordinator validation, and workflow state independently establish completion.

Full details: [v0.8.0 release notes](docs/release-notes-v0.8.0.md).
