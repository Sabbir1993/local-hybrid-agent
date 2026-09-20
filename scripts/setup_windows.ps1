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
Write-Host "  2. Intel/Vulkan: download llama-<build>-bin-win-vulkan-x64.zip"
Write-Host "     NVIDIA/CUDA:  download llama-<build>-bin-win-cuda-x64.zip instead"
Write-Host "  3. Unzip it to: $InstallDir"
Write-Host "  4. Update GPU drivers (Intel Arc Control, or NVIDIA/CUDA toolkit) if stale"
Write-Host "  5. Run .\list_devices.ps1 -BinDir `"$InstallDir`" to confirm your GPU(s) show up"
Write-Host "  6. Edit core\config.py CONFIG_DEFAULTS: set llama_bin_dir to `"$InstallDir`","
Write-Host "     backend to 'vulkan' or 'cuda', and gpu_devices to your device indices"
Write-Host "  7. Edit config\app.json: set models_dir to your GGUF folder"
Write-Host "  8. python server_manager.py --port 8000, then pick a model from the UI"
