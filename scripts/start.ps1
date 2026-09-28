# Convenience wrapper: .\start.ps1 -Profile ..\profiles\qwen3.8-27b.json
# Uses the repo's .venv when present (see setup_windows.ps1), else python on PATH.
param(
    [string]$Profile = "",

    [int]$Port = 8000
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } else { "python" }

$argsList = @((Join-Path $root "server_manager.py"), "--port", $Port)
if ($Profile) { $argsList += @("--profile", $Profile) }
& $py @argsList
