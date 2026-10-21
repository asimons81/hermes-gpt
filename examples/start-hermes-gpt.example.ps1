<#
.SYNOPSIS
    Example start script for hermes-gpt on a loopback-only tunnel.
.DESCRIPTION
    Copy this file to a local script if you want to customize paths for your machine.
    Keep the real tunnel/host values private and do not commit your local copy.
#>

$ErrorActionPreference = 'Stop'
$WorkingDir = 'C:\Users\<YOU>\hermes-gpt'
$PythonExe = 'C:\Users\<YOU>\AppData\Local\Programs\Python\Python311\python.exe'
$ServerScript = 'server.py'
$ListenHost = '127.0.0.1'
$ListenPort = 4750

$env:HERMES_HOME = 'C:\Users\<YOU>\AppData\Local\hermes'
$env:HERMES_GPT_OPERATOR_ENABLED = '1'
$env:HERMES_GPT_OPERATOR_LEVEL = 'skills_config'
$env:HERMES_GPT_OPERATOR_APPLY_MODE = 'dry_run'
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES = 'default'

Set-Location $WorkingDir
& $PythonExe $ServerScript --http --host $ListenHost --port $ListenPort
