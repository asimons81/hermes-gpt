# Operator Mode for Hermes GPT

Operator Mode is the policy-gated control plane for trusted MCP clients such as ChatGPT. This document describes the current v0.6.0 behavior.

For documentation authority and historical-artifact rules, see [docs/README.md](README.md).

## Core rule

**Tool visibility is not authority.** A tool can be present in the MCP schema and still refuse to mutate because the required policy gates are not satisfied.

Hermes GPT is designed to run on the user's machine, bound to loopback. Remote clients require a deliberately configured private or authenticated boundary in front of that loopback service. Public unauthenticated Operator hosting is unsupported.

Operator Mode is defense-in-depth, not an OS sandbox. Use OS-level isolation for untrusted input.

## Authority model

Operator authority is determined by four things:

1. `HERMES_GPT_OPERATOR_ENABLED`
2. `HERMES_GPT_OPERATOR_LEVEL`
3. `HERMES_GPT_OPERATOR_APPLY_MODE`
4. the individual tool call's mutation gates, normally `dry_run` and sometimes `confirm`

Owner operations add two more gates:

```text
HERMES_GPT_OWNER_ACTIVE=1
HERMES_GPT_OWNER_ACK=I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE
```

### Operator levels

Higher levels include the capabilities of lower levels.

| Level | What it unlocks |
| --- | --- |
| `read_only` | status, policy, audit, list/view/diff operations, Mission Control |
| `cron` | cron run, pause, copy, move |
| `skills` | skill create, edit, patch, write, copy, sync, delete |
| `skills_config` | non-secret config and environment writes |
| `workspace` | scoped workspace read/write/test, gateway restart, Codex jobs, contract/swarm dispatch |
| `owner` | break-glass raw command/file operations and final swarm approval; secret paths remain denied |

`skills_config` is a reasonable ceiling for routine configuration work. Use `workspace` only when a task requires scoped workspace operations or delegated execution. Treat `owner` as break-glass.

## Four operating postures

### 1. Read-only default

No Operator environment variables are required.

```powershell
hermes-gpt
```

Expected behavior:

- read/status/list tools work where enabled;
- mutating Operator tools refuse;
- Mission Control remains read-only.

### 2. Dry-run Operator Mode

Recommended for an always-on trusted connector.

```powershell
$env:HERMES_HOME="C:\Users\<YOU>\AppData\Local\hermes"
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="skills_config"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="dry_run"
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES="default"

python server.py --http --host 127.0.0.1 --port 4750
```

Mutating tools return plans or previews but do not apply changes.

### 3. Direct Operator Mode

Use only for a deliberate maintenance session.

```powershell
$env:HERMES_HOME="C:\Users\<YOU>\AppData\Local\hermes"
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="workspace"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="direct"
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES="default"
$env:HERMES_GPT_OPERATOR_ALLOWED_PATHS="C:\path\to\approved-workspace"
```

Direct mode only permits mutation. It does not force mutation. A mutating call still needs `dry_run=false`, and tools with an explicit confirmation gate also need `confirm=true`.

### 4. Owner Mode

Break-glass only.

```powershell
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="owner"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="direct"
$env:HERMES_GPT_OWNER_ACTIVE="1"
$env:HERMES_GPT_OWNER_ACK="I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE"
```

Configured `owner` authority is clamped unless the Owner activation and exact acknowledgement are present. Owner Mode still cannot access denied secret paths.

Do not use Owner Mode for a public, shared, or always-on connector.

## Dry-run and confirmation semantics

For a normal mutating Operator tool, actual mutation requires:

```text
HERMES_GPT_OPERATOR_APPLY_MODE=direct
```

plus:

```json
{
  "dry_run": false
}
```

Tools that create or dispatch external work also require `confirm=true` when their schema includes that gate.

A refusal caused by a missing gate is expected behavior, not a failure to execute the request.

## Mission Control

Mission Control is the read-only `hermes_mission_*` operational view of the Hermes deployment.

Surfaces:

- `overview`
- `health`
- `profiles`
- `fleet`
- `codex`
- `cron`
- `delegations`
- `failures`
- `approvals`
- `vault`
- `usage`
- `audit`

Mission Control is structurally read-only:

- SQLite sources are opened read-only;
- no Mission tool accepts write/apply arguments;
- no mutating shell action is performed;
- every Mission call is audited as unchanged/read-only;
- missing optional sources return a bounded unavailable reason instead of crashing the whole view.

### Data boundary

Mission Control never returns raw message, memory, transcript, request-dump, credential, token, vault-secret, or profile-secret bodies.

Prompt-like text is represented by bounded metadata such as `prompt_len` and `prompt_sha256`. Free-text operational fields such as failures, audit summaries, cron names, and delegation summaries receive conservative secret and PII stripping before they leave the host.

### Surface allowlist

`HERMES_GPT_MISSION_ALLOWED_SURFACES` is **restrictive when configured**.

- **unset:** all read-only Mission Control surfaces are available;
- **comma-separated list:** only listed valid surfaces are available;
- **empty value:** every Mission Control surface is denied.

Do not describe the unset state as deny-by-default. The implementation deliberately makes all read-only Mission surfaces available when the variable is absent.

Mission Control requires only `read_only` authority and never needs direct apply mode.

## Work Contracts

Work Contracts add a structured, verifiable work-order layer through `hermes_contract_*`.

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_contract_define(contract_json)` | read-only | Validate and canonicalize a contract. |
| `hermes_contract_dispatch(contract_json, confirm, dry_run)` | workspace | Dispatch a validated contract through the existing fleet authority model. |
| `hermes_contract_validate(contract_json)` | read-only by default | Validate completion from observed evidence. |
| `hermes_contract_status(contract_json)` | read-only | Link the contract to bounded observed run/delegation state. |

### Validation model

A worker's claim that work is complete is never proof by itself. Validation inspects observed state such as runs, artifacts, tests, audit evidence, and required review evidence.

Verdicts are bounded to:

- `SATISFIED`
- `NOT_SATISFIED`
- `INCONCLUSIVE`
- `INVALID_CONTRACT`

Missing evidence fails closed. A valid contract with no observed run cannot become `SATISFIED`.

Retry selection is deterministic. Forbidden-action audit evidence is scoped to the contract's task identity so unrelated concurrent work does not contaminate the verdict.

### Review limitation in v0.6.0

v0.6.0 has no production review-accept writer. If a contract requires review, the necessary evidence must already exist through an authorized external reviewer/audit path or human approval reference. If it does not exist, validation returns `NOT_SATISFIED`.

**v0.7 closes this gap**: `hermes_review_accept` (owner-gated, distinct
reviewer enforced) writes review-acceptance records that the validator reads
as evidence. See [Flight Deck (v0.7)](#flight-deck-v07).

Required test checks execute only through the workspace test allowlist and inherit the workspace/direct policy gates.

## Swarm Orchestration

Swarm Orchestration is the `hermes_swarm_*` DAG workflow layer built on Work Contracts.

Canonical workflow shape:

```text
research -> architecture -> implementation/tests/docs
         -> integration review -> Codex review
         -> acceptance validation -> HUMAN APPROVAL
```

Tools:

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_swarm_workflow_validate(workflow_json)` | read-only | Validate schema, owners, caps, contracts, dependencies, and cycles. |
| `hermes_swarm_workflow_create(workflow_json, confirm, dry_run)` | workspace | Register a workflow instance. |
| `hermes_swarm_workflow_list()` | read-only | List bounded workflow status. |
| `hermes_swarm_workflow_status(workflow_id)` | read-only | Return one bounded stage/owner/verdict/handoff view. |
| `hermes_swarm_stage_dispatch(workflow_id, stage_id, confirm, dry_run)` | workspace | Dispatch one ready stage as a Work Contract. |
| `hermes_swarm_stage_advance(workflow_id, stage_id, confirm, dry_run)` | workspace | Validate observed completion, record handoff, and promote ready children. |
| `hermes_swarm_approve(workflow_id, confirm, dry_run)` | owner | Record the final human approval. |

Default caps, unless explicitly overridden by the supported environment variables:

- 3 concurrent stages per workflow;
- 4 concurrent stages per board;
- 12 stages per workflow.

Failed validation can return a stage for one bounded rework retry. A second failure blocks the stage for human attention.

Codex may provide a bounded review verdict, but Codex is never an implementation owner. Final workflow approval is human and Owner-gated.

## Flight Deck (v0.7)

Flight Deck adds four coordinated v0.7 capabilities on top of the v0.6
control plane: production review evidence, structured event history, durable
encrypted token storage, and restart reconciliation. All new surfaces are
additive; no existing tool name, schema, or authority class changes.

### Review evidence (`hermes_review_accept`)

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_review_accept(contract_sha256, task_id, assignee, reviewer, verdict, evidence_refs, approval_reference, dry_run, confirm)` | **owner** + direct + confirm | Write a review-acceptance record for a Work Contract. Distinct reviewer is enforced at write time (`reviewer != assignee`); verdicts are bounded to `SATISFIED` / `NOT_SATISFIED`; evidence is referenced, never copied. |

The Work Contract validator reads the review-evidence store as an additional
evidence source while keeping the v0.6 audit + human-approval paths. A
`SATISFIED` acceptance by a reviewer distinct from the assignee satisfies the
review check; a self-review record never does.

### Structured event history (`hermes_events_*`)

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_events_query(source, subject_id, kind, since, until, limit)` | read_only + allowlist | Query the normalized, redacted event timeline (audit / swarm / codex / cron / kanban). |
| `hermes_events_tail(limit)` | read_only + allowlist | Recent events across all allowed sources. |

The event surface is a derived read-model over existing durable stores; it
never writes. Allowlist: `HERMES_GPT_EVENTS_ALLOWED_SOURCES` (unset = all
read-only sources; list = only listed; empty = none). Retention:
`HERMES_GPT_EVENTS_MAX_AGE_DAYS` (default 90). Redaction invariants match
Mission Control; prompts appear only as length/sha when present in the source.

### Durable token storage (`hermes_oauth_*`)

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_oauth_status()` | read_only | Durable token store presence/expiry only; never exposes token material. |
| `hermes_oauth_revoke(confirm, dry_run, rotate_key)` | **owner** + direct + confirm (pending legal scope decision) | Delete the encrypted token envelope; optionally rotate the master key. |

OAuth access/refresh tokens are persisted through `token_store` (AES-256-GCM
envelope at `<hermes_data>/secrets/hermes_gpt_tokens.json`, 0600; keyring →
key file → env key precedence) so a server restart does not invalidate
credentials. No token material is ever written to the audit log or any MCP
response. The `secrets/` directory is a denied path for all tools.

### Restart reconciliation (`hermes_swarm_reconcile`)

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_swarm_reconcile(apply)` | workspace/owner + direct (dry-run first) | Mark swarm stages stuck in `running` as `blocked` with `reason: interrupted_by_restart`; reload the durable token envelope; report a bounded summary. |

Reconciliation is fail-closed: it never auto-advances or auto-dispatches
work. The operator explicitly re-advances through the existing gated
`hermes_swarm_stage_advance`, which is idempotent for already-validated or
done stages (a re-advance returns current state as a no-op).

### v0.7 surface manifest

| Tool | Authority class | Gates | Audit | Allowlist |
| --- | --- | --- | --- | --- |
| `hermes_review_accept` | owner | direct + confirm | every call | n/a |
| `hermes_events_query` | read_only | allowlist | every call | `HERMES_GPT_EVENTS_ALLOWED_SOURCES` |
| `hermes_events_tail` | read_only | allowlist | every call | `HERMES_GPT_EVENTS_ALLOWED_SOURCES` |
| `hermes_oauth_status` | read_only | none | every call | n/a |
| `hermes_oauth_revoke` | owner | direct + confirm (pending legal) | every call | n/a |
| `hermes_swarm_reconcile` | workspace/owner | dry-run-first + apply | every call | n/a |

## Fleet routing through the local A2A registry

Fleet routing uses only peers already present in the authenticated local Hermes A2A registry.

| Tool | Authority | Behavior |
| --- | --- | --- |
| `hermes_fleet_list` | read-only | List named peers without exposing tokens or peer URLs. |
| `hermes_fleet_status` | read-only | Return bounded compatibility metadata for one peer. |
| `hermes_fleet_dispatch` | workspace + direct + confirm | Submit bounded work to one named peer. |
| `hermes_fleet_task` | read-only | Return bounded task state. |
| `hermes_fleet_dispatch_work_order` | workspace + direct + confirm | Validate and submit a structured profile-aware work order. |
| `hermes_fleet_result` | read-only | Return a validated safe completion bundle. |
| `hermes_fleet_authority_drift` | read-only | Report registry/manifest/profile/role/Agent Card drift. |

MCP callers cannot supply an arbitrary peer endpoint, bearer token, SSH command, or executable.

### Fleet authority manifest

Set `HERMES_GPT_FLEET_AUTHORITY_MANIFEST` to an absolute JSON path or use the default location under the Hermes data root:

```text
<Hermes data root>/config/fleet-authority.json
```

Start from `examples/fleet-authority.example.json`.

The manifest defines expected peer identity/role/profile authority. It must not contain URLs, credentials, or tokens. Before direct structured dispatch, Hermes GPT rechecks live peer identity and role against this server-controlled manifest. A mismatch stops dispatch.

High-impact structured work additionally requires bounded approval metadata. Public-action detection and role authority are enforced locally before work is sent.

## Codex CLI jobs through the Operator server

This is different from installing Hermes GPT as an MCP server inside Codex.

The normal Operator server exposes asynchronous delegated Codex tools:

- `hermes_codex_status`
- `hermes_codex_plan`
- `hermes_codex_start`
- `hermes_codex_review_start`
- `hermes_codex_jobs`
- `hermes_codex_job_status`
- `hermes_codex_job_result`
- `hermes_codex_cancel`

Real execution requires:

- Operator Mode at `workspace` or acknowledged `owner` level;
- an allowed work directory;
- `HERMES_GPT_ENABLE_CODEX_RUNNER=1`;
- direct apply mode;
- `confirm=true`;
- `dry_run=false`.

`HERMES_GPT_ALLOW_CODEX_WRITE=1` is required only for `workspace-write`. Read-only Codex jobs do not need it.

`HERMES_GPT_CODEX_EXE` can pin an absolute standalone Codex CLI executable. Hermes GPT rejects protected WindowsApps shims and executables that fail a version probe.

For Codex acting as an MCP client, use [docs/codex.md](codex.md). For the Windows ChatGPT -> Hermes GPT -> Codex CLI deployment, use [windows-chatgpt-codex.md](windows-chatgpt-codex.md).

## Audit behavior

Preferred audit path on Windows:

```text
%USERPROFILE%\AppData\Local\hermes\logs\hermes_gpt_operator_audit.jsonl
```

Fallback:

```text
<hermes-gpt>\logs\hermes_gpt_operator_audit.jsonl
```

Audit records contain bounded operational metadata such as tool, level, apply mode, dry-run state, changed/success state, relevant IDs, and length/hash metadata for content-bearing operations.

Audit records do not intentionally persist raw prompts, `.env` values, vault contents, credentials, or full secret-bearing config values.

## Diagnostics and recovery

### `hermes_operator_doctor`

Read-only deep health check across the Operator surface. Checks include gateway state, config/env readability, cron/skills, policy, audit readability, and connector capability.

Status vocabulary:

- `PASS`
- `WARN`
- `FAIL`
- `UNSUPPORTED`

### `hermes_operator_snapshot`

Returns one bounded current-state summary with a recommended next action.

### `hermes_operator_recover`

Conservative recovery planner. Dry-run is the default. Use `apply=false` first.

Actual recovery mutation requires `apply=true` plus the normal direct/workspace policy gates.

### `hermes_release_doctor`

Use before preparing a release. It checks repository state, secret-file hygiene, package version/docs consistency, import/compile health, and that the server is not left in direct mode. `full_tests=true` also runs the test suite.

Release statuses are `PASS`, `WARN`, or `BLOCKED`.

## Profile and path normalization

Typical Hermes data roots:

- Windows: `C:\Users\<YOU>\AppData\Local\hermes`
- Unix/macOS: `~/.hermes`

If `HERMES_HOME` points at a named profile or the Hermes Agent source directory, Operator profile operations normalize back to the Hermes data root. The default profile maps to that root; named profiles map under `profiles/<profile-name>`.

`HERMES_GPT_OPERATOR_ALLOWED_PATHS` should contain only the workspaces the Operator server is expected to touch.

## Remote access posture

Keep the MCP server bound to `127.0.0.1`.

For a remote client:

1. keep Hermes GPT on loopback;
2. put an authenticated/private boundary in front of it;
3. use `dry_run` for routine always-on access;
4. switch to `direct` only for a deliberate maintenance session;
5. switch back after the session;
6. never leave Owner Mode enabled on an always-on connector.

Operator Mode itself does not add public-network authentication.

## Secret-path policy

The server refuses or redacts access to secret-looking locations even at high authority, including categories such as:

- `.env` files;
- auth/token/cookie stores;
- vault secrets;
- SSH keys;
- AWS credential paths;
- MCP token files;
- other secret-looking filenames.

Owner Mode does not disable this policy.

## Troubleshooting

### Connector shows an old or incomplete tool list

- Verify the running server process is the one you expect.
- Inspect the MCP tool list directly.
- Reconnect or recreate the client connector if it cached an older schema.
- Do not rely on a fixed expected tool count; the count changes as gated surfaces evolve.

### A profile appears missing

- Check `HERMES_HOME`.
- Confirm root normalization.
- Remember that `default` resolves to the data root while named profiles live under `profiles/<profile-name>`.

### A mutating tool refuses

Check the exact tool's requirements plus:

- `HERMES_GPT_OPERATOR_ENABLED`
- `HERMES_GPT_OPERATOR_LEVEL`
- `HERMES_GPT_OPERATOR_APPLY_MODE`
- `dry_run`
- `confirm` when the tool defines that gate

A policy refusal is often the correct result.

### Owner tools refuse

Verify both Owner activation and the exact acknowledgement string:

```powershell
$env:HERMES_GPT_OWNER_ACTIVE="1"
$env:HERMES_GPT_OWNER_ACK="I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE"
```

## Related docs

- [Documentation map](README.md)
- [Codex integration](codex.md)
- [Windows ChatGPT -> Codex](windows-chatgpt-codex.md)
- [Updating](updating.md)
- [Retention policy](retention-policy.md)
- [v0.6.0 release notes](release-notes-v0.6.0.md)
