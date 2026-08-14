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

- configured `owner` is clamped to effective `workspace` unless break-glass activation is explicitly enabled
- owner tools require both the activation flag and exact owner acknowledgement
- owner mode is not recommended for always-on tunnels
- owner mode still denies secret paths

Example:

```powershell
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="owner"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="direct"
$env:HERMES_GPT_OWNER_ACTIVE="1"
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

## Fleet routing through the local A2A registry

The full Hermes GPT MCP surface includes seven fleet tools for an existing
authenticated Hermes A2A mesh:

| Tool | Access | Behavior |
| --- | --- | --- |
| `hermes_fleet_list` | read-only | Lists named local-registry peers; tokens and peer URLs never leave the host. |
| `hermes_fleet_status` | read-only | Returns a bounded metadata-only compatibility summary for one named peer. |
| `hermes_fleet_dispatch` | workspace + direct + confirmation | Submits a bounded task to one named peer. |
| `hermes_fleet_task` | read-only | Returns task id, state, timestamp, and artifact count only. |
| `hermes_fleet_dispatch_work_order` | workspace + direct + confirmation | Validates and submits a canonical profile-aware work order. |
| `hermes_fleet_result` | read-only | Returns only a validated safe completion bundle. |
| `hermes_fleet_authority_drift` | read-only | Reports registry/manifest/profile/role/Agent Card drift. |

Fleet routing is intentionally narrow. All fleet tools require enabled
read-only Operator Mode or higher. MCP callers cannot provide an endpoint,
bearer token, SSH command, or arbitrary executable. A real dispatch requires
all of: Operator Mode enabled at `workspace` or higher, server `direct` apply
mode, `dry_run=false`, and `confirm=true`. The dispatch message is recorded in
the audit log only as a length and SHA-256 digest; it is not returned in the
tool response.

`hermes_fleet_dispatch` creates remote work. Do not use it for casual probes,
and do not leave an internet-exposed connector unauthenticated merely because
the A2A peer mesh itself uses bearer authentication.

### Fleet authority manifest and deployment

Set `HERMES_GPT_FLEET_AUTHORITY_MANIFEST` to an absolute JSON file, or install
it at `<Hermes data root>/config/fleet-authority.json`. Start from
`examples/fleet-authority.example.json`. Version 1 has a `peers` array; each
peer contains `name`, `expected_host_role`, `expected_card_identity`,
`allowed_profiles`, `max_authorization`, and `allow_public_actions`. Valid
authorization classes are `none`, `read_only`, `reversible_write`, and
`high_impact`. Do not include URLs, credentials, or tokens.

1. Install the manifest for the service account (`0600` on POSIX).
2. Match names to the authenticated registry and set expected card identities
   and host roles. Keep Nous Girl limited to `default`.
3. Restart Hermes GPT, enable read-only Operator Mode, and run
   `hermes_fleet_authority_drift`.
4. Resolve findings before enabling workspace/direct mode. Dispatch still
   requires `dry_run=false` and `confirm=true`. Immediately before a confirmed
   structured dispatch, Hermes GPT runs a bounded live Agent Card check and
   requires its identity and host role to exactly match the manifest; failure
   stops the dispatch before the work order is sent. Dry runs remain network-free
   apart from the local registry lookup and do not contact the peer.

High-impact work additionally requires `approved=true` plus bounded
`approved_by` and `approval_reference` metadata. Public actions on Nous Girl,
Vault-policy edits, raw-secret requests, and work above role authority are
rejected locally. Public-action detection examines affirmative command intent
in the objective, so supporting filenames, review language, negated constraints,
and acceptance checks do not become public-action requests.

## Mission Control (v0.6 M0, read-only)

Mission Control is a read-only operational view of the whole Hermes fleet —
profiles, fleet agents, Codex jobs, cron activity, delegated work, failures,
pending approvals, vault, usage, and the operator audit trail — exposed to
trusted clients (ChatGPT first) as the `hermes_mission_*` tool family
(`operator_mission.py`).

It is structurally read-only:

- every SQLite store is opened `file:...?mode=ro`; no write / dry-run / apply
  arguments exist; no mutating shell calls are made.
- no message, memory, transcript, request-dump, or profile-secret body ever
  crosses the surface — prompts appear as `{prompt_len, prompt_sha256}` only,
  delegation/kanban bodies are stripped, Codex transcripts and Vault
  credentials are never returned, and `.env`/`auth.json`/tokens are excluded.
- every mission call is written to the operator audit log (`dry_run=true`,
  `changed=false`).

Each surface reports `available:false + reason` when its source is absent
(e.g. no Codex store on the host) rather than failing. Output is bounded per
design §8.4 (overview 64 KB / hard 128 KB; surface 256 KB / hard 512 KB), with
lists truncated via `truncated:true, count_total:N`.

Per-client authorization uses the `HERMES_GPT_MISSION_ALLOWED_SURFACES`
allowlist (deny-by-default): a comma-separated list of surface names
(`health`, `profiles`, `fleet`, `codex`, `cron`, `delegations`, `failures`,
`approvals`, `vault`, `usage`, `audit`, `overview`). When unset, all read-only
surfaces are allowed; an empty value denies everything; a listed-only value
restricts a client to that subset. This is the seam a future OAuth scope maps
onto (design D12). Denied surfaces return `AUTHZ_DENIED`, not a data error.

Mission Control requires only operator level `read_only` (the default) — it
never needs `direct` apply mode.

## Work Contracts (v0.6 M1)

Work Contracts (`operator_contract.py`) add a structured work-order layer on top
of Mission Control: declarative `hermes.work-contract/v1` documents whose
completion is verified against **observed** state, not a worker's claim (design
`docs/design/v0.6-work-contracts.md`).

Tools:

- `hermes_contract_define(contract_json)` — read-only, pure. Validates and
  canonicalizes a contract (schema, scope, forbidden actions, artifacts, tests,
  review, criteria, authorization) and returns the canonical document
  (redacted: objective appears only as `{prompt_len, prompt_sha256}`) plus
  `contract_sha256`.
- `hermes_contract_dispatch(contract_json, confirm, dry_run)` — workspace
  level, dry-run-first. Submits the contract as a fleet work order reusing the
  existing authority manifest, live peer verification, dry-run/confirm gates,
  and audit. Requires a unique `task_id` and `confirm=true` + direct mode to
  actually dispatch.
- `hermes_contract_validate(contract_json)` — read-only by default. Returns a
  deterministic verdict (`SATISFIED` / `NOT_SATISFIED` / `INCONCLUSIVE` /
  `INVALID_CONTRACT`) with per-check detail and `false_done_detected`. Evidence
  is observed-only (kanban runs, async delegations, artifacts on disk, audit
  trail); a worker-supplied result is never proof. A valid contract with no
  observed run returns `INCONCLUSIVE` (fail-closed), never `SATISFIED`.
  Test checks run only through the workspace allowlist
  (`hermes_workspace_run_test`, `shell=False`) and are individually gated at
  workspace + direct apply mode (design D6) — at `read_only` a required test is
  `UNVERIFIED` and the verdict is `NOT_SATISFIED`.
- `hermes_contract_status(contract_json)` — read-only. Links a contract to its
  observed run/delegation state (redacted summary).

Every call is audited with `contract_sha256` + `task_id`; no objective text is
ever written to the audit log or the surface.

## Swarm Orchestration (v0.6 M2)

Swarm Orchestration (`operator_swarm.py` + `operator_swarm_workflows.py`) is a
workflow engine on top of Mission Control (M0) and Work Contracts (M1): a
declarative, validated DAG of stages (design
`docs/design/v0.6-swarm-orchestration.md`). The canonical shape is

```
research → architecture → (implementation / tests / docs in parallel)
→ integration review → Codex review → acceptance validation → HUMAN APPROVAL
```

Every stage is dispatched as an M1 **contract**; completion is validated by the
M1 validator against **observed** Mission Control state, so a false "done"
claim returns the stage for rework (bounded to one retry, then blocked for a
human). Implementation stages run in upstream kanban **worktrees** (separate
branch per stage; the engine never manages git itself). Codex review drives the
existing runner unchanged (fixed argv, `shell=False`, bounded timeout, approved
workdir) and reads only a bounded verdict JSON — never a raw transcript.

Tools:

- `hermes_swarm_workflow_validate(workflow_json)` — read-only, pure. Validates
  the DAG (schema, cycles, owners, caps, per-stage contracts) and returns the
  stage plan.
- `hermes_swarm_workflow_create(workflow_json, confirm, dry_run)` — workspace,
  dry-run-first. Registers a workflow instance; returns `workflow_id` + stage
  plan. Direct requires `confirm=true`.
- `hermes_swarm_workflow_list()` — read-only. Instances + status (running /
  blocked / done / awaiting_approval).
- `hermes_swarm_workflow_status(workflow_id)` — read-only. One workflow's stage
  map, owners, verdicts, handoffs, observed runs, approval record.
- `hermes_swarm_stage_dispatch(workflow_id, stage_id, confirm, dry_run)` —
  workspace, dry-run-first. Dispatches one ready stage (parents done) as an M1
  contract; respects per-workflow and per-board concurrency caps.
- `hermes_swarm_stage_advance(workflow_id, stage_id, confirm, dry_run)` —
  workspace, dry-run-first. Runs the M1 validator against observed state;
  records the handoff (`from` / `to` / `artifact_refs` / `contract_verdict`);
  promotes next ready stages. Failed validation → `returned_for_rework` once,
  then `blocked`.
- `hermes_swarm_approve(workflow_id, confirm, dry_run)` — **owner** level,
  direct. Records the final human approval; the workflow never auto-advances
  past this gate.

Caps (per workflow, env-overridable `HERMES_GPT_SWARM_MAX_PARALLEL`,
`HERMES_GPT_SWARM_BOARD_CAP`, `HERMES_GPT_SWARM_MAX_STAGES`): default 3
concurrent stages per workflow, 4 per board, 12 stages per workflow. Workflow
state is stored as operational JSON under the Hermes data root
(`swarm-workflows/`), never surfaced raw; status surfaces are bounded and
redacted via the Mission Control envelope. Every call is audited with
`workflow_id` / `stage` / `owner` / `verdict`; no objective text is logged.
Worktrees and Codex verdict/transcript artifacts persist for review; `default`
cleans them after the release gate (see the retention note on every workflow).

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
