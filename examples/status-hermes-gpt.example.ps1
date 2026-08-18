<#
.SYNOPSIS
    Example status script for a loopback Hermes GPT deployment.
.DESCRIPTION
    Reports the local MCP listener and can optionally run tunnel-client doctor
    for a named OpenAI Secure MCP Tunnel profile.

    This script intentionally does not assume a public tunnel URL. Secure MCP
    Tunnel keeps Hermes GPT on loopback and uses a separately managed tunnel ID.
#>

param(
    [int]$ListenPort = 4750,
    [string]$TunnelProfile = 'hermes-gpt',
    [string]$TunnelClientExe = 'tunnel-client.exe',
    [switch]$RunDoctor
)

$ListenHost = '127.0.0.1'
$McpUrl = "http://$ListenHost`:$ListenPort/mcp"

Write-Host "Local MCP URL  : $McpUrl"

$listener = Get-NetTCPConnection `
    -LocalAddress $ListenHost `
    -LocalPort $ListenPort `
    -State Listen `
    -ErrorAction SilentlyContinue |
    Select-Object -First 1

if ($null -eq $listener) {
    Write-Host "Listener       : not listening on $ListenHost`:$ListenPort"
}
else {
    Write-Host "Listener       : listening (PID $($listener.OwningProcess))"
}

Write-Host "Tunnel profile : $TunnelProfile"
Write-Host 'Tunnel URL     : not public; select the associated tunnel in the supported OpenAI product'
Write-Host 'MCP probe      : use an MCP client or tunnel-client doctor, not a browser GET'

if (-not $RunDoctor) {
    Write-Host "Doctor         : skipped (rerun with -RunDoctor to execute tunnel-client doctor --explain)"
    exit 0
}

if ([string]::IsNullOrWhiteSpace($env:CONTROL_PLANE_API_KEY)) {
    throw 'CONTROL_PLANE_API_KEY is required to run tunnel-client doctor.'
}

& $TunnelClientExe doctor --profile $TunnelProfile --explain
exit $LASTEXITCODE
