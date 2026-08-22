# Hermes GPT v0.8.0 - Fabric

G7 Owner ship authorization: 2026-08-21, recorded on [issue #27](https://github.com/asimons81/hermes-gpt/issues/27).

Hermes GPT v0.8.0 turns the v0.7 control plane into a local-first distributed execution fabric. A bounded Swarm stage can execute on another authenticated Hermes machine while the coordinator preserves Work Contract semantics, server-controlled authority, observed-state evidence, review independence, and final human approval.

Remote worker self-report remains transport data. It is never sufficient proof of completion.

## Cross-machine Fabric execution

- Added the packaged `hermes-gpt-fabric-peer` runtime for authenticated A2A/Fabric peers.
- Swarm execution can use `execution.backend=auto` to select a local or remote runtime from current node capabilities while preserving explicit operator overrides.
- Non-loopback Fabric peer serving requires direct TLS with both `--cert` and `--key`; insecure remote transport fails closed.
- Remote placement preserves the selected profile, logical workspace, execution backend, and authority ceiling rather than trusting the peer to widen them.

## Capability-aware routing

- Added durable node capability manifests, health and freshness tracking, deterministic ranking, and exclusion reasons.
- Automatic routing respects capability freshness, backend availability, remote/local placement, profile policy, workspace constraints, and authority ceilings.
- Final routing receipts retain the authoritative selected-node fields used by Flight Deck, including health, capability freshness, eligibility, transport backend, and authority ceiling.

## Remote evidence and artifacts

- Added remote evidence collection and admission paths that feed the existing Work Contract validator.
- Missing or unavailable required Fabric evidence fails closed and cannot become `SATISFIED`.
- Added immutable remote artifact transfer/admission with manifest and content hashing. Coordinator-side admission verifies the received bytes rather than accepting a worker claim.
- Verified remote artifacts can satisfy contract artifact requirements without bypassing the existing observed-state validation boundary.

## Reconciliation and single-writer safety

- Added restart, timeout, and cancellation reconciliation for distributed attempts.
- Recoverable transport ambiguity reconciles the original attempt instead of silently creating a replacement attempt.
- Added write-ownership and write-epoch protections for remote mutation-capable execution paths.
- Duplicate-writer and ambiguous-attempt conditions fail closed.

## Flight Deck Fabric visibility

- Added read-only Fabric views for nodes, placement, attempts, evidence, and routing history.
- Flight Deck surfaces the selected node/backend/transport and the route fields that made the placement eligible.
- v0.8 includes the final route-truthfulness repair so a selected healthy/fresh/eligible route is no longer rendered as unhealthy merely because older receipt fields were absent.

## G6 two-machine acceptance

The final Fabric implementation target `4953c5f23db8d356365af8e18148e63d3c80125c` passed fresh real two-machine acceptance. The authoritative G6 evidence packet and independent-review result are recorded on [issue #37](https://github.com/asimons81/hermes-gpt/issues/37). The acceptance used distinct Linux machines, an authenticated remote Fabric peer, `execution.backend=auto`, real remote Pi RPC execution, an induced live transport interruption, same-attempt reconciliation, remote artifact admission with an independent coordinator re-hash, fail-closed missing-evidence behavior, Work Contract validation, the approval gate, and an independent final review.

Independent review verdict: `PASS`; G6 closure blocked: `NO`. G7 Owner ship authorization is recorded on [issue #27](https://github.com/asimons81/hermes-gpt/issues/27).

## Additional changes since v0.7.0

- Added `hermes_export_file`, a bounded workspace-authorized binary export surface with denied-path enforcement, symlink escape refusal, size caps, optional extension allowlisting, and safe audit metadata.
- Added the OpenAI Secure MCP Tunnel deployment path for private ChatGPT/Codex/OpenAI access while Hermes GPT stays loopback-bound.
- Pinned the MCP SDK to the FastMCP-compatible 1.x line because MCP 2.x removes `mcp.server.fastmcp`.
- Hardened fleet dispatch timeout recovery so submitted work remains pollable when the initial request times out.
- Removed wildcard proxy trust from the curated Codex MCP HTTP runner.
- Made disabled OIDC discovery return a public 404 instead of an auth challenge.
- Hardened Operator doctor gateway health so a heartbeat without a live gateway PID fails closed.
- Hardened Hermes Agent source-root detection against stray namespace `tools/` directories.

## Known presentation limitation

A reconciled Fabric attempt that ultimately reaches `COMPLETED` can still retain the historical `FABRIC_TRANSPORT_TIMEOUT` in the Flight Deck blocker/error presentation. The timeout is real historical evidence, but presenting it as a current blocker after successful reconciliation is misleading. Independent G6 review classified this as non-blocking because terminal peer evidence, artifact admission, coordinator validation, and workflow state independently establish successful completion.

## Safety and compatibility

- Hermes GPT remains local-first and intended for trusted-machine/private-boundary deployment.
- Server-controlled authority remains authoritative across remote execution.
- Work Contract validation remains observed-state based and fail-closed.
- Final workflow approval remains explicitly human/Owner gated.
- A GitHub release and a PyPI release are separate distribution channels; verify the published version on each channel.
