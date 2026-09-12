# Quick check that both Arc A770s are visible to llama.cpp's Vulkan backend.
# Usage: .\list_devices.ps1 -BinDir "C:\llama-vulkan"

param(
    [string]$BinDir = "C:\llama-vulkan"
)

$bench = Join-Path $BinDir "llama-bench.exe"
if (-not (Test-Path $bench)) {
    Write-Error "llama-bench.exe not found at $bench - check the path or unzip the Vulkan release there."
    exit 1
}

Write-Host "Vulkan devices visible to llama.cpp:" -ForegroundColor Cyan
& $bench --list-devices

Write-Host "`nCross-check against Windows Task Manager > Performance tab to confirm which" -ForegroundColor Yellow
Write-Host "index is your PCIe 3.0 card vs your other slot." -ForegroundColor Yellow
