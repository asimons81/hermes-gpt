# Hermes GPT and Codex

Hermes GPT supports **two different Codex workflows**. Keep them separate when configuring systems or generating tool calls.

1. **Codex as an MCP client**: Codex loads a curated Hermes GPT MCP server and calls Hermes tools.
2. **Codex CLI as a delegated worker/reviewer**: ChatGPT or another trusted client calls the normal Hermes GPT Operator server, which launches bounded async Codex CLI jobs through `hermes_codex_*`.

The first workflow uses the Codex/MCP feature gates. The second uses Operator `workspace` authority plus the Codex runner gate.

For documentation authority rules, see [docs/README.md](README.md).

## Workflow A: Codex as an MCP client

### Install the MCP entry

Set the base gates in the environment that launches Codex:

```powershell
$env:HERMES_GPT_ENABLE_CODEX="1"
$env:HERMES_GPT_ENABLE_MCP="1"
```

Install the core toolset:

```powershell
hermes-gpt codex install --toolset core
hermes-gpt codex doctor
```

`install` prefers `codex mcp add hermes-gpt -- ...` when the Codex CLI supports it. If Hermes GPT must edit TOML directly, it validates the config, creates a timestamped backup, preserves unrelated settings, and changes only the Hermes GPT MCP entry. The operation is idempotent.

For the opt-in Operator alias toolset:

```powershell
hermes-gpt codex install --toolset operator --refresh
hermes-gpt codex doctor
```

Changing an existing toolset requires `--refresh`, which backs up and replaces only the Hermes GPT entry. Existing entries with no toolset metadata are treated as `core`.

### Repository-local configuration

Run from the target Git repository:

```powershell
hermes-gpt codex install --project
```

This creates or updates:

```text
<git-root>/.codex/config.toml
```

### Verify or remove

```powershell
hermes-gpt codex print-config
hermes-gpt codex doctor
hermes-gpt codex uninstall
```

`doctor` is read-only. `uninstall` removes only Hermes GPT MCP configuration and creates a backup first.

### Core Codex MCP tools

The curated Codex MCP server registers these names:

| Tool | Behavior |
| --- | --- |
| `hermes_status` | Local Hermes GPT / gateway status. |
| `hermes_capabilities` | Enabled features and missing gates. |
| `hermes_plan` | Compact read-only repository context and implementation plan. |
| `hermes_vision_analyze` | Analyze an approved local raster image. |
| `hermes_web_search` | Search the web without fetching result pages. |
| `hermes_extract_page` | Extract readable public HTTP(S) content with network safety checks. |
| `hermes_cron_plan` | Produce a dry-run cron proposal. |
| `hermes_cron_create` | Create a cron job only through explicit write/confirmation gates. |
| `hermes_author_skill` | Draft a skill; writes require explicit gates. |
| `hermes_gateway_diagnostics` | Read-only gateway diagnostics. |

The `operator` toolset adds curated namespaced aliases for Operator diagnostics, audit, cron, skills, non-secret config/environment, and gateway operations. It intentionally excludes broad workspace, raw git/command, and Owner file-write surfaces.

### Optional session-history integration

Session history is intentionally separate from the curated `core` and `operator` toolsets. The separately installed **Hermes GPT Session History** integration exposes native read-only `hermes_session_list`, `hermes_session_search`, `hermes_session_read`, and `hermes_session_export` tools to Codex while the backing Hermes GPT server still requires `HERMES_GPT_ENABLE_SESSION_SEARCH=1`. These tools do not create files. Internal `system`, `tool`, and `function` content additionally requires `HERMES_GPT_ENABLE_SESSION_INTERNAL_CONTENT=1`; keep those roles and lineage disabled during routine inspection. See [session history](session-history.md).

### Tool-name namespace warning

The curated Codex MCP server is not identical to the main Hermes GPT server.

Important example:

```text
main Hermes GPT server:  hermes_web_extract
Codex-focused MCP:       hermes_extract_page
```

Agents must verify which MCP server/toolset is active before generating calls. Do not substitute a familiar tool name from another surface.

### Feature gates for the core toolset

Base gates:

```text
HERMES_GPT_ENABLE_CODEX=1
HERMES_GPT_ENABLE_MCP=1
```

Optional capabilities use gates such as:

```text
HERMES_GPT_ENABLE_VISION=1
HERMES_GPT_ENABLE_WEB=1
HERMES_GPT_ENABLE_CRON=1
HERMES_GPT_ENABLE_DIAGNOSTICS=1
```

Persistent core-tool writes are dry-run-first. Skill or cron writes require the appropriate write gates in addition to the feature gates and any applicable Operator policy.

### Core-tool safety behavior

- Local image paths resolve symlinks and must stay within an approved project root / allowed root.
- Secret-looking paths are rejected.
- Web extraction accepts public HTTP(S) URLs by default and rejects file URLs, localhost, private/loopback/link-local/reserved addresses, and metadata targets unless a specific private-network override is deliberately enabled.
- Returned structured text is recursively redacted for common secret/token/cookie/private-key patterns.
- The MCP server launches even when optional gates are absent so tools can return an actionable blocked response rather than disappearing silently.

## Workflow B: Codex CLI as a delegated worker or reviewer

This workflow uses the **normal Hermes GPT Operator server**, not the curated Codex-as-client MCP server.

Tools:

- `hermes_codex_status`
- `hermes_codex_plan`
- `hermes_codex_start`
- `hermes_codex_review_start`
- `hermes_codex_jobs`
- `hermes_codex_job_status`
- `hermes_codex_job_result`
- `hermes_codex_cancel`

### Required authority for real execution

A real job requires:

- Operator Mode enabled;
- Operator level `workspace` or acknowledged `owner`;
- the work directory allowed by Operator policy;
- `HERMES_GPT_ENABLE_CODEX_RUNNER=1`;
- `HERMES_GPT_OPERATOR_APPLY_MODE=direct`;
- `confirm=true`;
- `dry_run=false`.

For `sandbox=workspace-write`, also set:

```text
HERMES_GPT_ALLOW_CODEX_WRITE=1
```

Read-only jobs do not need the write gate.

Delegated jobs default to `execution_mode="normal"`. An explicit job-scoped `execution_mode="nolo"` adds Codex's top-level `-a never` approval policy while preserving the requested `read-only` or `workspace-write` sandbox. `workspace-write` still requires `HERMES_GPT_ALLOW_CODEX_WRITE=1`, and Hermes still enforces the approved work directory, direct mode, `confirm=true`, and `dry_run=false`. NOLO expires with the job and is not a persistent global bypass.

**The runner path does not require `HERMES_GPT_ENABLE_CODEX` or `HERMES_GPT_ENABLE_MCP`.** Those two gates belong to Workflow A, where Codex itself is the MCP client.

### Codex executable resolution

Hermes GPT resolves a launchable standalone Codex CLI at status and launch time.

To pin a specific executable:

```powershell
$env:HERMES_GPT_CODEX_EXE="C:\path\to\codex.exe"
```

The override must resolve to an existing regular file, must not be under a protected `WindowsApps` path, and must pass a `codex --version` probe.

Without an override, Hermes GPT searches `PATH`, skips protected or unlaunchable candidates, and reports the selected path/source through `hermes_codex_status`.

Important status fields include:

- `codex_available`
- `codex_path`
- `codex_source`
- `codex_version` when available
- `codex_reason` when unavailable

### Work-directory requirements

The work directory must be both:

1. allowed by Hermes GPT Operator policy; and
2. acceptable to the Codex CLI as a trusted Git workspace.

If Codex reports that the directory is not trusted, use a dedicated intended repository and establish trust there. Do not weaken Hermes GPT's path gates or initialize Git in a broad personal directory merely to satisfy the CLI.

### Runner safety model

Delegated jobs use:

- fixed argument construction;
- `shell=False`;
- bounded timeouts;
- supported sandboxes only (`read-only`, `workspace-write`);
- bounded/redacted result material;
- prompt hashes rather than raw prompt persistence in Operator metadata/audit;
- approved work directories only.

`danger-full-access`, sandbox bypasses, arbitrary command injection, arbitrary executable arguments, and arbitrary extra-directory grants are unsupported. The supported job-scoped `nolo` mode changes Codex's approval policy only; it does not disable the selected sandbox or Hermes policy checks.

Codex review jobs are also bounded. In Swarm Orchestration, Codex can be a reviewer but never an implementation owner.

## Windows ChatGPT -> Codex deployment

For a Windows setup where ChatGPT connects to the normal Hermes GPT Operator server through a private boundary and Hermes GPT dispatches explicitly approved jobs to the standalone Codex CLI, use:

[ChatGPT to Codex through Hermes GPT on Windows](windows-chatgpt-codex.md)

That guide uses Workflow B. Do not add the Workflow A MCP-client gates unless you are also installing Hermes GPT into Codex as an MCP server.

## Updating before connector changes

Check first:

```powershell
hermes-gpt update
```

Apply explicitly:

```powershell
hermes-gpt update --apply
```

Installed-package updates check PyPI. GitHub and PyPI releases can temporarily differ, so a newer GitHub tag does not guarantee that the installed-package updater will offer that version. See [updating](updating.md).

## Example prompts for Workflow A

```text
Use hermes_plan to inspect this repository and produce a dry-run implementation plan. Do not edit files.
```

```text
Use hermes_cron_plan to turn “every Monday at 9am summarize project alerts” into a safe proposal. Do not create it.
```

```text
Use hermes_vision_analyze with this project image and keep the answer concise.
```

## Troubleshooting

### Every curated MCP tool says `CODEX_DISABLED`

Set both Workflow A base gates in the process that starts Codex and restart Codex:

```text
HERMES_GPT_ENABLE_CODEX=1
HERMES_GPT_ENABLE_MCP=1
```

### `codex doctor` reports no MCP entry

Re-run `hermes-gpt codex install`. Use `--project` only when you want repository-local configuration.

### Runner reports no launchable Codex CLI

Run `hermes_codex_status` and inspect `codex_path`, `codex_source`, and `codex_reason`. Install the standalone CLI or set `HERMES_GPT_CODEX_EXE` to a validated absolute path.

### Windows access-denied errors mention WindowsApps

A protected desktop-app shim is being selected by an old process/configuration or outside Hermes GPT's validated resolver path. Update/restart Hermes GPT, then verify `hermes_codex_status` selects the standalone CLI outside `WindowsApps`.

### Runner says the work directory is not trusted

Use a dedicated Git repository intended for the job and establish Codex trust there. Keep Hermes GPT's allowed-path policy narrow.

### Vision rejects a path

Provide the correct project root and keep the image beneath it. Secret paths and symlink escapes are intentionally blocked.

### Page extraction rejects a URL

Use a public `http` or `https` URL unless a deliberate private-network override is part of your trusted deployment.

## Related docs

- [Documentation map](README.md)
- [Operator Mode](operator-mode.md)
- [Windows ChatGPT -> Codex](windows-chatgpt-codex.md)
- [Updating](updating.md)
