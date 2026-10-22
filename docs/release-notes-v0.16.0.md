# Hermes GPT v0.16.0 — Close the Loop

Release date: October 21, 2026.

> **STATUS: PUBLISHED.** GitHub release [v0.16.0](https://github.com/asimons81/hermes-gpt/releases/tag/v0.16.0) is live with the wheel, sdist, and `SHA256SUMS` attached, and [`hermes-gpt==0.16.0`](https://pypi.org/project/hermes-gpt/0.16.0/) is on PyPI (both verified 2026-10-21). Verify each channel independently.

v0.16.0 makes what waits on the Owner reach them and lets them resolve it in one place, without adding any new authority. Owner inbox items can now be sent as notifications to Hermes or iMessage targets. The inbox includes Work Contracts that need review acceptance, has stable item ids, and can be acknowledged. Flight Deck offers the same gated approvals and stops that the tools already provide. A one-call Mission launch is available behind the Autopilot gate, and a safe package upgrade command is available. Every new mutating path keeps its existing gates.

## Notifications (default off)

`operator_attention_delivery` relays **new** [Owner inbox](owner-inbox.md) items to targets you configure. It is off unless `HERMES_GPT_ATTENTION_DELIVERY=1` is set. It is a notification relay only. A message says that something is waiting on the Owner, and the Owner acts in Flight Deck or with the gated tool named in the message. Replying to a message does nothing. See [attention delivery](attention-delivery.md).

- **Hermes backend.** `HERMES_GPT_ATTENTION_TARGETS` entries such as `hermes:telegram` or `hermes:discord:#ops` are sent with `hermes send`, which uses the platforms already configured in Hermes Agent. The relay configures no platform itself. The examples in the guide are telegram, discord, slack, and signal; use `hermes send --list` to see what your installation provides.
- **iMessage backend.** `imessage:<handle>` targets are sent through `imsg rpc`, using a basic `send`. Effects, replies, and attachments are not used.
- **Delivery behavior.** One message per new item, or one digest per target when more than `HERMES_GPT_ATTENTION_MAX_PER_RUN` items are new. Items are deduplicated, items acknowledged with `hermes_owner_inbox_ack` are skipped, and a failing target backs off exponentially without blocking the others. Each child process gets a fixed argument list, no shell, a 20-second timeout, and a minimal environment.
- **Entry points.** `python -m operator_attention_delivery --once`, or the `hermes-gpt-attention-relay` console script. `--watch` runs it repeatedly as an optional, separate service.
- **Status tool.** `hermes_attention_delivery_status` is read-only and registered only with `HERMES_GPT_ATTENTION_DELIVERY=1`. It sends nothing.

## Owner inbox: review source, stable ids, acknowledgements

- **Work Contract review.** A successful delegation whose immutable contract still needs review acceptance appears as `contract_review_pending`. It is listed only after a reconcile has recorded the `review` check, and it clears as soon as a matching acceptance exists. See [owner inbox](owner-inbox.md#work-contract-review-source).
- **Stable `item_id`s.** Each item id is derived from its kind, Mission, and a timestamp-free key. An id does not change when nothing changed, so an acknowledgement stays attached to the same condition.
- **`hermes_owner_inbox_ack`.** This tool is always registered. It hides items by id and never approves, advances, or changes a Mission, plan, Work Contract, or delegation. A write needs workspace level and direct apply mode, and the tool is dry run by default. Acknowledging does not resolve the underlying condition.

## Flight Deck inbox actions

Flight Deck offers these actions from the Owner inbox. Each one is a dry-run request followed by a separate confirm. Each calls the existing gated tool through `POST /api/ops/action`, and the server enforces the tool's own checks.

| Item | Action | Tool | Gate |
| --- | --- | --- | --- |
| `mission_awaiting_approval` | Approve Mission | `hermes_mission_approve` | Owner Mode, with direct apply for a write |
| `autopilot_waiting_owner` | Approve or reject an approval-frontier node | `hermes_plan_node_transition` | Owner Mode in the browser, approval and `high_impact` nodes only |
| `autopilot_waiting_owner` | Stop Autopilot | `hermes_autopilot_stop` | Workspace level and direct apply |
| Any item | Acknowledge | `hermes_owner_inbox_ack` | Workspace level and direct apply |

See [Flight Deck actions](owner-inbox.md#flight-deck-actions) and the [UI security boundary](ui-security-boundary.md#61-browser-mutation-allowlist-current).

## Cross-site refusal on the action route

`POST /api/ops/action` now refuses cross-site requests before it parses the body. A non-JSON `Content-Type` returns `415 UNSUPPORTED_MEDIA_TYPE`. A `Sec-Fetch-Site` value other than `same-origin` or `none`, or an `Origin` whose host and port differ from `Host`, returns `403 CROSS_SITE_REFUSED`. These checks also protect the loopback default. They are added to the Operator gates and do not replace them. See [UI security boundary §6](ui-security-boundary.md#62-request-protections).

## One-call Mission launch (Autopilot-gated)

`hermes_mission_launch` runs four existing steps in order: create the Mission, derive its plan, optionally set a budget, and start Autopilot. It is registered only with `HERMES_GPT_AUTOPILOT=1`.

- It is **dry run by default**. A preview validates each step without writing. Applying needs `dry_run=false` and `confirm=true`.
- It **stops at the first refusal**. Nothing is rolled back, and the response lists the created ids so the Owner can continue with the existing tools.
- It **never approves** a Mission and refuses a spec that tries to disable final approval.
- A Mission with **no budget envelope is not held** by the budget gate. Only a crossed, invalid, or unreadable envelope holds new dispatch.
- Re-running it creates a new Mission. There is no idempotency key.

See [one-call launch](autopilot.md#one-call-launch-v016).

## Safe package upgrade

`hermes-gpt update --apply --version X.Y.Z` upgrades the `hermes-gpt` package inside one existing virtual environment. It checks the wheel digest, refuses downgrades and dependency changes unless you allow them, snapshots the previous wheel into a new rollback directory, verifies the import from a neutral directory, optionally restarts one named service, and reinstalls the snapshot if anything after the install fails. See [updating](updating.md#upgrade-a-package-installed-service-safely).

## Fixes

- **Contract review.** The validator and the owner inbox read the whole review-acceptance store. Before this fix, a busy store could leave a contract unsatisfied after its 200th acceptance, because the reader stopped early. `read_review_acceptances`, the Flight Deck recent-records list, now returns the newest records.
- **Owner inbox MCP tool.** The `hermes_owner_inbox` success path no longer fails on a positional argument.
- **Test fixture.** A `wire` fixture in `conftest.py` returns MCP objects in protocol alias form, so tests assert on protocol names instead of SDK attribute names.

## Tool count

With every optional gate unset, the main server registers **138 tools**. The connector acceptance surface, which sets `HERMES_GPT_ENABLE_FINANCE=1`, registers **139**; the only difference is `hermes_finance_analyze`. The gated surfaces add:

- `HERMES_GPT_AUTOPILOT=1`: +4 (the three Autopilot tools and `hermes_mission_launch`);
- `HERMES_GPT_ATTENTION_DELIVERY=1`: +1 (`hermes_attention_delivery_status`);
- `HERMES_GPT_ENABLE_JEV=1`: +3 Jev tools;
- both `HERMES_GPT_ENABLE_JEV=1` and `HERMES_GPT_REVIEW_ASSIST=1`: +1 `hermes_review_assess`.

With `HERMES_GPT_TOOL_PROFILE=chat-lean` and every gate unset, **30 tools** are advertised; with `HERMES_GPT_AUTOPILOT=1` added, **34**. Profiles only remove tools.

## Upgrading

Use the safe package upgrade. It requires `0.16.0` on PyPI, so verify that channel first, or pass a local wheel with `--wheel`:

```sh
hermes-gpt update --apply --version 0.16.0 \
  --venv /absolute/path/to/venv \
  --rollback-dir /absolute/path/to/new-empty-rollback-dir \
  [--service NAME]
```

- Add `--allow-dependency-changes` only after reviewing the reported differences.
- A venv shared by several services changes for all of them.
- After the upgrade, read `serverInfo.version` through your MCP client, confirm the tool inventory, and refresh the client's cached tool list. The updater cannot authenticate to the service.
- To roll back, follow the commands in the `ROLLBACK.md` the updater writes into the rollback directory.

Installing 0.16.0 does not turn on notifications, the one-call launch, or the Owner inbox actions. Each still needs its own gate and setup.

## Setting up notifications

Start with one target and one pass, and check the summary before adding targets. Full details are in [attention delivery](attention-delivery.md).

**Hermes target**

1. Configure the platform in Hermes Agent, then run `hermes send --list` and confirm the target appears.
2. Run the relay once:

   ```sh
   HERMES_GPT_ATTENTION_DELIVERY=1 \
   HERMES_GPT_ATTENTION_TARGETS=hermes:telegram \
   HERMES_GPT_HERMES_CLI="$(command -v hermes)" \
   hermes-gpt-attention-relay --once
   ```

**iMessage target**

1. Sign in to Messages on the Mac that runs the relay, and grant the relay's Python process Automation and Full Disk Access in System Settings.
2. Run the relay once:

   ```sh
   HERMES_GPT_ATTENTION_DELIVERY=1 \
   HERMES_GPT_ATTENTION_TARGETS=imessage:+15551234567 \
   HERMES_GPT_IMSG_CLI="$(command -v imsg)" \
   hermes-gpt-attention-relay --once
   ```

Replace the placeholder handle with one of your own. Use `--watch --interval 60` only when you want unattended notifications, and keep it as a separate service.

## Cautions

- **Notifications are off by default and notification-only.** Delivery is not acknowledgement, and a message never carries an approval or a command.
- **The iMessage path has not been exercised live in development.** Test with one of your own handles before you rely on it.
- **Acknowledgements are read from the last 1 MiB of their store.** A very long-lived store needs compaction or a larger window before an old acknowledgement stops hiding its item. This is a known limit.
- **Re-running the launch creates a new Mission**, and a partial launch is not rolled back.
- **Acknowledging an item does not resolve it.** Handle the condition with the gated tool named in the item.
- **Flight Deck approval is Owner-only.** Browser node transitions require Owner Mode, and Mission approval still goes through `hermes_mission_approve`'s own Owner check.

## Verification

Follow [the maintainer release checklist](../RELEASE_CHECKLIST.md). For this release that includes the focused owner-inbox, attention-delivery, registration, and tool-profile suites; the full Python suite; the package build, `twine check`, and package hygiene checks; and the connector acceptance gate, which must report 139 tools with `serverInfo.version` equal to `0.16.0`. GitHub and PyPI are independent channels. Verify each published version directly, and verify the running service's MCP `serverInfo.version` and tool inventory after any upgrade. Installing an earlier release does not establish that these features are present.

Current guides: [owner inbox](owner-inbox.md), [attention delivery](attention-delivery.md), [Autopilot](autopilot.md), [updating](updating.md), [UI security boundary](ui-security-boundary.md), and [Operator mode](operator-mode.md).
