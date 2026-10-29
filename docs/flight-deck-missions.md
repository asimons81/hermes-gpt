# Flight Deck Missions (v0.9)

For the v0.17 guided launch, durable receipt recovery, pending-review workbench and read-only delivery page, see [Daily Operations](daily-operations.md). Existing Operator, direct, Autopilot and Owner gates remain authoritative.


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

The Mission screens (list, detail, Autopilot panel) contain no mutation controls. State transitions, attachment writes, reconciliation, delegation dispatch/cancel, and Owner approval continue through their existing operator surfaces and policy gates. The one place the Flight Deck acts on Missions is the Owner inbox, described below: its actions call the same gated tools through `POST /api/ops/action`, and they add no authority.

## Owner inbox actions (v0.16)

Status: shipped in v0.16.0 (the Owner inbox Flight Deck actions and their approval-frontier gates).

The Owner inbox panel (`GET /api/ops/owner-inbox`) offers per-item actions for Missions that wait on the Owner. Each action is a dry-run request followed by a separate confirm request. The server enforces every gate, including Owner Mode, and refuses with the tool's own message.

- `mission_awaiting_approval`: **Approve Mission…** calls `hermes_mission_approve`. The Owner enters an approval reference. Owner Mode must be active on the server. With it off, the browser shows the refusal and nothing is approved.
- `autopilot_waiting_owner`: **Approve node `<id>`…** for each frontier node calls `hermes_plan_approve_gate` (v0.16.1), which walks the legal state path to `completed`; its dry run lists the steps. **Reject node `<id>`…** calls `hermes_plan_node_transition` with target `failed`. Both are restricted to an approval frontier: an approval or high-impact node, with Owner Mode required. **Stop Autopilot…** calls `hermes_autopilot_stop`, which is not behind `HERMES_GPT_AUTOPILOT`.
- Every item: **Acknowledge** calls `hermes_owner_inbox_ack`. It hides the item and changes nothing else.

The full table, the exact checks, and the refusal codes are in [owner-inbox.md](owner-inbox.md#flight-deck-actions). The allowlist and request protections are in [ui-security-boundary.md](ui-security-boundary.md#6-mutations-remain-auditable-and-gated).

Mission detail and Autopilot screens still have no action controls. Starting Autopilot, dispatch, reconciliation and every other Mission mutation stay in MCP.

## Autopilot view (v0.13)

`GET /api/ops/missions/{mission_id}/autopilot` shows the durable Autopilot run for a Mission: its state, limits, replan count, last scheduling pass (dispatched/held/failed nodes, the approval frontier, any budget or runtime limit that stopped new work), wake-up counters, and the recovery bookkeeping counts. It exists whether or not `HERMES_GPT_AUTOPILOT` is set, because it only reads a run that already exists; with no run it returns `found: false`. Starting, stopping, and every decision Autopilot makes remain outside the browser.

It follows the same rules as the rest of this screen:

- **GET only, no writes.** Unlike the `hermes_autopilot_status` MCP tool, which heals the stored run record and reconciles the job, this route writes nothing. It stays truthful anyway: it checks worker liveness without writing and reports an `effective_state` with `stale: true` when the stored record says `running` but the worker is dead or already finished. The stored record is shown as found.
- **Allow-listed fields.** The browser receives an explicit projection of the run, not the stored record, so a field added later is never exposed by accident. No process ids, config hashes, placements, or raw recovery internals.
- **Derived summary.** The response includes a `summary` (progress, approval frontier, budget, recovery counters, limits, wake-up counters, and an `attention` list with `needs_owner`) built by the same code as the `hermes_autopilot_status` tool. It is an allow-listed projection: worker peers, delegation ids, and unlisted fields are never sent, and if it cannot be built the route returns `{"available": false}` while still reporting the run. Reading it never enforces a budget or pauses a Mission.
- **Cursor first.** `live_cursor` is captured before the durable read, as on the Mission detail route.
- **Redacted and read-level.** The payload passes through the Flight Deck redaction boundary and requires the Operator read level.

The view is a snapshot to re-read on a wake-up, never proof: Mission, plan, delegation, and evidence stores remain authoritative.


The Mission detail screen includes an Autopilot panel for task progress, worker state, budget spend and holds, retry/replan counts, runtime remaining, and owner attention. It refreshes after Mission revisions and every five seconds without overlapping polling requests. Failed refreshes retain the last snapshot with a visible verification warning. Unverified or dead workers are never presented as healthy.

Release wheels bundle the built Flight Deck. Set `HERMES_GPT_UI_ENABLED=1` to serve it under `/ui/`; the existing browser authentication and Operator gates still apply. Source checkouts use `web/dist` after `npm ci && npm run build`. `HERMES_GPT_UI_DIR` continues to override either location.
