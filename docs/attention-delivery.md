# Attention delivery (default off)

Status: **shipped in v0.16.0; default off.** Verify the installed version and that `operator_attention_delivery.py` exists before relying on it (see [runtime provenance](runtime-checkout.md)).

## Current behavior

The attention delivery relay sends **new** [Owner inbox](owner-inbox.md) items to configured messaging targets. It is off unless the administrator sets `HERMES_GPT_ATTENTION_DELIVERY=1`. When off, every entry point is a no-op and returns `{"enabled": false}`.

It is a **notification relay only**. Its MCP surface is one read-only status tool, `hermes_attention_delivery_status`, which is registered only when `HERMES_GPT_ATTENTION_DELIVERY=1` and never sends anything. It sends a short message saying something is waiting on the Owner. It does not approve, advance, reply to, or change anything, and a message never contains approvals or commands. Replying to a message does nothing. The Owner acts in Flight Deck or with the gated tools named in the message, under their normal gates.

Code: `operator_attention_delivery.py`. Tests: `test_operator_attention_delivery.py`.

### What is sent

- Each eligible inbox item becomes one message, or, when more than `HERMES_GPT_ATTENTION_MAX_PER_RUN` new items are waiting for one target, **one digest message** for that target.
- A message has a fixed header `Hermes GPT needs you`, the item kind label, the mission id, the inbox summary (already redacted, then re-redacted), the suggested tool name, and the optional link. When the inbox is incomplete, a line `Some sources unavailable` is added.
- Lines starting with `MEDIA:` and control characters are removed. Each message is capped at 1,000 characters.
- Messages never contain raw prompts, objectives, transcripts, memory bodies, or secrets.

### What is skipped

- Items without an `item_id` (or with a malformed one) are skipped and counted as `missing_item_id`.
- Items with `acknowledged: true` are skipped.
- Items whose `since` is older than 14 days are skipped as `stale`, so pruned delivery records cannot make old items look new again.
- Items already delivered to that target are not sent again.

### Delivery state and failures

- An item is marked delivered **only after its backend reports success**.
- A failed target records a bounded error code: `TIMEOUT`, `EXIT_<n>`, `BAD_RESPONSE`, `NOT_CONFIGURED`, or `SPAWN_FAILED`. It backs off exponentially per target (30 s, doubling, capped at 1 h) and is not retried inside the window. Other targets keep going.
- State lives in `missions/attention_delivery_state.json` under the Hermes data root. It is written atomically, pruned to 14 days and at most 2,000 entries per target, and protected by a single-writer lock (`missions/attention_delivery.lock`). If the state file is unreadable, nothing is sent.

## Configuration

All settings are **administrator environment variables**. Tool arguments cannot set them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HERMES_GPT_ATTENTION_DELIVERY` | unset (off) | Must be exactly `1` to enable. |
| `HERMES_GPT_ATTENTION_TARGETS` | required when enabled | 1 to 8 comma-separated `backend:target` entries, e.g. `hermes:telegram,hermes:discord:#ops,imessage:+15551234567`. |
| `HERMES_GPT_HERMES_CLI` | none | Absolute path to the `hermes` executable. Required only if a `hermes:` target is configured. |
| `HERMES_GPT_IMSG_CLI` | none | Absolute path to the `imsg` executable. Required only if an `imessage:` target is configured. |
| `HERMES_GPT_ATTENTION_LINK_BASE` | unset | Optional `https://` base. When set, each message ends with `<base>/ui/ops/owner-inbox`. |
| `HERMES_GPT_ATTENTION_MAX_PER_RUN` | `5` | 1 to 20 per target per pass, one message each. More than this becomes one digest. |

Invalid configuration fails closed with exit code `2` and a message that does not echo the configured values. Each executable must exist, be a regular file, and be executable.

Target syntax:

- `hermes:<target>`: Hermes' own target syntax, validated with `^[a-z_]+(:[A-Za-z0-9#@+._:-]{1,200})?$`. Examples: `telegram`, `telegram:-100123`, `discord:#ops`, `slack:C0123`, `signal:+1555...`.
- `imessage:<handle>`: an E.164 phone (`^\+[1-9][0-9]{6,14}$`) or an email address.

## Backends

The relay holds **no platform credentials**. Each backend reuses an existing tool as a child process, with a fixed argv, `shell=False`, a 20 second timeout, stdout and stderr bounded to 64 KiB, kill and reap on timeout, and a minimal environment: `PATH` set to the executable's directory plus `/usr/bin:/bin`, and only `HOME` and `LANG` forwarded. No OAuth, bearer, TypeSafe, or cloud variables are passed.

### `hermes`: Hermes Agent delivery

Fixed argv, message on stdin:

```text
<HERMES_GPT_HERMES_CLI> send --to <target> --file - --quiet --json
```

Success is exit code `0` and no JSON failure report on stdout. Exit codes from `hermes send` are `0` ok, `1` delivery or backend error, `2` usage error. `hermes send` works without a running gateway, using the platform credentials in Hermes' own configuration.

### `imessage`: `imsg` JSON-RPC

Fixed argv:

```text
<HERMES_GPT_IMSG_CLI> rpc --json
```

The relay writes two JSON-RPC 2.0 requests on stdin, one per line:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","id":2,"method":"send","params":{"to":"+15551234567","text":"<message>","service":"imessage"}}
```

It reads stdout until the response with `id` 2 arrives, closes stdin, and reaps the child. Success requires a JSON-RPC response with `result` and no `error`. The `send` params (`to`, `text`, `service`) come from the `imsg` 0.15 RPC method table; they are not in the CLI help, so re-check them after upgrading `imsg`.

## Setup

### Hermes target

1. Configure the platform in Hermes Agent first. The relay does not configure platforms.
2. Run `hermes send --list` and confirm the target appears (for example `telegram` or `discord:#ops`).
3. Set `HERMES_GPT_HERMES_CLI` to the absolute path of the `hermes` executable (for example `command -v hermes`).

### iMessage target

1. Sign in to Messages on the Mac that runs the relay.
2. Grant macOS Automation and Full Disk Access to the process that runs the relay. For the optional launchd service below, that is the Python interpreter it launches. Grant it in System Settings, then restart the service.
3. Set `HERMES_GPT_IMSG_CLI` to the absolute path of `imsg` (for example `command -v imsg`).

Basic `send` is the path this relay uses. Plain `imsg send` and the RPC `send` method do not need the IMCore bridge. Features such as effects, replies, and attachments through `send-rich` do.

### First run

Start with one target and one pass, and watch the summary:

```bash
HERMES_GPT_ATTENTION_DELIVERY=1 \
HERMES_GPT_ATTENTION_TARGETS=imessage:+15551234567 \
HERMES_GPT_IMSG_CLI=/opt/homebrew/bin/imsg \
python -m operator_attention_delivery --once
```

The first pass delivers every eligible item that has an `item_id` and is not stale. Check the summary before adding targets.

## Running

```bash
python -m operator_attention_delivery --once                  # one pass (default)
python -m operator_attention_delivery --watch --interval 60   # repeat until SIGTERM/SIGINT; 15-3600 s
hermes-gpt-attention-relay --once                             # same as the module, from the installed package
```

Exit codes: `0` on success or when disabled, `2` on configuration error (or an out-of-range `--interval`), `1` on an unexpected run failure. A pass that cannot take the state lock reports `status: skipped`, `reason: locked`.

Output is one bounded JSON object per pass: `status`, `complete`, `items`, `targets` (with masked targets), `messages_sent`, `items_delivered`, `errors`, and `generated_at`.

### Optional launchd service

`--watch` is an optional, separate service. Keep it off unless you want unattended notifications. Example user agent, with placeholder paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.hermes-gpt.attention-delivery</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/hermes-gpt/.venv/bin/python</string>
    <string>-m</string>
    <string>operator_attention_delivery</string>
    <string>--watch</string>
    <string>--interval</string>
    <string>60</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/hermes-gpt</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HERMES_GPT_ATTENTION_DELIVERY</key><string>1</string>
    <key>HERMES_GPT_ATTENTION_TARGETS</key><string>imessage:+15551234567</string>
    <key>HERMES_GPT_IMSG_CLI</key><string>/opt/homebrew/bin/imsg</string>
    <key>HERMES_GPT_ATTENTION_LINK_BASE</key><string>https://your-host.example</string>
  </dict>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
```

Load it with `launchctl bootstrap gui/$(id -u) <plist>`. The example has no `KeepAlive`: a configuration error exits `2` and stays stopped until you fix the configuration and restart the service.

## Status (read-only)

`status(hermes_root)` returns the enabled flag, configuration state, each target with a masked form (phone shown as `+1•••4567`), last success and last error, failure count, next attempt, and pending count. It writes nothing. The MCP tool `hermes_attention_delivery_status()` returns the same JSON with no arguments; the server supplies the data root. The Flight Deck does not expose it.

## Safety boundary

- **Notification only.** Messages describe what is waiting. They never carry approvals, commands, or links that act. Replying to a message does nothing.
- **No second gateway and no credential authority.** Delivery is Hermes Agent's and `imsg`'s own path. The relay stores no platform tokens and runs no agent loop. See [BOUNDARY.md](../BOUNDARY.md).
- **Administrator-only configuration.** The relay reads environment variables. No MCP tool can enable it, set a target, or choose an executable.
- **Fixed argv, no shell, minimal environment.** Child processes get no inherited secrets.
- **Redaction before send.** Summaries pass through the shared redactors and a local pass for bearer tokens, `sk-` keys, and `password=`/`token=`/`secret=`/`api_key=` assignments. Raw prompts, objectives, and transcripts are never read into a message.
- **Fail closed.** Invalid configuration, an unreadable inbox, or an unreadable state file sends nothing.
- **At-least-once, bounded.** An item is marked delivered only after success. If a send succeeds but the state file cannot be written, the next pass may send that item again, and the run reports `STATE_WRITE_FAILED`.
- **Delivery is not acknowledgement.** Acknowledging an item with `hermes_owner_inbox_ack` stops future delivery of it. Delivery does not acknowledge anything.

## Limits

- No reply handling or per-user routing.
- No delivery control over MCP or in the Flight Deck. The relay runs as a CLI, `python -m operator_attention_delivery` or the `hermes-gpt-attention-relay` console script.
- `imsg` delivery has not been exercised live in development. Test with one of your own handles before relying on it.
- Digest messages report counts and kinds, not per-item detail.

Related: [owner-inbox.md](owner-inbox.md) (the inbox this relay reads), [operator-mode.md](operator-mode.md), [jev.md](jev.md).
