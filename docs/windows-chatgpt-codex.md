# ChatGPT to Codex through Hermes GPT on Windows

This guide covers one specific deployment path:

```text
ChatGPT
  -> OpenAI Secure MCP Tunnel or another authenticated/private boundary
  -> normal Hermes GPT Operator MCP on 127.0.0.1:4750/mcp
  -> Hermes Operator policy
  -> standalone Codex CLI
  -> approved Git workspace
```

This is **Codex CLI as a delegated worker/reviewer**. It is not the separate workflow where Codex itself loads Hermes GPT as an MCP server.

For documentation authority rules, see [docs/README.md](README.md).

## Security posture

This is an advanced local setup.

- Keep Hermes GPT bound to loopback.
- For supported OpenAI products, prefer [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md) when available so Hermes GPT does not need a public inbound hostname.
- If Secure MCP Tunnel is unavailable for the target client, put another deliberately configured private/authenticated boundary in front of Hermes GPT.
- Restrict allowed workspaces narrowly.
- Start with `sandbox=read-only`.
- Do not enable Owner Mode for an always-on connector.
- Operator Mode is not an OS sandbox.
- Public unauthenticated Operator hosting is unsupported.

## Prerequisites

- Windows 10 or 11
- Python 3.10+
- Hermes GPT installed or checked out locally
- Hermes Agent available to the Hermes GPT runtime
- OpenAI Secure MCP Tunnel or another private/authenticated boundary that can carry the loopback MCP endpoint to ChatGPT
- The standalone Codex CLI
- A dedicated Git repository to use as the approved work directory

Install and authenticate the standalone Codex CLI:

```powershell
npm install -g @openai/codex
codex login
codex --version
```

The Codex or ChatGPT desktop application can remain installed. Hermes GPT must resolve a standalone CLI executable that can actually launch.

## 1. Resolve the Codex CLI cleanly

Inspect candidates:

```powershell
Get-Command codex -All | Select-Object Source
```

If multiple installations exist, the safest approach is to pin the standalone CLI explicitly in the environment that launches Hermes GPT:

```powershell
$env:HERMES_GPT_CODEX_EXE="C:\path\to\standalone\codex.exe"
```

Hermes GPT validates this override before use. The path must:

- be absolute;
- exist as a regular file;
- not resolve under protected `WindowsApps`;
- pass a `codex --version` probe.

Without an override, Hermes GPT searches `PATH` and skips protected or unlaunchable candidates.

Task Scheduler can receive a different `PATH` from an interactive terminal, so verify resolution in the actual launch environment.

## 2. Start Hermes GPT with the runner gates

A minimal real **read-only Codex runner** setup looks like this:

```powershell
$env:HERMES_GPT_OPERATOR_ENABLED = "1"
$env:HERMES_GPT_OPERATOR_LEVEL = "workspace"
$env:HERMES_GPT_OPERATOR_APPLY_MODE = "direct"
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES = "default"
$env:HERMES_GPT_OPERATOR_ALLOWED_PATHS = "C:\path\to\approved-workspace"
$env:HERMES_GPT_ENABLE_CODEX_RUNNER = "1"
$env:HERMES_GPT_CODEX_EXE = "C:\path\to\standalone\codex.exe"

Set-Location -LiteralPath "C:\path\to\hermes-gpt"
& ".venv\Scripts\python.exe" ".\server.py" --http --host 127.0.0.1 --port 4750
```

`HERMES_GPT_CODEX_EXE` is optional if Hermes GPT already resolves the correct standalone CLI from `PATH`.

Leave `HERMES_GPT_ALLOW_CODEX_WRITE` unset while validating the system. It is needed only for `sandbox=workspace-write`.

### Gates you do not need for this path

Do **not** add these merely because Codex is involved:

```text
HERMES_GPT_ENABLE_CODEX
HERMES_GPT_ENABLE_MCP
HERMES_GPT_ENABLE_SESSION_SEARCH
```

`HERMES_GPT_ENABLE_CODEX` and `HERMES_GPT_ENABLE_MCP` belong to the separate **Codex-as-MCP-client** workflow. Session search is unrelated to launching bounded Codex runner jobs.

Real runner execution still requires `confirm=true` and `dry_run=false` on the job call.

## 3. Prepare the approved workspace

The work directory must satisfy two independent checks:

1. Hermes GPT must allow it through `HERMES_GPT_OPERATOR_ALLOWED_PATHS`.
2. The Codex CLI must accept it as a trusted Git workspace.

For a new dedicated test repository:

```powershell
Set-Location -LiteralPath "C:\path\to\approved-workspace"
git init
```

Do not initialize Git in a broad personal directory merely to satisfy the CLI.

## 4. Connect ChatGPT

ChatGPT cannot directly reach the computer's loopback address.

### Preferred OpenAI path: Secure MCP Tunnel

When Secure MCP Tunnel is available for the target account/workspace, follow [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md). Keep Hermes GPT on `127.0.0.1:4750`, point `tunnel-client` at `http://127.0.0.1:4750/mcp`, validate the named profile with `doctor --explain`, and select the associated tunnel in ChatGPT developer mode.

This path does not require a public Hermes GPT hostname or a public `HERMES_GPT_ALLOWED_HOSTS` entry. The repository also ships [`../examples/start-openai-secure-mcp-tunnel.example.ps1`](../examples/start-openai-secure-mcp-tunnel.example.ps1) for supervised Windows startup.

### Other private/authenticated boundaries

If Secure MCP Tunnel is not the chosen transport, forward the local MCP endpoint through a deliberately configured private/authenticated HTTPS boundary, then configure the connector with that boundary's HTTPS endpoint:

```text
Protocol: Streaming HTTP
URL: https://<private-host>/mcp
Authentication: the protection configured for that boundary
```

Do not enter `http://127.0.0.1:4750/mcp` as a remote ChatGPT connector URL. Do not expose the Operator endpoint to the public internet without authentication.

If the client shows an old or incomplete tool list after changing Hermes GPT, restart the intended server process and reconnect/recreate the connector so its MCP schema refreshes.

## 5. Validate policy before running Codex

First call:

```text
hermes_operator_policy
```

Confirm:

- Operator Mode is enabled;
- effective level is `workspace`;
- apply mode is `direct` only because this deployment is intended to execute confirmed jobs;
- allowed paths are narrow;
- Owner Mode is not active.

Then call:

```text
hermes_codex_status
```

Expected fields include values like:

```json
{
  "success": true,
  "enabled": true,
  "write_enabled": false,
  "operator_enabled": true,
  "operator_level": "workspace",
  "apply_mode": "direct",
  "codex_available": true,
  "codex_source": "env"
}
```

Also inspect `codex_path` and `codex_version`. If `codex_available` is false, read `codex_reason` instead of attempting a job.

## 6. Run the first read-only job

Start with a minimal inspection task:

```text
Start a Hermes Codex job in <approved-workspace> with sandbox=read-only,
confirm=true, and dry_run=false. Ask Codex only to report its working
directory and confirm that it changed no files. Monitor it to completion.
```

A successful validation job should:

- run in the approved repository;
- use the standalone Codex CLI reported by `hermes_codex_status`;
- complete without write authority;
- make no file changes.

Only after that should you consider `workspace-write` and `HERMES_GPT_ALLOW_CODEX_WRITE=1` for deliberately approved write jobs.

## 7. Run Codex reviews

Use `hermes_codex_review_start` for bounded review jobs. Review targets are constrained by the runner rather than accepting arbitrary command-line arguments.

In Swarm Orchestration, Codex can provide a review verdict but is never an implementation owner.

## 8. Start automatically without visible consoles

For Secure MCP Tunnel, prefer a single supervised launcher so Hermes GPT and `tunnel-client` fail together instead of leaving a stale half-running bridge. Copy and customize [`../examples/start-openai-secure-mcp-tunnel.example.ps1`](../examples/start-openai-secure-mcp-tunnel.example.ps1) in a private local path.

For another boundary, Task Scheduler can start Hermes GPT and the boundary at logon using separate supervised tasks.

Launching `powershell.exe` directly can flash a console even with `-WindowStyle Hidden`. A small Windows Script Host wrapper can hide the launcher while letting Task Scheduler monitor the process.

Save as `run-powershell-hidden.vbs` in a controlled local directory:

```vbscript
Option Explicit

Dim shell, command, scriptPath
If WScript.Arguments.Count <> 1 Then WScript.Quit 2

scriptPath = WScript.Arguments(0)
Set shell = CreateObject("WScript.Shell")
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & scriptPath & """"
WScript.Quit shell.Run(command, 0, True)
```

Task Scheduler action:

```text
Program/script: C:\Windows\System32\wscript.exe
Arguments: "C:\path\to\run-powershell-hidden.vbs" "C:\path\to\launcher.ps1"
Start in: the launcher's working directory
```

Configure restart-on-failure as appropriate and keep logs free of credentials.

## 9. Restart safely

A stopped scheduled task can occasionally leave a child `python.exe` running. If a new launch appears successful but Hermes GPT still behaves like the previous process, check the listener:

```powershell
netstat -ano | Select-String '127.0.0.1:4750'
```

Match the PID to the known Hermes GPT process before terminating anything. Never kill an arbitrary Python process.

After stopping the verified stale process, restart Hermes GPT and confirm exactly one expected listener owns port `4750`.

## Troubleshooting

| Symptom | Likely cause | Resolution |
| --- | --- | --- |
| `codex_available=false` | No launchable standalone CLI resolved | Inspect `codex_reason`; install Codex or set `HERMES_GPT_CODEX_EXE` to a valid standalone CLI. |
| Access denied / path mentions `WindowsApps` | Old process, external launcher, or stale config is selecting a protected desktop-app shim | Update/restart Hermes GPT, pin `HERMES_GPT_CODEX_EXE`, and re-check `hermes_codex_status`. |
| `Not inside a trusted directory...` | Codex does not trust the selected work directory | Use an intended Git repository and establish trust there. |
| Job returns `POLICY_REFUSED` | Operator level/path gates do not authorize the workdir | Inspect `hermes_operator_policy`; narrow and correct `HERMES_GPT_OPERATOR_ALLOWED_PATHS`. |
| Job returns runner disabled | `HERMES_GPT_ENABLE_CODEX_RUNNER=1` is missing in the running process | Set it in the actual launch environment and restart Hermes GPT. |
| Job previews but does not execute | Server/call is still dry-run | For an approved real job, use direct mode plus `confirm=true` and `dry_run=false`. |
| New server cannot bind port 4750 | Stale server still owns the listener | Identify and verify the listener PID before ending that specific process. |
| Secure tunnel is not visible in ChatGPT | Missing workspace association, Tunnels Use, or developer-mode access | Follow the association/permission checks in [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md). |
| ChatGPT shows old tools/gates | Stale server or cached connector schema | Verify the listener, restart Hermes GPT, then reconnect/recreate the connector. |

## Security checklist

- Keep Hermes GPT on `127.0.0.1`.
- Prefer Secure MCP Tunnel for supported OpenAI private access when available.
- Require a deliberate private/authenticated boundary for every other remote path.
- Restrict `HERMES_GPT_OPERATOR_ALLOWED_PATHS` to specific workspaces.
- Leave `HERMES_GPT_ALLOW_CODEX_WRITE` unset until a write job is deliberately required.
- Keep Owner Mode off for always-on access.
- Validate with `sandbox=read-only` first.
- Pin `HERMES_GPT_CODEX_EXE` when executable ambiguity exists.
- Never put tunnel credentials, API keys, tokens, or private keys in launch scripts, logs, prompts, or documentation.

## Related docs

- [Documentation map](README.md)
- [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md)
- [Hermes GPT and Codex](codex.md)
- [Operator Mode](operator-mode.md)
- [Updating](updating.md)
