# Operator Mode for hermes-gpt

## Codex bridge posture (v0.5.0)

Codex can opt into the control plane with `hermes-gpt codex install --toolset operator --refresh`. Registration does not grant authority; the same level, profile, apply, path, redaction, and audit policy applies.

Trusted clients can delegate asynchronous Codex jobs through the eight `hermes_codex_*` tools. Set `HERMES_GPT_ENABLE_CODEX_RUNNER=1`; direct execution also requires workspace-or-higher level, direct mode, an allowed directory, `confirm=true`, and `dry_run=false`. `HERMES_GPT_ALLOW_CODEX_WRITE=1` is needed only for `workspace-write`. Normal jobs do not require Owner Mode, and raw prompts are not persisted.

`hermes-gpt` is a local MCP bridge for exposing selected Hermes Agent capabilities to trusted MCP clients like ChatGPT. It is meant to run on your machine, bound to loopback, with a tunnel in front of it only when you deliberately want remote access.

Operator Mode is the safer control plane inside `hermes-gpt`. It exposes operator tools, but tool visibility does not mean mutation is allowed. Whether a call can change anything depends on:

- the operator level
- the server apply mode
- the tool’s own `dry_run` argument
- and, for owner tools, the exact break-glass acknowledgement

Default posture should stay `dry_run` for always-on tunnel use.

## New user quickstart

1. Install and run `hermes-gpt`.
2. Start it in dry-run Operator Mode.
3. Connect ChatGPT or another MCP client to the tunnel URL.
4. Call:
   - `hermes_operator_policy`
   - `hermes_operator_status`
   - `hermes_cron_list`
5. Only switch to direct mode when you are doing a deliberate maintenance session.

## Four safety postures

### A. Read-only default

No environment variables are needed.

Behavior:

- status, read, list, and diff tools work
- mutating tools refuse because Operator Mode is disabled

Example:

```powershell
hermes-gpt
```

This is the safest starting point if you only want inspection.

### B. Dry-run Operator Mode

This is the recommended always-on tunnel posture.

Behavior:

- operator tools are available
- mutating tools return plans or previews
- nothing actually changes
- safe default for ChatGPT connector use

Example:

```powershell
$env:HERMES_HOME="C:\Users\<YOU>\AppData\Local\hermes"
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="skills_config"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="dry_run"
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES="default,hermes-researcher,hermes-trt-manager,hermes-nexus-wiki"

python server.py --http --host 127.0.0.1 --port 4750
```

### C. Direct Operator Mode

Use this only when you intentionally want writes.

Behavior:

- the server policy allows direct mutation
- individual tool calls still must pass `dry_run=false`
- mutation requires two gates:
  1. server apply mode must be `direct`
  2. the individual call must ask for `dry_run=false`

Example:

```powershell
$env:HERMES_HOME="C:\Users\<YOU>\AppData\Local\hermes"
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="skills_config"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="direct"
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES="default,hermes-researcher,hermes-trt-manager,hermes-nexus-wiki"

python server.py --http --host 127.0.0.1 --port 4750
```

A mutating tool still needs:

```json
{
  "dry_run": false
}
```

### D. Owner Mode

Break-glass only.

Behavior:

- owner tools are visible but refuse unless the exact owner acknowledgement is set
- owner mode is not recommended for always-on tunnels
- owner mode still denies secret paths

Example:

```powershell
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="owner"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="direct"
$env:HERMES_GPT_OWNER_ACK="I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE"
```

Do not use Owner Mode for public, shared, or always-on connectors.

## Operator levels

Higher levels include the lower levels before them.

| Level | What it unlocks |
| --- | --- |
| `read_only` | status, policy, audit tail, cron list/status, skill list/view/diff, config get, env status, gateway status, git status/diff |
| `cron` | plus cron run, pause, copy, move |
| `skills` | plus skill create, edit, patch, write_file, copy, sync_to_default, delete |
| `skills_config` | plus config set/patch and non-secret env set/copy |
| `workspace` | plus scoped workspace read/patch/write/test and gateway restart under allowed paths |
| `owner` | break-glass raw command and raw file patch/write; still denies secret paths and requires exact owner acknowledgement |

`skills_config` is a good normal operator level for trusted dry-run usage.
`workspace` is for scoped workspace file operations only under allowed paths.
`owner` is break-glass.

## Dry-run vs direct: the important bit

`HERMES_GPT_OPERATOR_APPLY_MODE=dry_run` means mutating tools only preview.
`HERMES_GPT_OPERATOR_APPLY_MODE=direct` means the server permits direct mutation.

But every mutating call still defaults to `dry_run=true`.
Actual mutation requires both:

- `HERMES_GPT_OPERATOR_APPLY_MODE=direct`
- tool argument `dry_run=false`

Dry-run cron move:

```json
{
  "source_profile": "hermes-researcher",
  "target_profile": "default",
  "job_id": "example-job-id",
  "pause_source": true,
  "test_run_target": false,
  "dry_run": true
}
```

Direct cron move:

```json
{
  "source_profile": "hermes-researcher",
  "target_profile": "default",
  "job_id": "example-job-id",
  "pause_source": true,
  "test_run_target": false,
  "dry_run": false
}
```

The direct version only mutates if the server is already running with apply mode `direct`.

## Recommended tunnel setup

Keep the MCP server bound to `127.0.0.1`.
Put the tunnel in front of loopback only.
Keep always-on tunnel mode in `dry_run`.
Switch to `direct` only for a deliberate maintenance session.
Switch back to `dry_run` afterward.
Never enable Owner Mode on an always-on tunnel.

Safe tunnel posture example:

```powershell
$env:HERMES_HOME="C:\Users\<YOU>\AppData\Local\hermes"
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="skills_config"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="dry_run"
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES="default,hermes-researcher,hermes-trt-manager,hermes-nexus-wiki"

python server.py --http --host 127.0.0.1 --port 4750
```

## Profile root normalization

Hermes data root is usually:

- Windows: `C:\Users\<YOU>\AppData\Local\hermes`
- Unix/macOS style: `~/.hermes`

If `HERMES_HOME` points to a named profile or `hermes-agent`, `hermes-gpt` normalizes back to the data root for operator profile operations.
The default profile maps to the data root.
Named profiles map to `profiles/<profile-name>`.

## Audit logs

Audit log path:

- `%USERPROFILE%\AppData\Local\hermes\logs\hermes_gpt_operator_audit.jsonl` (preferred)
- `<hermes-gpt>\logs\hermes_gpt_operator_audit.jsonl` (fallback)

What is logged:

- timestamp
- tool name
- level
- apply mode
- dry_run flag
- success / changed / summary
- error summary when a call fails
- profile or profiles involved
- path summary
- job id, skill name, or key when relevant
- prompt/content length plus SHA-256 for content-bearing calls

What is never logged:

- raw `.env` values
- full prompts
- full config values when they may contain secrets
- vault contents
- command output likely to contain secrets

Prompt/content is represented by length and hash only, not raw text.

## Diagnostics and recovery

v0.3.0 adds reliability tools to inspect and recover the operator surface safely.

### hermes_operator_doctor

Run this when something feels off: gateway not responding, cron jobs not firing, skills missing, or tools return unexpected failures.

It checks:

- operator runtime reachability
- gateway PID / heartbeat
- config.yaml readability
- .env readability (names only)
- cron registry readability
- skills registry readability
- operator policy validity
- last audit record readability
- connector / API bridge capability (reported as UNSUPPORTED unless a real command/API exists)

Each check returns one of: `PASS`, `WARN`, `FAIL`, `UNSUPPORTED`. The overall result is the worst non-unsupported status. If anything fails, the tool recommends `hermes_operator_recover` with `apply=false` first.

Example overall statuses:

- `PASS` — everything looks healthy.
- `WARN` — attention recommended (e.g., stale heartbeat, missing optional files).
- `FAIL` — action required (e.g., dead gateway PID, unreadable cron registry).
- `UNSUPPORTED` — a capability is not implemented; not a failure.

### hermes_operator_snapshot

Returns a single JSON summary of current state: version, profile, gateway status, cron summary, env summary, skills count, last audit timestamp, repo status, known issues, and a recommended next action. Use it for quick status checks or before running recovery.

### hermes_operator_recover

Dry-run by default. It plans a recovery sequence:

1. read config
2. validate env
3. restart gateway if the doctor check failed
4. check connector routes (reported as UNSUPPORTED)
5. recheck cron
6. recheck skill index
7. write audit record

To actually mutate, pass `apply=true` and ensure the server is in direct operator mode with level `workspace` or higher. Without those gates, recovery stays a plan.

### hermes_release_doctor

Run before tagging a release. Fast checks by default:

- git repo / branch / dirty tree
- secret-file scan (`.env`, `*.pem`, `*.key`, auth/token files, etc.)
- `pyproject.toml` version
- CHANGELOG/README/docs mention the current package version
- import / py_compile checks
- operator apply mode is not direct

Pass `full_tests=true` to also run the pytest suite. Results are classified as `PASS`, `WARN`, or `BLOCKED`; the expected release version is derived from package metadata.

### Structured errors

Operator-facing failures now return a safe envelope:

```json
{
  "success": false,
  "ok": false,
  "error": "safe human message",
  "layer": "gateway",
  "code": "GATEWAY_UNREACHABLE",
  "safe_message": "Gateway status could not be verified.",
  "suggested_action": "Run hermes_operator_recover with apply=false first.",
  "trace_id": "..."
}
```

Legacy fields (`success`, `error`) are preserved. Secrets, env values, and absolute paths are redacted.

## What is still denied

The server still refuses or redacts access to:

- `.env`
- auth files
- token files
- vault files
- SSH keys
- OAuth files
- cookies
- MCP token files
- secret-looking filenames

That denial applies even in higher modes.

## Troubleshooting

### I only see 5 tools

- Reconnect the connector.
- Create a new connector name if the old one is cached.
- Verify `/mcp` directly with list-tools.
- If direct list-tools shows 39 tools, the server is fine and the connector registration is stale.

### Profile appears missing

- Check `HERMES_HOME`.
- Confirm root normalization back to the data root.
- Remember that default resolves to the data root, while named profiles map under `profiles/<profile-name>`.

### Mutating tools refuse

Check all of these:

- `HERMES_GPT_OPERATOR_ENABLED`
- `HERMES_GPT_OPERATOR_LEVEL`
- `HERMES_GPT_OPERATOR_APPLY_MODE`
- the tool call’s `dry_run` argument

A refusal here is usually correct behavior, not a bug.

### Owner tools refuse

That is expected unless the exact owner acknowledgement is set:

```powershell
$env:HERMES_GPT_OWNER_ACK="I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE"
```

If the string differs, owner tools should still refuse.

## Keep in mind

- Operator Mode is not a sandbox.
- Public exposure is not safe without real auth.
- Direct mode is not the default.
- Owner Mode is not safe for always-on use.
- Use OS isolation for untrusted input.
