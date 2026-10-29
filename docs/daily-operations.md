# Daily Operations

Hermes GPT 0.17.0 adds a guided Mission launch, durable launch recovery, pending-review workbench, and delivery status to Flight Deck. Enable the existing UI and open **Missions → New Mission**. No new authority is granted by the browser.

## Launch a bounded Mission

The form requires a title, objective, acceptance criteria and owner profile. Choose a positive budget quota or explicitly choose no budget envelope. A quota uses the configured unit; it is not automatically a dollar limit. Omitting an envelope permits dispatch without that budget gate.

The initial UI uses one concurrent worker, one attempt per node, no replans and a 30-minute runtime limit. Preview validates the complete chain; edits invalidate the preview. Confirm performs Mission creation, plan creation, optional budget creation and Autopilot start through `hermes_mission_launch`. The server requires Operator workspace authority, direct apply mode, `HERMES_GPT_AUTOPILOT=1`, and confirmation. Existing runner gates still apply. Final Owner approval cannot be disabled by this flow.

The browser requires the `delegations` Mission Control surface. As elsewhere, an unset `HERMES_GPT_MISSION_ALLOWED_SURFACES` permits all read surfaces; an empty value permits none. The allowlist does not replace Operator mutation gates.

## Receipts and uncertain outcomes

`hermes_mission_launch` now accepts an optional `request_id`: 16–64 ASCII letters, digits, hyphens or underscores. Existing callers that omit it retain legacy behavior and do not gain duplicate prevention. New callers should generate a random opaque id, retain it before submission, and use it for the same attempt only.

A keyed direct launch commits its intent to `missions/launch-receipts.db` before any Mission/plan/dispatch effect. The key and normalized input are hashed; receipts retain Mission identity, step outcomes, created identifiers and timestamps, never the original objective or launch input. Dry runs and gate refusals do not create receipts. Duplicate calls with the same key/input inspect the original attempt; a different input is refused with `LAUNCH_REQUEST_CONFLICT`.

The browser saves only the opaque id in session storage before confirming. A lost response or reload offers **Check receipt**, using a GET request. It does not submit again. MCP callers can reuse the same key/input to inspect the receipt; this never resumes a partial chain. `completed` means the launch chain completed, not that the Mission work succeeded or received final approval.

`in_progress` can mean active work or a crash after admission. Check the original Mission, plan, delegation and Autopilot state before any administrative recovery. There is no automatic redispatch, expiry or unlock. The browser intentionally offers no generic retry for partial or unknown work. Retain the original tab/request id; clearing browser storage loses its recovery pointer, not the server reservation.

The store permits at most 10,000 receipts and fails closed when full. See [retention and rollback](retention-policy.md#launch-receipts-v017). Do not delete a receipt to retry uncertain work.

## Review pending work

Open **Owner inbox → Review evidence** on a pending Work Contract review. The page displays the exact contract SHA-256, task/delegation references, recorded validation results and declared artifact requirements. These are bounded metadata, not a fresh artifact read, raw logs, transcripts or automatic evidence approval. Independently inspect the actual deliverables before accepting them.

Enter a reviewer distinct from every executing identity, a verdict, and bounded evidence references or an approval reference. Preview and confirm use the existing `hermes_review_accept` writer, requiring Owner Mode and its acknowledgement. The server reloads the pending item and rejects a stale revision, mismatched contract/task/assignee, or executing reviewer. Refresh evidence before trying again after a stale-state refusal.

After recording acceptance, explicitly preview and confirm **Reconcile delegation**. This reuses the stored original contract. Review acceptance, reconciliation, inbox acknowledgement, plan approval and final Mission approval are separate actions. Jev advice is optional and is not acceptance evidence. The review read surface requires `approvals`; reconciliation retains workspace/direct gates.

## Delivery status

**Delivery status** is a read-only `health` surface. It shows the package version, relay configuration state, masked targets, pending count, failure/backoff state and `SENT_UNVERIFIED`. No send, configure or resend action is provided. An unverified send is suppressed from resend to avoid duplicates; it is not proof of recipient delivery.

Agent provenance/capability inspection is deferred. This page explicitly says compatibility is not probed. Session control's finite-protocol preflight is separate and does not prove live Desktop acceptance. See [session control](session-control.md) and [attention delivery](attention-delivery.md).

## Upgrade and rollback

Refresh client action/schema caches after upgrading: `hermes_mission_launch` has an additive argument, while the neutral main-server tool count stays 139 (140 with Finance enabled). Package publication does not upgrade a running instance or refresh a connector.

Before changing an installed instance, capture its exact version/source revision and back up the receipt store while work is idle. Older releases ignore this additive store, but their launch path lacks keyed deduplication: hold launches during rollback, preserve receipts, and inspect all uncertain attempts. Use the existing installation method to restore the pinned old package/source; the check-first updater intentionally does not provide arbitrary downgrade. Never copy live credentials into a release artifact.
