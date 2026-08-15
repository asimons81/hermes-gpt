# hermes-gpt

[![PyPI version](https://img.shields.io/pypi/v/hermes-gpt.svg)](https://pypi.org/project/hermes-gpt/)
[![PyPI downloads](https://img.shields.io/pypi/dm/hermes-gpt.svg)](https://pypi.org/project/hermes-gpt/)

![Hermes GPT v0.6.0 - ChatGPT for Hermes Agent](assets/hermes-gpt-v0.6.0-readme-hero.jpg)

`hermes-gpt` is a local-first MCP sidecar for Hermes Agent. It exposes selected Hermes capabilities to trusted MCP clients without modifying Hermes Agent source files.

## Current status

- **Repository version:** 0.7.0 (local checkout, not yet released)
- **Latest GitHub release:** v0.6.0
- **Latest PyPI release:** 0.6.0
- **Python requirement:** 3.10+
- **Deployment posture:** local-dev / trusted-machine only
- **Remote public hosting:** unsupported without a real authenticated private boundary

> [!IMPORTANT]
> GitHub releases and PyPI can temporarily be on different versions. The PyPI badge above is the source of truth for what `pip install hermes-gpt` installs. Do not assume a PyPI install contains v0.7 features unless the badge reports v0.7.0 or newer.

For the current documentation map and source-of-truth rules, start with [docs/README.md](docs/README.md). Agents working in this repository should also read [AGENTS.md](AGENTS.md).

## What v0.7.0 adds

v0.7.0 "Flight Deck" adds four coordinated capabilities on top of the v0.6 control plane:

1. **Production review evidence** - `hermes_review_accept`, an owner-gated writer with distinct-reviewer enforcement, feeding `hermes_contract_validate`.
2. **Structured event history** - `hermes_events_query` / `hermes_events_tail`, a read-only redacted timeline over audit/swarm/codex/cron/kanban.
3. **Durable encrypted token storage** - OAuth credentials survive restarts (AES-256-GCM envelope) with `hermes_oauth_status` / `hermes_oauth_revoke`.
4. **Restart reconciliation** - `hermes_swarm_reconcile` marks interrupted swarm stages blocked (never auto-advances); stage advance is idempotent.

Plus the MCP compatibility manifest, cross-machine seam interfaces (stretch,
interfaces only), and a CI hermeticity fix. See the [v0.7.0 release notes](docs/release-notes-v0.7.0.md), the [MCP compatibility manifest](docs/mcp-compatibility.md), and [retention policy](docs/retention-policy.md).

## What v0.6.0 adds

v0.6.0 adds three coordinated control-plane layers on top of the existing Operator and Codex integrations:

1. **Mission Control** - bounded, audited, read-only operational views through `hermes_mission_*`.
2. **Work Contracts** - declarative `hermes_contract_*` work orders whose completion is validated from observed state rather than worker self-report.
3. **Swarm Orchestration** - bounded `hermes_swarm_*` DAG workflows with explicit ownership, capped concurrency, fail-closed validation, review gates, and final human approval.

See the [v0.6.0 release notes](docs/release-notes-v0.6.0.md) and [retention policy](docs/retention-policy.md).

## Choose the path you need

| Goal | Start here |
| --- | --- |
| Understand the repository and current docs | [Documentation map](docs/README.md) |
| Run Hermes GPT locally | [Local quickstart](#local-quickstart) |
| Authenticate a remote MCP connector | [OAuth and bearer authentication](docs/oauth.md) |
| Verify the MCP protocol surface | [MCP compatibility manifest](docs/mcp-compatibility.md) |
| Use Codex as an MCP client | [Codex guide](docs/codex.md) |
| Use ChatGPT or another trusted client to operate Hermes | [Operator Mode](docs/operator-mode.md) |
| Let ChatGPT dispatch bounded work to the Codex CLI on Windows | [Windows ChatGPT -> Codex guide](docs/windows-chatgpt-codex.md) |
| Update an install safely | [Updating](docs/updating.md) |
| Review v0.6 data cleanup rules | [Retention policy](docs/retention-policy.md) |
| Understand historical implementation decisions | [Design and release artifacts](docs/README.md#historical-and-internal-artifacts) |

## Local quickstart

### Install from PyPI

```bash
python -m pip install hermes-gpt
```

Check the PyPI badge before relying on version-specific features.

### Run the current source checkout

```bash
git clone https://github.com/asimons81/hermes-gpt.git
cd hermes-gpt
python -m pip install .
hermes-gpt
```

The final v0.6.0 wheel and sdist are also attached to the [GitHub v0.6.0 release](https://github.com/asimons81/hermes-gpt/releases/tag/v0.6.0). The v0.7.0 release notes cover the Flight Deck surfaces (`hermes_review_accept`, `hermes_events_*`, `hermes_oauth_*`, `hermes_swarm_reconcile`); operator diagnostics and recovery tools (`hermes_operator_doctor`, `hermes_operator_snapshot`, `hermes_release_doctor`, `hermes_operator_recover`) are documented in [docs/operator-mode.md](docs/operator-mode.md).

## Default local MCP surface

With no optional feature gates enabled, the server exposes a small read-oriented surface:

- `hermes_read_file(path, offset=1, limit=500)`
- `hermes_search_files(pattern, target="content", path=".", file_glob=None, limit=50)`
- `hermes_memory(action="search", target="memory", content=None, old_text=None)`
- `hermes_skill_list()`
- `hermes_skill_view(name)`

Optional legacy feature gates remain available for compatibility:

| Capability | Gate | Default |
| --- | --- | --- |
| File write / patch | `HERMES_GPT_ENABLE_WRITE=1` | hidden |
| Memory mutation | `HERMES_GPT_ENABLE_MEMORY_WRITE=1` | disabled |
| Session search | `HERMES_GPT_ENABLE_SESSION_SEARCH=1` | hidden |
| Terminal execution | `HERMES_GPT_ENABLE_TERMINAL=1` | hidden |
| Vision | `HERMES_GPT_ENABLE_VISION=1` | hidden |
| Web search / extraction | `HERMES_GPT_ENABLE_WEB=1` | hidden |

For new automation and maintenance work, prefer Operator Mode instead of enabling broad legacy write gates.

## Run modes

### Stdio

For a local MCP client that can launch a subprocess:

```bash
hermes-gpt
```

or from a checkout:

```bash
python server.py
```

### Local streamable HTTP

```bash
python server.py --http --host 127.0.0.1 --port 7677
```

Endpoint:

```text
http://127.0.0.1:7677/mcp
```

Keep the server on loopback. A remote client such as ChatGPT cannot use your machine's `127.0.0.1` directly, so remote access requires a deliberately configured private/authenticated boundary. Do not publish an unauthenticated Operator endpoint to the internet.

Hermes GPT can enforce either a strong static bearer token or a built-in,
single-confidential-client OAuth authorization-code flow with rotating refresh
tokens. OAuth credentials are memory-backed and fail closed on missing client
authentication, unsupported scope/resource, expiry, or replay. See
[OAuth and bearer authentication](docs/oauth.md) before enabling the `remote`
profile; authentication does not activate Operator mutation or Owner Mode.

## Operator Mode

Operator Mode is the policy-gated control plane for trusted clients. Tool visibility does not grant mutation authority.

| Level | Adds |
| --- | --- |
| `read_only` | status, policy, audit, list/view/diff, Mission Control |
| `cron` | cron run/pause/copy/move |
| `skills` | skill create/edit/patch/write/copy/sync/delete |
| `skills_config` | non-secret config and environment writes |
| `workspace` | scoped workspace writes/tests, gateway restart, Codex jobs, contract/swarm dispatch |
| `owner` | break-glass raw command/file operations and final swarm approval; secret paths remain denied |

Mutation requires both the server and the individual call to opt in:

```text
HERMES_GPT_OPERATOR_ENABLED=1
HERMES_GPT_OPERATOR_APPLY_MODE=direct
```

and the mutating call must use `dry_run=false`. Tools that require explicit confirmation also require `confirm=true`.

Owner Mode additionally requires:

```text
HERMES_GPT_OWNER_ACTIVE=1
HERMES_GPT_OWNER_ACK=I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE
```

See [docs/operator-mode.md](docs/operator-mode.md) for the complete policy model and exact gates.

## Mission Control

Mission Control is structurally read-only. It exposes bounded operational summaries for:

`overview`, `health`, `profiles`, `fleet`, `codex`, `cron`, `delegations`, `failures`, `approvals`, `vault`, `usage`, and `audit`.

Important authorization semantics for `HERMES_GPT_MISSION_ALLOWED_SURFACES`:

- **unset:** all read-only Mission Control surfaces are available;
- **set to a comma-separated list:** only listed valid surfaces are available;
- **set to an empty value:** all Mission Control surfaces are denied.

Mission Control excludes raw message, memory, transcript, request-dump, credential, token, and profile-secret bodies. Prompt-like content is surfaced only as bounded metadata such as length and SHA-256. Free-text operational fields receive conservative redaction / PII stripping before they leave the host.

## Work Contracts

The `hermes_contract_*` family makes completion verifiable instead of trusting a worker's `done` claim.

- `hermes_contract_define` validates and canonicalizes a contract.
- `hermes_contract_dispatch` is workspace-level and dry-run-first.
- `hermes_contract_validate` checks observed runs, artifacts, audit evidence, tests, and review evidence.
- `hermes_contract_status` links a contract to bounded observed state.

Validation is fail-closed. Missing evidence cannot become `SATISFIED`. v0.6.0 does not ship a production review-accept writer, so required review evidence must already exist through an authorized external review path or human approval reference.

## Swarm Orchestration

The `hermes_swarm_*` family runs bounded DAG workflows on top of Work Contracts.

Typical shape:

```text
research -> architecture -> implementation/tests/docs
         -> integration review -> Codex review
         -> acceptance validation -> HUMAN APPROVAL
```

Key properties:

- explicit stage ownership;
- validated dependencies and cycle rejection;
- default caps of 3 concurrent stages per workflow, 4 per board, and 12 stages per workflow;
- one bounded rework retry before blocking for human attention;
- Codex can review but is never an implementation owner;
- final approval is an Owner-level human gate.

## Codex integration

Hermes GPT supports two different Codex relationships. Keep them conceptually separate:

1. **Codex as MCP client** - install the curated Hermes GPT MCP toolset into Codex. See [docs/codex.md](docs/codex.md).
2. **Codex CLI as delegated worker/reviewer** - a trusted Hermes GPT client can start bounded async Codex jobs through `hermes_codex_*`. This requires Operator `workspace` level, an approved work directory, `HERMES_GPT_ENABLE_CODEX_RUNNER=1`, direct mode for execution, `confirm=true`, and `dry_run=false`.

`HERMES_GPT_ALLOW_CODEX_WRITE=1` is required only for `workspace-write` jobs.

### Tool-name note

The main Hermes GPT server and the curated Codex MCP server have one intentional naming difference:

- main server web extraction: `hermes_web_extract`
- Codex-focused MCP extraction: `hermes_extract_page`

Do not silently substitute one name for the other when generating tool calls.

## Fleet routing

When Hermes already has authenticated peers in its local A2A registry, Hermes GPT can route bounded work to named peers through `hermes_fleet_*`.

Callers cannot provide arbitrary peer URLs or bearer tokens. Real dispatch remains constrained by Operator level, direct mode, confirmation, the local registry, and the server-controlled fleet authority manifest. See [Operator Mode](docs/operator-mode.md#fleet-routing-through-the-local-a2a-registry).

## Security invariants

These rules are part of the product contract, not optional recommendations:

- loopback is the default network boundary;
- public unauthenticated hosting is unsupported;
- Operator Mode is not a sandbox;
- mutations are off by default and dry-run-first when enabled;
- secret-looking paths such as `.env`, `auth.json`, token stores, `.ssh`, `.aws`, and vault secrets remain denied;
- subprocesses use fixed argv and `shell=False` on protected execution paths;
- raw prompts are not written into Operator audit records;
- Mission Control never exposes raw messages, memory bodies, transcripts, request dumps, or credentials;
- Owner Mode does not disable secret-path protections.

Use OS-level isolation for untrusted input.

## Updating

Updates are check-first:

```bash
hermes-gpt update
```

Apply only after reviewing the result:

```bash
hermes-gpt update --apply
```

Git checkout updates require a clean checkout on the default branch and use fast-forward-only behavior. Installed-package updates use pip only when a newer package version is available. See [docs/updating.md](docs/updating.md).

## Documentation

Current operational documentation:

- [Documentation map and source-of-truth rules](docs/README.md)
- [OAuth and bearer authentication](docs/oauth.md)
- [Operator Mode](docs/operator-mode.md)
- [Codex integration](docs/codex.md)
- [Windows ChatGPT -> Codex deployment](docs/windows-chatgpt-codex.md)
- [Updating](docs/updating.md)
- [Retention policy](docs/retention-policy.md)
- [v0.6.0 release notes](docs/release-notes-v0.6.0.md)
- [Changelog](CHANGELOG.md)

Historical release notes and pre-release design / risk / planning artifacts remain in the repository for provenance. They are not authoritative instructions for current runtime behavior. See [docs/README.md](docs/README.md) before using them as implementation guidance.

## Development and verification

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
python tools/check_package_hygiene.py dist/*
```

Release-specific checks are listed in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).

## License

MIT. See [LICENSE](LICENSE).
