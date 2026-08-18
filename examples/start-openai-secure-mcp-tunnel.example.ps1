<#
.SYNOPSIS
    Example supervised launcher for Hermes GPT plus OpenAI Secure MCP Tunnel.
.DESCRIPTION
    Copy this file to a private local path before customizing machine-specific
    executable paths. Keep all credentials out of this file.

    Required environment:
      CONTROL_PLANE_API_KEY  Tunnel runtime API key used by tunnel-client.

    Optional Hermes GPT environment such as Operator policy or a static bearer
    token should also be supplied by the service/scheduled-task environment,
    not committed into this example.
#>

$ErrorActionPreference = 'Stop'

$WorkingDir = 'C:\Users\<YOU>\hermes-gpt'
$PythonExe = 'C:\Users\<YOU>\AppData\Local\Programs\Python\Python311\python.exe'
$TunnelClientExe = 'C:\Tools\tunnel-client\tunnel-client.exe'
$TunnelProfile = 'hermes-gpt'
$ListenHost = '127.0.0.1'
$ListenPort = 4750
$ReadyTimeoutSeconds = 30
$PollSeconds = 1

function Assert-FileExists {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not found: $Path"
    }
}

function Wait-ForOwnedListener {
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$HostAddress,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $Process.Refresh()
        if ($Process.HasExited) {
            throw "Hermes GPT exited before the MCP listener became ready (exit $($Process.ExitCode))."
        }

        $listener = Get-NetTCPConnection `
            -LocalAddress $HostAddress `
            -LocalPort $Port `
            -State Listen `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.OwningProcess -eq $Process.Id } |
            Select-Object -First 1

        if ($null -ne $listener) {
            return
        }

        Start-Sleep -Milliseconds 250
    }

    throw "Timed out waiting for Hermes GPT PID $($Process.Id) to own $HostAddress`:$Port."
}

function Stop-OwnedProcess {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return
    }

    try {
        $Process.Refresh()
        if ($Process.HasExited) {
            return
        }

        Stop-Process -Id $Process.Id -ErrorAction Stop
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 100
            $Process.Refresh()
            if ($Process.HasExited) {
                return
            }
        }

        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
    catch {
        Write-Warning "Could not stop owned PID $($Process.Id): $($_.Exception.Message)"
    }
}

if ([string]::IsNullOrWhiteSpace($env:CONTROL_PLANE_API_KEY)) {
    throw 'CONTROL_PLANE_API_KEY is required. Load the tunnel runtime key from a private service environment before starting this launcher.'
}

Assert-FileExists -Path $PythonExe -Label 'Python executable'
Assert-FileExists -Path $TunnelClientExe -Label 'tunnel-client executable'
Assert-FileExists -Path (Join-Path $WorkingDir 'server.py') -Label 'Hermes GPT server.py'

$hermesProcess = $null
$tunnelProcess = $null
$exitCode = 1

try {
    $hermesProcess = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList @('server.py', '--http', '--host', $ListenHost, '--port', [string]$ListenPort) `
        -WorkingDirectory $WorkingDir `
        -PassThru `
        -NoNewWindow

    Wait-ForOwnedListener `
        -Process $hermesProcess `
        -HostAddress $ListenHost `
        -Port $ListenPort `
        -TimeoutSeconds $ReadyTimeoutSeconds

    Write-Host "Hermes GPT ready on http://$ListenHost`:$ListenPort/mcp (PID $($hermesProcess.Id))."

    $doctorProcess = Start-Process `
        -FilePath $TunnelClientExe `
        -ArgumentList @('doctor', '--profile', $TunnelProfile, '--explain') `
        -PassThru `
        -Wait `
        -NoNewWindow

    if ($doctorProcess.ExitCode -ne 0) {
        throw "tunnel-client doctor failed with exit code $($doctorProcess.ExitCode)."
    }

    $tunnelProcess = Start-Process `
        -FilePath $TunnelClientExe `
        -ArgumentList @('run', '--profile', $TunnelProfile) `
        -PassThru `
        -NoNewWindow

    Write-Host "OpenAI Secure MCP Tunnel running with profile '$TunnelProfile' (PID $($tunnelProcess.Id))."

    while ($true) {
        Start-Sleep -Seconds $PollSeconds
        $hermesProcess.Refresh()
        $tunnelProcess.Refresh()

        if ($hermesProcess.HasExited) {
            $exitCode = $hermesProcess.ExitCode
            Write-Warning "Hermes GPT exited with code $exitCode. Stopping the owned tunnel-client process."
            break
        }

        if ($tunnelProcess.HasExited) {
            $exitCode = $tunnelProcess.ExitCode
            Write-Warning "tunnel-client exited with code $exitCode. Stopping the owned Hermes GPT process."
            break
        }
    }
}
finally {
    Stop-OwnedProcess -Process $tunnelProcess
    Stop-OwnedProcess -Process $hermesProcess
}

exit $exitCode
