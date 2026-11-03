# Run the SearXNG helper inside WSL2 from Windows.
#   powershell -File scripts\searxng_wsl.ps1 install   # once
#   powershell -File scripts\searxng_wsl.ps1 start     # keep this window open while you use the app
#   powershell -File scripts\searxng_wsl.ps1 check
param([ValidateSet("install", "start", "check")][string]$Action = "start", [string]$Distro = "")

$script = (Resolve-Path (Join-Path $PSScriptRoot "setup_searxng_wsl.sh")).Path
$wslPath = (wsl.exe wslpath -a ($script -replace '\\', '/')).Trim()
$args = @()
if ($Distro) { $args += @("-d", $Distro) }
& wsl.exe @args bash $wslPath $Action
