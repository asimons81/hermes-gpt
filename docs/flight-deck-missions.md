# Flight Deck Missions (v0.9)

Flight Deck exposes first-class Missions as a read-only operational view. The browser does not gain Mission mutation, dispatch, cancellation, reconciliation, or approval authority.

## Routes

- `GET /api/ops/missions` — bounded Mission list with current durable state.
- `GET /api/ops/missions/{mission_id}` — durable Mission detail plus linked delegation summaries.
- `GET /api/ops/missions/{mission_id}/events` — bounded cursor/long-poll wake-up events filtered to one Mission.
- `GET /api/ops/missions/{mission_id}/autopilot` — the Mission's Autopilot run (v0.13), read-only; see below.
- `GET /api/ops/delegations/{delegation_id}` — one normalized delegation read model.

All browser payloads pass through the existing Flight Deck redaction boundary. Mission and Delegation stores remain authoritative; live-event payloads are wake-up notices only. The detail screen responds to a wake-up by re-reading durable Mission state rather than treating the event payload as completion evidence.

## Visible Mission state

The Mission list/detail screens expose bounded title/objective metadata, owner profile, status/version, acceptance criteria, context references and digests, explicit skills manifests, approval presence/requirement, attachments, linked delegation state, and recent Mission events.

The detail endpoint captures its live-event cursor before reading the Mission snapshot. This prevents a state transition racing with the snapshot from being skipped: the change is either already reflected in the durable snapshot or remains after the returned cursor and wakes the browser for another durable read.

## Authority boundary

The Mission UI contains no direct mutation controls. State transitions, attachment writes, reconciliation, delegation dispatch/cancel, and Owner approval continue through their existing operator surfaces and policy gates. Flight Deck is presentation and observation only.

## Autopilot view (v0.13)

`GET /api/ops/missions/{mission_id}/autopilot` shows the durable Autopilot run for a Mission: its state, limits, replan count, last scheduling pass (dispatched/held/failed nodes, the approval frontier, any budget or runtime limit that stopped new work), wake-up counters, and the recovery bookkeeping counts. It exists whether or not `HERMES_GPT_AUTOPILOT` is set, because it only reads a run that already exists; with no run it returns `found: false`. Starting, stopping, and every decision Autopilot makes remain outside the browser.

It follows the same rules as the rest of this screen:

- **GET only, no writes.** Unlike the `hermes_autopilot_status` MCP tool, which heals the stored run record and reconciles the job, this route writes nothing. It stays truthful anyway: it checks worker liveness without writing and reports an `effective_state` with `stale: true` when the stored record says `running` but the worker is dead or already finished. The stored record is shown as found.
- **Allow-listed fields.** The browser receives an explicit projection of the run, not the stored record, so a field added later is never exposed by accident. No process ids, config hashes, placements, or raw recovery internals.
- **Cursor first.** `live_cursor` is captured before the durable read, as on the Mission detail route.
- **Redacted and read-level.** The payload passes through the Flight Deck redaction boundary and requires the Operator read level.

The view is a snapshot to re-read on a wake-up, never proof: Mission, plan, delegation, and evidence stores remain authoritative.

