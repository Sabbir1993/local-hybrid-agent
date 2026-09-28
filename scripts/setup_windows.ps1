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
Write-Host "  2. Create a .venv in the repo and install the Python deps into it"
Write-Host "     (hash-locked requirements.lock, falling back to requirements.txt)"
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
& python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python 3.10+ is required."
    exit 1
}

$root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    python -m venv (Join-Path $root ".venv")
}
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install --require-hashes -r (Join-Path $root "requirements.lock")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Locked install failed - falling back to requirements.txt" -ForegroundColor Yellow
    & $venvPy -m pip install -r (Join-Path $root "requirements.txt")
}

Write-Host ""
Write-Host "Next steps (manual):" -ForegroundColor Yellow
Write-Host "  1. Go to https://github.com/ggml-org/llama.cpp/releases"
Write-Host "  2. Intel/Vulkan: download llama-<build>-bin-win-vulkan-x64.zip"
Write-Host "     NVIDIA/CUDA:  download llama-<build>-bin-win-cuda-x64.zip instead"
Write-Host "  3. Unzip it to: $InstallDir"
Write-Host "  4. Update GPU drivers (Intel Arc Control, or NVIDIA/CUDA toolkit) if stale"
Write-Host "  5. Run .\list_devices.ps1 -BinDir `"$InstallDir`" to confirm your GPU(s) show up"
Write-Host "  6. Edit config\app.json 'runtimes': set llama_bin_dir to `"$InstallDir`","
Write-Host "     backend to 'vulkan' or 'cuda', gpu_devices to your device indices, and 'runtime' to that preset"
Write-Host "  7. Edit config\app.json: set models_dir to your GGUF folder"
Write-Host "  8. .\start.ps1 -Port 8000 (uses .venv), then pick a model from the UI"
