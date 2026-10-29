# Hermes GPT v0.17.0 — Daily Operations

The feature release connects guided Mission launch, recoverable launch receipts and Owner review in Flight Deck. See [Daily Operations](daily-operations.md) for the exact gates, recovery and rollback procedure.

- Preview a bounded Mission, explicitly choose its budget posture, then confirm. Changed input invalidates the preview.
- Recover the original launch receipt after a disconnect. A stable optional MCP `request_id` prevents redispatch of the same attempt, including across processes and restarts. Partial work remains reserved for reconciliation.
- Open a pending review from the Owner inbox with exact contract/task/delegation references. Record a distinct reviewer's evidence through the existing Owner-gated writer; stale evidence bindings are refused. Reconciliation and final Mission approval remain separate.
- Inspect masked notification targets, pending/failure/backoff state and unverified delivery without sending or resending anything.

Includes the v0.16.2 finite structured session protocol, durable session reservations, Autopilot terminal-outcome ordering fix and publication of exact CI-built distributions.

## Compatibility and limits

Default tools remain 139, or 140 with Finance enabled. `hermes_mission_launch` has an additive optional argument; refresh client schemas. Existing callers without a request id retain their old launch behavior. Receipt storage is additive, capped at `10_000` receipts and retained without automatic expiry. An uncertain attempt is never automatically retried.

The delivery page does not probe Agent provenance. Live Windows/Desktop session acceptance remains unverified; issue #103 is not closed on offline results. Tests use isolated homes and synthetic workers/evidence, with both MCP SDK families and the supported CI OS/Python matrix. A released package is not acceptance of an installed service, live Mission, notification delivery, or connector.

GitHub and PyPI are independent channels. Verify the version and artifact hashes on each before installation. No fleet rollout is included in this release.
