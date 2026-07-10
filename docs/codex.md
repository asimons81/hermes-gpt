# Hermes GPT for Codex

Hermes GPT can expose selected local Hermes Agent capabilities to the Codex app, Codex CLI, and Codex IDE integration through a local MCP server. It is a local bridge; it does not patch Codex, store OpenAI credentials, bypass approvals, or enable writes by default.

## Install

Set the base gates in the environment that will launch Codex, then install the MCP entry:

```powershell
$env:HERMES_GPT_ENABLE_CODEX="1"
$env:HERMES_GPT_ENABLE_MCP="1"
hermes-gpt codex install
hermes-gpt codex doctor
```

`install` uses `codex mcp add hermes-gpt -- ...` when the Codex CLI is available. If it must edit TOML directly, it first validates the config, creates a timestamped backup, preserves unrelated configuration, and adds only `[mcp_servers."hermes-gpt"]` plus its environment table. It is idempotent.

For a repository-local entry, run the command from inside that repository:

```powershell
hermes-gpt codex install --project
```

This creates or updates `<git-root>/.codex/config.toml`; Codex CLI currently has no project-scope switch for `mcp add`.

## Verify and remove

```powershell
hermes-gpt codex print-config
hermes-gpt codex doctor
hermes-gpt codex uninstall
```

`doctor` is read-only. It checks the Codex binary/version, target config, MCP registry, base gates, gateway status, and redaction smoke path. `uninstall` removes only the Hermes GPT MCP tables and makes a backup first.

Before reinstalling a connector after a release, run `hermes-gpt update` to check safely; use `hermes-gpt update --apply` only when you are ready to update the checkout or installed package. See [updating](updating.md) for the exact safety behavior.

## Available Codex tools

| Tool | Behavior |
| --- | --- |
| `hermes_status` | Local Hermes GPT/gateway status, including state-file PID fallback. |
| `hermes_capabilities` | Enabled features and the gate required for disabled ones. |
| `hermes_plan` | Compact, read-only repository context and implementation plan. |
| `hermes_vision_analyze` | Approved local raster image analysis. |
| `hermes_web_search` | Search only; no pages are fetched. |
| `hermes_extract_page` | Public HTTP(S) content extraction. |
| `hermes_cron_plan` | Dry-run cron proposal from a simple schedule request. |
| `hermes_cron_create` | Explicitly confirmed, tightly gated creation. |
| `hermes_author_skill` | Skill draft by default; write requires explicit gates. |
| `hermes_gateway_diagnostics` | Read-only gateway and PID diagnostics. |

## Safety model

The server launches even when gates are absent, so Codex can list the tool schemas and receive an actionable blocked response. The base gates are:

```text
HERMES_GPT_ENABLE_CODEX=1
HERMES_GPT_ENABLE_MCP=1
```

Individual feature gates are `HERMES_GPT_ENABLE_VISION=1`, `HERMES_GPT_ENABLE_WEB=1`, `HERMES_GPT_ENABLE_CRON=1`, and `HERMES_GPT_ENABLE_DIAGNOSTICS=1`.

Every persistent action is dry-run by default. Direct skill or cron writes require the base/feature gates plus `HERMES_GPT_ALLOW_WRITE=1` and the relevant `HERMES_GPT_ALLOW_SKILL_WRITE=1` or `HERMES_GPT_ALLOW_CRON_WRITE=1`; existing Hermes Operator Mode policy and direct-apply gates remain in force too.

Local image paths resolve symlinks, must remain under an explicit `project_root` (or `HERMES_GPT_CODEX_ALLOWED_ROOTS`), and reject secret paths and unsupported types. Web extraction accepts only public HTTP(S) URLs; it blocks file URLs, localhost, private/loopback/link-local/reserved IPs, and metadata targets unless the explicit private-network override is set. Returned text is redacted for common API keys, provider/GitHub tokens, bearer/cookie/session values, and private keys.

## Example Codex prompts

```text
Use hermes_plan to inspect this repository and produce a dry-run build plan. Do not edit files.
```

```text
Use hermes_cron_plan to turn “every Monday at 9am summarize project alerts” into a safe proposal. Do not create it.
```

```text
Use hermes_vision_analyze with this project image and keep the answer concise.
```

## Troubleshooting

- If every tool says `CODEX_DISABLED`, set both base gates in the process that starts Codex and restart Codex.
- If `doctor` reports no MCP entry, rerun `hermes-gpt codex install`; use `--project` when you intend the current repository only.
- If vision rejects a path, provide `project_root` and keep the image beneath it; secret files and symlink escapes are intentionally blocked.
- If page extraction rejects a URL, use a public `http` or `https` URL. Local/private address access is intentionally not the default.
