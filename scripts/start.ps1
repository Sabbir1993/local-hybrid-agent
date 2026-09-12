# Convenience wrapper: .\start.ps1 -Profile ..\profiles\qwen3.8-27b.json
param(
    [Parameter(Mandatory = $true)]
    [string]$Profile,

    [int]$Port = 8000
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir

python (Join-Path $root "server_manager.py") --profile $Profile --port $Port
