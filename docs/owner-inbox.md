# Owner inbox (read-only)

Status: **shipped in v0.15.0**; always registered (read-only). Verify the installed version and tool inventory before relying on it (see [runtime provenance](runtime-checkout.md)).

## Current behavior

The Owner inbox lists everything currently waiting on the Owner in one place. It is a **read-only projection** over existing read paths. It writes nothing, heals nothing, approves nothing, and advances nothing. It adds no authority: every suggested action is an existing tool, and that tool's own gates (Owner level, dry-run first, confirm, audit) still apply.

Code: `operator_owner_inbox.py`.

- `build_inbox(hermes_root: Path | None = None, limit: int = 50) -> dict`. `limit` must be an int from 1 to 200; anything else raises `ValueError`.
- `hermes_owner_inbox(limit: int = 50, hermes_root: Path | None = None) -> str`. JSON-string wrapper that returns `{"success": true, "tool": "hermes_owner_inbox", ...}`, or an error envelope for an invalid limit or a denied read level. It writes one audit record through `operator_policy.audit_record`.

Flight Deck route: `GET /api/ops/owner-inbox?limit=N` in `ui_ops.py`. It returns the same projection in the `{"ok": true, "data": ...}` envelope, redacted by `ui_security`. There is no POST. It respects the `approvals` entry of the Mission allowlist (`HERMES_GPT_MISSION_ALLOWED_SURFACES`: unset allows, a list allows only listed surfaces, empty denies). A bad `limit` returns `400 INVALID_LIMIT`.

### Result shape

```json
{
  "items": [
    {"kind": "...", "mission_id": "msn-...", "summary": "...", "since": "<ISO-8601 or null>",
     "suggested_tool": "<tool name or null>", "requires": "owner|workspace|review"}
  ],
  "count": 1,
  "truncated": false,
  "limit": 50,
  "sources": {"missions": "ok", "autopilot": "empty", "controller_attention": "empty"},
  "complete": true,
  "skipped_sources": ["work_contract_review"],
  "read_only": true,
  "generated_at": "<ISO-8601>"
}
```

Items are sorted newest first and capped at `limit`. `truncated` is true when more items existed.

Each source is one of:

- `ok`: at least one item was read from it.
- `empty`: it was read successfully and had nothing waiting.
- `unavailable`: it could not be read reliably. `complete` is then `false`.

An empty `items` list is not proof of "nothing pending" unless `complete` is `true`.

### Sources and kinds

| Source | Read path (existing) | Kind | Suggested tool | `requires` |
| --- | --- | --- | --- | --- |
| `missions` | `operator_mission_runtime.hermes_mission_list(status="awaiting_approval")` | `mission_awaiting_approval` | `hermes_mission_approve` | `owner` |
| `autopilot` | `operator_autopilot.observe_status` for each run record; included when the effective state is `waiting_for_owner` or the summary has `needs_owner` | `autopilot_waiting_owner` | `hermes_mission_approve` if the mission is awaiting approval, else `hermes_plan_node_transition` when frontier nodes exist, else none | `owner` |
| `controller_attention` | bounded tail of `missions/controller_attention_spool.jsonl` (last 256 KiB, at most 200 lines) | `budget_breaker`, `red_health`, `no_capable_target` | `hermes_budget_check`, or `hermes_controller_status` | `owner` (`no_capable_target`: `workspace`) |

Notes on each source:

- **missions**: the summary is a fixed sentence. Mission titles and objectives are never copied into the inbox.
- **autopilot**: each run record must parse. A corrupt record makes the source `unavailable`. The summary lists frontier node ids and owner-severity attention codes.
- **controller_attention**: the spool has no acknowledgement record, so every spooled envelope in the window is treated as unacknowledged. A malformed line makes the source `unavailable`. Repeated envelopes with the same `dedupe_key` appear once, newest kept.

### Skipped source

`work_contract_review` is **not implemented**. `operator_review` stores only completed review-acceptance records, and no cheap pending-review read path exists. It is listed in `skipped_sources` so the gap is visible. `complete` covers only the three sources above.

### Summaries and redaction

Every summary string passes through `ui_security.redact_browser` and is capped at 240 characters. Summaries carry ids, states and short fixed text, never raw prompts, transcripts, memory bodies or secrets.

### Read-only guarantee

`build_inbox` uses only read paths. Opening the Mission SQLite database read-only can make SQLite create or update its `missions.db-shm` / `missions.db-wal` sidecar files. Those are SQLite internals, not durable Mission records. The test `test_build_inbox_writes_nothing` in `test_operator_owner_inbox.py` checks that durable files and Mission rows are unchanged.

## How to act on an item

Use the existing gated tools, not the inbox:

- `mission_awaiting_approval`: inspect with `hermes_mission_get`, then `hermes_mission_approve` (Owner mode, dry-run first, `confirm=true` for a direct write).
- `autopilot_waiting_owner`: inspect with `hermes_autopilot_status`. Resolve the frontier with `hermes_plan_node_transition` or `hermes_mission_approve`, under the same gates.
- `budget_breaker`: inspect with `hermes_budget_check`.
- `red_health`, `no_capable_target`: inspect with `hermes_controller_status`.

The inbox never calls these tools.

## Limits and boundaries

- Not an approval queue. Approving or rejecting still happens only through the tools above.
- Not a notification system. Nothing is pushed by this inbox; the inbox is pulled. An optional, default-off relay can push new items as notifications: see [attention-delivery.md](attention-delivery.md).
- Registered as the MCP tool `hermes_owner_inbox(limit=50)` in every profile, including `chat-lean`. The MCP input is `limit` only; `hermes_root` is not exposed. Its annotations are `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`.
- `limit` above 200 is rejected, not clamped.

Historical context: the Autopilot `waiting_for_owner` state and the Mission `awaiting_approval` state are described in [autopilot.md](autopilot.md) and [missions.md](missions.md). The Flight Deck Mission views are in [flight-deck-missions.md](flight-deck-missions.md).
