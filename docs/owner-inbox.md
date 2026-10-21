# Owner inbox

Status: the read-only inbox **shipped in v0.15.0**. The Work Contract review source, stable item ids and acknowledgements are **v0.16 changes on branch `feat/v016-inbox-ack`**; they are not part of a release until the release notes say so. Verify the installed version and tool inventory before relying on any of them (see [runtime provenance](runtime-checkout.md)).

## Current behavior

The Owner inbox lists everything currently waiting on the Owner in one place. It is a **read-only projection** over existing read paths. `build_inbox` writes nothing, heals nothing, approves nothing, and advances nothing. It adds no authority: every suggested action is an existing tool, and that tool's own gates (Owner level, dry-run first, confirm, audit) still apply.

The inbox has one write path of its own: `hermes_owner_inbox_ack`, which appends an acknowledgement. An acknowledgement only **hides an item** from the inbox and from notifications. It never approves, transitions, or changes any Mission, plan, Work Contract, or delegation. See [Acknowledgements](#acknowledgements).

Code: `operator_owner_inbox.py`.

Python entry points (exact names):

- `build_inbox(hermes_root: Path | None = None, limit: int = 50, include_acknowledged: bool = False) -> dict`. `limit` must be an int from 1 to 200; anything else raises `ValueError`. `include_acknowledged` must be a bool; anything else raises `ValueError`.
- `hermes_owner_inbox(limit: int = 50, include_acknowledged: bool = False, hermes_root: Path | None = None) -> str`. JSON-string wrapper that returns `{"success": true, "tool": "hermes_owner_inbox", ...}`, or an error envelope for an invalid argument or a denied read level. It writes one audit record through `operator_policy.audit_record`.
- `hermes_owner_inbox_ack(item_ids: list[str], dry_run: bool = True, hermes_root: Path | None = None) -> str`. The acknowledgement writer. See [Acknowledgements](#acknowledgements).
- `acknowledged_ids(hermes_root: Path | None = None) -> set[str]`. Returns the acknowledged item ids. It raises `OSError` or `ValueError` when the store is unreadable.
- `ACK_STORE_RELATIVE = "missions/attention_acks.jsonl"`. Relative to the Hermes root.

Flight Deck route: `GET /api/ops/owner-inbox?limit=N` in `ui_ops.py`. It returns the same projection in the `{"ok": true, "data": ...}` envelope, redacted by `ui_security`. There is no POST. The route calls `build_inbox` with the defaults, so **acknowledged items are hidden** and the route has no `include_acknowledged` parameter. It respects the `approvals` entry of the Mission allowlist (`HERMES_GPT_MISSION_ALLOWED_SURFACES`: unset allows, a list allows only listed surfaces, empty denies). A bad `limit` returns `400 INVALID_LIMIT`.

### Result shape

```json
{
  "items": [
    {"item_id": "<24 hex chars>", "kind": "...", "mission_id": "msn-...", "summary": "...",
     "since": "<ISO-8601 or null>", "suggested_tool": "<tool name or null>",
     "requires": "owner|workspace|review", "acknowledged": false}
  ],
  "count": 1,
  "truncated": false,
  "limit": 50,
  "sources": {"missions": "ok", "autopilot": "empty", "controller_attention": "empty",
              "work_contract_review": "empty", "acks": "empty"},
  "complete": true,
  "hidden_acknowledged": 0,
  "skipped_sources": [],
  "read_only": true,
  "generated_at": "<ISO-8601>"
}
```

Items are sorted newest first and capped at `limit`. `truncated` is true when more items existed. Acknowledged items are removed before the cap is applied, so they do not use up `limit`. `hidden_acknowledged` counts the acknowledged items removed from this result; it is `0` when `include_acknowledged=true`. `skipped_sources` is currently always empty; every source is read.

Each source is one of:

- `ok`: at least one item was read from it (for `acks`: at least one acknowledgement is stored in the read window).
- `empty`: it was read successfully and had nothing waiting.
- `unavailable`: it could not be read reliably. `complete` is then `false`.

An empty `items` list is not proof of "nothing pending" unless `complete` is `true`.

### Sources and kinds

| Source | Read path (existing) | Kind | Suggested tool | `requires` |
| --- | --- | --- | --- | --- |
| `missions` | `operator_mission_runtime.hermes_mission_list(status="awaiting_approval")` | `mission_awaiting_approval` | `hermes_mission_approve` | `owner` |
| `autopilot` | `operator_autopilot.observe_status` for each run record; included when the effective state is `waiting_for_owner` or the summary has `needs_owner` | `autopilot_waiting_owner` | `hermes_mission_approve` if the mission is awaiting approval, else `hermes_plan_node_transition` when frontier nodes exist, else none | `owner` |
| `controller_attention` | bounded tail of `missions/controller_attention_spool.jsonl` (last 256 KiB, at most 200 lines) | `budget_breaker`, `red_health`, `no_capable_target` | `hermes_budget_check`, or `hermes_controller_status` | `owner` (`no_capable_target`: `workspace`) |
| `work_contract_review` | delegation rows in `delegations/delegations.db`, their validation manifests, and `operator_review` acceptance records (see below) | `contract_review_pending` | `hermes_review_accept` | `review` |
| `acks` | bounded tail of `missions/attention_acks.jsonl` (last 1 MiB) | none; it filters the other sources | none | none |

Notes on each source:

- **missions**: the summary is a fixed sentence. Mission titles and objectives are never copied into the inbox.
- **autopilot**: each run record must parse. A corrupt record makes the source `unavailable`. The summary lists frontier node ids and owner-severity attention codes.
- **controller_attention**: a malformed line makes the source `unavailable`. Repeated envelopes with the same `dedupe_key` appear once, newest kept.
- **work_contract_review**: see [Work Contract review source](#work-contract-review-source).
- **acks**: see [Acknowledgements](#acknowledgements).

### Work Contract review source

An item of kind `contract_review_pending` means a delegation executed successfully, its immutable Work Contract requires review, and no review acceptance covers it yet.

The source reads existing records only, with no fresh validation. A delegation row qualifies when all of the following hold:

1. `delegations.state = 'reconciling'`, with `cancel_requested = 0` and `cancellation_in_progress = 0`. Reconciliation parks a successful backend run in `reconciling` while its contract verdict is not `SATISFIED`.
2. Its backend outcome (`outcome`, falling back to `backend_state`) normalizes to `succeeded` through `operator_delegations._normalize_state`.
3. Its persisted validation summary (`validation_summary_json`) has the `review` check as `FAIL` or `UNVERIFIED`. The `review` check is the blocker, which the reconcile step recorded.
4. Every other check the immutable contract requires is `PASS`: `run_state` and `authorization` always, `artifacts` when `artifacts_present`, `tests` when `tests_pass`, and `forbidden` when `no_forbidden_actions`.
5. The validation manifest (`delegation_validation_manifests`) matches the row's `contract_sha256` and `task_id`, and its `review_requirements.required` is `true`.
6. No acceptance record in `review-evidence/review-acceptances.jsonl` has the same `contract_sha256`, `verdict = SATISFIED`, and a `reviewer` outside the contract's attributable identities (`assigned_profile` and `assigned_agent`). This is the same evidence rule the validator applies to review-evidence records.

Reading a delegation the review source cannot classify makes the source `unavailable`, not silently empty. The triggers are a corrupt or unreadable delegation store, a corrupt summary, a missing or mismatched manifest, more than 200 `reconciling` rows, a review-evidence store that cannot be read, or more than 1,000 acceptance lines for one contract. A missing delegation store is `empty`.

Two consequences follow from using persisted records:

- A delegation appears only after a reconcile has persisted the `review` check. A backend that just finished is not listed until `hermes_delegation_reconcile` runs.
- An acceptance record clears the item at once. The delegation's stored state stays `reconciling` until the next reconcile, which is the existing reconcile path, not the inbox.

The summary carries the first 12 hex characters of the contract digest and the task id, never the objective. To get the full `contract_sha256` for `hermes_review_accept`, call `hermes_delegation_get`.

### Stable item ids

Every item has `item_id`: the first 24 lowercase hex characters of

```text
sha256("<kind>|<mission_id>|<stable_key>")
```

where `mission_id` is the item's `mission_id` (empty string when none, truncated to 128 characters). `stable_key` is source-specific and uses no timestamps:

| Kind | `stable_key` |
| --- | --- |
| `mission_awaiting_approval` | the Mission status (`awaiting_approval`) |
| `autopilot_waiting_owner` | the sorted frontier node ids, comma-joined (all of them, not only the first 8 displayed) |
| `budget_breaker`, `red_health`, `no_capable_target` | the envelope's `dedupe_key`, or `<occurred_at>|<kind>` when there is no `dedupe_key` |
| `contract_review_pending` | `<contract_sha256>|<delegation_id>` |

Ids do not change when nothing changed, and they do not depend on `updated_at` or other timestamps. Two different items have different ids. An acknowledgement keys on `item_id`. Because `mission_awaiting_approval` is keyed on the Mission id and status only, an acknowledged approval item stays hidden if its Mission leaves `awaiting_approval` and later re-enters it: the id is the same. An acknowledgement of a spool envelope is keyed on its `dedupe_key` (or timestamp and kind), so it covers that exact condition only.

### Summaries and redaction

Every summary string passes through `ui_security.redact_browser` and is capped at 240 characters. Summaries carry ids, states and short fixed text, never raw prompts, transcripts, memory bodies, objectives or secrets.

### Read-only guarantee

`build_inbox` uses only read paths. Opening the Mission and delegation SQLite databases read-only can make SQLite create or update `-shm` / `-wal` sidecar files. Those are SQLite internals, not durable records. The test `test_build_inbox_writes_nothing` in `test_operator_owner_inbox.py` checks that durable files and Mission rows are unchanged, and the review-source test checks the same with a delegation pending review.

## Acknowledgements

Store: `missions/attention_acks.jsonl` under the Hermes root (`ACK_STORE_RELATIVE`). Each line is one JSON object:

```json
{"acked_at": "<ISO-8601>", "by": "owner_inbox_ack", "item_id": "<24 hex chars>"}
```

The file is append-only. Reads take the last 1 MiB; a partial first line in that window is dropped. An acknowledgement is not removed by any tool in this release.

Reading rules:

- A malformed line or invalid record in the read window makes the `acks` source `unavailable`. `complete` becomes `false`, and every item is treated as unacknowledged. The inbox still returns items.
- An acknowledged item is hidden unless `include_acknowledged=true`. The flag adds `"acknowledged": true` to shown items.

### Writer: `hermes_owner_inbox_ack`

MCP tool: `hermes_owner_inbox_ack(item_ids, dry_run=True)`. It is always registered; the server supplies the data root. The MCP `hermes_owner_inbox` tool takes `limit` and `include_acknowledged`.

Signature: `hermes_owner_inbox_ack(item_ids: list[str], dry_run: bool = True, hermes_root: Path | None = None) -> str`.

**Gate:** the workspace Operator level plus direct apply mode, through `OperatorPolicy.require_level("workspace")` and `require_mutation(dry_run)`. Dry run is the default. Direct mode means `HERMES_GPT_OPERATOR_ENABLED=1`, `HERMES_GPT_OPERATOR_LEVEL` at least `workspace`, and `HERMES_GPT_OPERATOR_APPLY_MODE=direct`, with `dry_run=false`.

Why this gate: an acknowledgement only hides an item. It is a local, append-only, low-impact write. Owner Mode is not required, because an acknowledgement approves nothing. Workspace is the level the existing local-state writers use (`hermes_mission_create`, for example), and the existing `require_mutation` check governs direct apply. The signature has no `confirm` argument, so the gate is the level plus apply mode, with dry-run as the default. Other writers (`hermes_review_accept`, `hermes_mission_create`) require `confirm=true` for a direct write; that is a separate decision if a caller needs it.

Behavior:

- **Refused** (`success: false`, code `OWNER_INBOX_ACK_DENIED`) when the policy is below `workspace`. When the apply mode is not `direct`, `dry_run=false` returns a dry-run plan (`dry_run: true`, `changed: false`) with nothing written, the same convention as `hermes_mission_create`.
- **Validation**: `item_ids` must be a list of 1 to 50 entries, each exactly 24 lowercase hex characters. Violations return `INVALID_OWNER_INBOX_ACK` and write nothing. Raw values are never echoed in the error.
- **Known and unknown ids**: only ids present in the current inbox, including already-acknowledged ones, are accepted. The inbox is read with `limit=200` and `include_acknowledged=true`. Other ids are reported in `unknown`. An unknown id is not an error.
- **Dry run** (the default): returns `{"success": true, "dry_run": true, "changed": false, "planned": [...], "already_acknowledged": [...], "unknown": [...]}` and writes nothing.
- **Real write**: appends one line per planned id under a process lock. An id already in the store is skipped, so re-acknowledging writes nothing (`changed: false`, the id listed in `already_acknowledged`).
- **Unreadable ack store**: `OWNER_INBOX_ACK_STORE_UNAVAILABLE`, and nothing is written.
- **Audit**: every call writes one record through `operator_policy.audit_record`, including denials and dry runs.

Acknowledging never approves, transitions, or changes a Mission, plan, Work Contract, or delegation. The mission, plan, contract, and delegation stores are unchanged by the writer. The test `test_ack_never_mutates_mission_plan_or_contract_stores` checks this.

## How to act on an item

Use the existing gated tools, not the inbox:

- `mission_awaiting_approval`: inspect with `hermes_mission_get`, then `hermes_mission_approve` (Owner mode, dry-run first, `confirm=true` for a direct write).
- `autopilot_waiting_owner`: inspect with `hermes_autopilot_status`. Resolve the frontier with `hermes_plan_node_transition` or `hermes_mission_approve`, under the same gates.
- `budget_breaker`: inspect with `hermes_budget_check`.
- `red_health`, `no_capable_target`: inspect with `hermes_controller_status`.
- `contract_review_pending`: inspect with `hermes_delegation_get` for the full `contract_sha256`, then `hermes_review_accept`. That tool needs Owner Mode with a reviewer distinct from the assignee. It is dry-run by default, and a direct write needs `dry_run=false` with `confirm=true`. The item clears once the acceptance exists; the delegation state changes only after `hermes_delegation_reconcile`.

Once a condition is handled, the item can be acknowledged with `hermes_owner_inbox_ack` to hide it. Acknowledging is not handling: it does not resolve the underlying condition.

The inbox never calls these tools.

## Limits and boundaries

- Not an approval queue. Approving or rejecting still happens only through the tools above.
- Not a notification system. Nothing is pushed by this inbox; the inbox is pulled. An optional, default-off relay can push new items as notifications: see [attention-delivery.md](attention-delivery.md). The relay skips items already acknowledged through `hermes_owner_inbox_ack`.

- `limit` above 200 is rejected, not clamped.
- The acknowledgement read window is the last 1 MiB. An acknowledgement older than that window no longer hides its item. A long-lived store needs compaction or a larger window before it reaches that size; this is a known limit.
- Review evidence is read in full, one line at a time, for the contract being checked. The owner inbox and the contract validator use the same read (`operator_review.acceptance_records_for`), so they agree. Lines longer than 64 KiB are skipped, and so are lines that are not JSON. A corrupt review-acceptance line therefore cannot count as an acceptance. A store that cannot be read, or more than 1,000 matching acceptance lines for one contract, makes the source unavailable in the inbox and counts as no evidence in the validator.

Historical context: the Autopilot `waiting_for_owner` state and the Mission `awaiting_approval` state are described in [autopilot.md](autopilot.md) and [missions.md](missions.md). The Flight Deck Mission views are in [flight-deck-missions.md](flight-deck-missions.md). The review-acceptance record is described in the Work Contract and review docs.
