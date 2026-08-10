# ChatGPT to Codex through Hermes GPT on Windows

This guide describes a Windows deployment in which ChatGPT is the conversational client and Hermes GPT dispatches explicitly approved jobs to the standalone Codex CLI.

```text
ChatGPT
  -> authenticated private tunnel
  -> Hermes GPT MCP on 127.0.0.1:4750/mcp
  -> Hermes Operator policy
  -> standalone Codex CLI
  -> approved Git workspace
```

This is an advanced local setup. Hermes Operator Mode is not a sandbox, and an unauthenticated public MCP endpoint is unsafe. Keep the server on loopback, protect the tunnel, restrict the allowed workspace list, and start with read-only Codex jobs.

## Prerequisites

- Windows 10 or 11
- Python 3.10 or later and a working Hermes GPT checkout
- A private tunnel that can forward HTTPS to `http://127.0.0.1:4750`
- The standalone Codex CLI, installed separately from the Codex or ChatGPT desktop application
- A dedicated Git repository to use as the approved Codex work directory

Install and authenticate the standalone CLI:

```powershell
npm install -g @openai/codex
codex login
codex --version
```

The desktop application may remain installed. Confirm that the executable resolved by the Hermes launch environment is the standalone CLI and does not point into `C:\Program Files\WindowsApps`:

```powershell
Get-Command codex -All | Select-Object Source
```

On systems with more than one Codex installation, put the standalone CLI directory first in `PATH` in the same process that starts Hermes. Do not rely on an interactive shell's `PATH`, because Task Scheduler can receive a different environment.

## Start Hermes with the required gates

The following example enables real read-only Codex dispatch for one approved workspace. Replace every placeholder before use.

```powershell
$env:HERMES_GPT_ENABLE_SESSION_SEARCH = "1"
$env:HERMES_GPT_OPERATOR_ENABLED = "1"
$env:HERMES_GPT_OPERATOR_LEVEL = "workspace"
$env:HERMES_GPT_OPERATOR_APPLY_MODE = "direct"
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES = "default"
$env:HERMES_GPT_OPERATOR_ALLOWED_PATHS = "C:\path\to\approved-workspace"
$env:HERMES_GPT_ENABLE_CODEX_RUNNER = "1"

Set-Location -LiteralPath "C:\path\to\hermes-gpt"
& ".venv\Scripts\python.exe" ".\server.py" --http --host 127.0.0.1 --port 4750
```

Leave `HERMES_GPT_ALLOW_CODEX_WRITE` unset while validating the connection. Read-only jobs do not require it. Real job dispatch still requires `confirm=true` and `dry_run=false` on the tool call.

The approved work directory must be a trusted Git repository. For a new dedicated test directory:

```powershell
Set-Location -LiteralPath "C:\path\to\approved-workspace"
git init
```

Do not initialize Git in a broad personal directory merely to satisfy this requirement.

## Connect ChatGPT

ChatGPT cannot connect to the computer's loopback address directly. Forward the local endpoint through an authenticated private tunnel, then configure the ChatGPT connector with:

```text
Protocol: Streaming HTTP
URL: https://<private-tunnel-host>/mcp
Authentication: the protection configured for the private tunnel
```

Do not enter `http://127.0.0.1:4750/mcp` in ChatGPT. Do not publish an unauthenticated Operator endpoint to the public internet.

If ChatGPT shows an old or incomplete tool list after changing Hermes gates, reconnect or recreate the connector so its schema is refreshed.

## Validate the connection

First ask ChatGPT to call `hermes_operator_policy`. A read-only runner setup should report Operator Mode enabled at `workspace` level, direct apply mode, a narrow allowed-path count, and Owner Mode disabled.

Next ask ChatGPT to call `hermes_codex_status`. Expected fields include:

```json
{
  "success": true,
  "enabled": true,
  "write_enabled": false,
  "operator_enabled": true,
  "operator_level": "workspace",
  "apply_mode": "direct",
  "codex_available": true
}
```

Finally, start a minimal read-only job:

```text
Start a Hermes Codex job in <approved-workspace> with sandbox=read-only,
confirm=true, and dry_run=false. Ask Codex only to report its working
directory and confirm that it changed no files. Monitor it to completion.
```

A successful job completes with return code `0`, reports the approved directory, and makes no file changes.

## Start automatically without visible consoles

Task Scheduler can start both the MCP server and the tunnel at logon. A task whose action launches `powershell.exe` directly may display a console even when `-WindowStyle Hidden` is present. A small Windows Script Host wrapper avoids that flash while allowing Task Scheduler to monitor the long-running child process.

Save the following as `run-powershell-hidden.vbs` in a controlled local directory:

```vbscript
Option Explicit

Dim shell, command, scriptPath
If WScript.Arguments.Count <> 1 Then WScript.Quit 2

scriptPath = WScript.Arguments(0)
Set shell = CreateObject("WScript.Shell")
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & scriptPath & """"
WScript.Quit shell.Run(command, 0, True)
```

Use this Task Scheduler action for each long-running PowerShell launcher:

```text
Program/script: C:\Windows\System32\wscript.exe
Arguments: "C:\path\to\run-powershell-hidden.vbs" "C:\path\to\launcher.ps1"
Start in: the launcher's working directory
```

Create separate tasks for Hermes and the tunnel. Configure restart-on-failure as appropriate for the machine, and keep logs free of credentials.

## Restart safely

Ending a scheduled task can occasionally leave its child `python.exe` running. If a new task run produces fresh startup output but Hermes still behaves like the previous configuration, check the listener before starting another server:

```powershell
netstat -ano | Select-String '127.0.0.1:4750'
```

Match the listening PID to the old Hermes Python process in Task Manager before ending it. Never terminate an arbitrary Python process. After the verified stale process exits, start the Hermes task again and confirm that exactly one process is listening on port `4750`.

## Troubleshooting

| Symptom | Likely cause | Resolution |
| --- | --- | --- |
| `CODEX_START_FAILED: [WinError 5] Access is denied` | Hermes found a protected WindowsApps Codex executable | Put the standalone npm Codex directory first in the launcher's `PATH`, restart Hermes, and verify `Get-Command codex -All`. |
| `codex_available=true` followed by `WinError 5` | The availability check found a binary that cannot be executed by Hermes | Verify the exact executable selected in the scheduled launch environment; it must not resolve into WindowsApps. |
| `Not inside a trusted directory and --skip-git-repo-check was not specified` | The selected work directory is not a trusted Git repository | Use a dedicated repository or initialize Git in the intended workspace. |
| New server cannot bind to port `4750` | A stale server still owns the port | Identify the listener PID, confirm that it is the old Hermes Python process, end only that process, and restart the task. |
| PowerShell consoles appear at logon | The scheduled task launches PowerShell directly | Use the `wscript.exe` wrapper shown above. |
| ChatGPT shows old tools or gates | The connector schema or server process is stale | Verify the listener, restart Hermes, then reconnect or recreate the ChatGPT connector. |
| Codex stops authenticating after an update | The standalone CLI credentials or executable path changed | Run `codex login` and `codex --version`, verify executable resolution, and restart Hermes. |

## Security checklist

- Keep Hermes bound to `127.0.0.1`.
- Require authentication or an equivalent private boundary on the tunnel.
- Keep `HERMES_GPT_OPERATOR_ALLOWED_PATHS` limited to specific workspaces.
- Leave `HERMES_GPT_ALLOW_CODEX_WRITE` unset until write jobs are deliberately required.
- Do not enable Owner Mode for an always-on connector.
- Use `sandbox=read-only` for initial inspection and validation jobs.
- Never place tunnel credentials, API keys, or authentication tokens in launch scripts, logs, or documentation.

See [Hermes GPT for Codex](codex.md) for the Codex-facing MCP connector and runner safety model, and [Operator Mode](operator-mode.md) for policy levels, gates, diagnostics, and recovery.
