# Hermes GPT v0.15.0 — Owner Loop

Release date: October 21, 2026.

> **STATUS: PUBLISHED.** GitHub release [v0.15.0](https://github.com/asimons81/hermes-gpt/releases/tag/v0.15.0) is live with the wheel, sdist, and `SHA256SUMS` attached, and [`hermes-gpt==0.15.0`](https://pypi.org/project/hermes-gpt/0.15.0/) is on PyPI (both verified 2026-10-21). Verify each channel independently.

v0.15.0 makes Owner decisions faster without adding any new authority. It lists what is waiting on the Owner in one read-only place, adds an advisory review score that never counts as evidence, lets chat clients use a smaller tool surface, and makes file backups safer to configure. Every new mutating path keeps its existing gates.

## Owner inbox (read-only)

`hermes_owner_inbox` lists Missions awaiting approval, Autopilot runs waiting for the Owner, and recent controller attention (budget breaker, RED health, no capable target). It is always registered and is read-only: it approves, advances, and writes nothing. Each item names the existing gated tool to use next. Check `sources` and `complete` before treating an empty list as "nothing pending". Work Contracts awaiting review acceptance are not yet covered and are listed in `skipped_sources`. A read-only Flight Deck route, `GET /api/ops/owner-inbox`, returns the same projection, and the Deck overview links to a read-only Owner inbox panel (`/ops/owner-inbox`) that shows unavailable or skipped sources instead of an empty-inbox reassurance. See [owner inbox](owner-inbox.md).

## Advisory review assist (default off)

`hermes_review_assess` asks the pinned Jev adapter for an advisory score of how well a dispatched Work Contract's state fits its completion criteria. It is registered only when both `HERMES_GPT_ENABLE_JEV=1` and `HERMES_GPT_REVIEW_ASSIST=1` are set. It requires the `read_only` Operator level, sends the criteria and at most one bounded artifact to TypeSafe, appends each advisory to a separate store, and writes an audit record without a `task_id`. Advisories are never review acceptance and never Work Contract evidence; the validator's verdict is identical before and after one is written. See [review assist](review-assist.md).

## Jev decision bridge (default off)

The three typed judgments (`hermes_jev_yes_no`, `hermes_jev_choose`, `hermes_jev_score`) are unchanged in behavior and are registered only with `HERMES_GPT_ENABLE_JEV=1`. Installing a package does not provision the Owner key or activate the gate. See [Jev](jev.md).

## Connector tool profiles

`HERMES_GPT_TOOL_PROFILE` selects a smaller advertised surface:

- unset or `full` (default): every registered tool, unchanged;
- `operator`: `full` without the raw host primitives (`hermes_read_file`, `hermes_write_file`, `hermes_patch`, `hermes_search_files`, `hermes_run_command`, `hermes_memory`);
- `chat-lean`: about 29 status, oversight, and owner-decision tools with every gate off.

A profile only removes tools. It never adds a tool or bypasses an env gate, a confirm or dry-run check, or Operator/Owner policy. Any other value fails closed at startup. See [connector tool profiles](mcp-compatibility.md#connector-tool-profiles).

## Configurable file backups

Thanks to @brunocasado for the change in #85. `HERMES_GPT_OPERATOR_FILE_BACKUPS` now disables backup copies only for an explicit `0`, `false`, `no`, `off`, or `disabled` (case-insensitive). An unset, empty, or unrecognized value keeps backups on, so a typo cannot silently remove the rollback copy. See [Operator mode](operator-mode.md#optional-file-backups).

## Tool count

With optional gates unset the main server registers **138 tools** (137 in v0.14.0, plus `hermes_owner_inbox`). The gated surfaces add:

- `HERMES_GPT_AUTOPILOT=1`: +3 Autopilot tools;
- `HERMES_GPT_ENABLE_JEV=1`: +3 Jev tools;
- both `HERMES_GPT_ENABLE_JEV=1` and `HERMES_GPT_REVIEW_ASSIST=1`: +1 `hermes_review_assess`.

## Verification and upgrade

Follow [the maintainer release checklist](../RELEASE_CHECKLIST.md): the focused owner-inbox, review-assist, tool-profile, and registration regressions, the full Python suite, the package build and hygiene checks, and the connector acceptance gate. GitHub and PyPI are independent channels. Verify each published version directly before claiming availability, and verify the running service's MCP `serverInfo.version` and tool inventory after upgrade. Installing an earlier release does not establish that these features are present.

Current guides: [owner inbox](owner-inbox.md), [review assist](review-assist.md), [Jev](jev.md), [Operator mode](operator-mode.md), and [MCP compatibility](mcp-compatibility.md).
