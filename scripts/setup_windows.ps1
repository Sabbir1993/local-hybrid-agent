# One-time setup helper. Doesn't do anything you couldn't do by hand -
# just saves the copy/paste. Review before running, as always with scripts
# off the internet.
#
# Usage: .\setup_windows.ps1 -InstallDir "C:\llama-vulkan"

param(
    [string]$InstallDir = "C:\llama-vulkan"
)

Write-Host "This script will:" -ForegroundColor Cyan
Write-Host "  1. Check for Python 3.10+"
Write-Host "  2. Install the orchestration Python deps (fastapi, uvicorn, httpx)"
Write-Host "  3. Point you at the right llama.cpp release page (download is manual -"
Write-Host "     GitHub release assets change per build number, not worth scripting)"
Write-Host ""

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Error "Python not found on PATH. Install Python 3.10+ from python.org first."
    exit 1
}
$pyVersion = (& python --version)
Write-Host "Found $pyVersion"

$reqPath = Join-Path (Split-Path -Parent $PSScriptRoot) "requirements.txt"
python -m pip install -r $reqPath

Write-Host ""
Write-Host "Next steps (manual):" -ForegroundColor Yellow
Write-Host "  1. Go to https://github.com/ggml-org/llama.cpp/releases"
Write-Host "  2. Download the asset named like: llama-<build>-bin-win-vulkan-x64.zip"
Write-Host "     (NOT -sycl- or -cuda-)"
Write-Host "  3. Unzip it to: $InstallDir"
Write-Host "  4. Update Arc drivers from Intel if you haven't recently"
Write-Host "  5. Run .\list_devices.ps1 -BinDir `"$InstallDir`" to confirm both GPUs show up"
Write-Host "  6. Edit profiles\*.json: set llama_bin_dir to `"$InstallDir`" and model_path"
Write-Host "     to your downloaded GGUF files"
Write-Host "  7. python autotune.py --profile profiles\qwen3.8-27b.json"
