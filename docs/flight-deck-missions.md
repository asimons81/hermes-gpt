# Flight Deck Missions (v0.9)

Flight Deck exposes first-class Missions as a read-only operational view. The browser does not gain Mission mutation, dispatch, cancellation, reconciliation, or approval authority.

## Routes

- `GET /api/ops/missions` — bounded Mission list with current durable state.
- `GET /api/ops/missions/{mission_id}` — durable Mission detail plus linked delegation summaries.
- `GET /api/ops/missions/{mission_id}/events` — bounded cursor/long-poll wake-up events filtered to one Mission.
- `GET /api/ops/delegations/{delegation_id}` — one normalized delegation read model.

All browser payloads pass through the existing Flight Deck redaction boundary. Mission and Delegation stores remain authoritative; live-event payloads are wake-up notices only. The detail screen responds to a wake-up by re-reading durable Mission state rather than treating the event payload as completion evidence.

## Visible Mission state

The Mission list/detail screens expose bounded title/objective metadata, owner profile, status/version, acceptance criteria, context references and digests, explicit skills manifests, approval presence/requirement, attachments, linked delegation state, and recent Mission events.

The detail endpoint captures its live-event cursor before reading the Mission snapshot. This prevents a state transition racing with the snapshot from being skipped: the change is either already reflected in the durable snapshot or remains after the returned cursor and wakes the browser for another durable read.

## Authority boundary

The Mission UI contains no direct mutation controls. State transitions, attachment writes, reconciliation, delegation dispatch/cancel, and Owner approval continue through their existing operator surfaces and policy gates. Flight Deck is presentation and observation only.
