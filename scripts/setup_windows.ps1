# One-time setup helper. Doesn't do anything you couldn't do by hand -
# just saves the copy/paste. Review before running, as always with scripts
# off the internet.
#
# Usage: .\setup_windows.ps1 -InstallDir "C:\llama-vulkan"               (Python deps only, then prints the next steps)
#        .\setup_windows.ps1 -InstallDir "C:\llama-vulkan" -Download     (also fetches llama.cpp, stable-diffusion.cpp and every model group)
#        -Backend cuda  for NVIDIA.  -Groups routing,vision,embedder,voice,image  to pick model groups.

param(
    [string]$InstallDir = "C:\llama-vulkan",
    [switch]$Download,
    [ValidateSet("vulkan", "cuda", "cpu")][string]$Backend = "vulkan",
    [string]$ModelsDir = "",
    [string[]]$Groups = @("routing", "vision", "embedder", "voice", "image")
)

Write-Host "This script will:" -ForegroundColor Cyan
Write-Host "  1. Check for Python 3.10+"
Write-Host "  2. Create a .venv in the repo and install the Python deps into it"
Write-Host "     (hash-locked requirements.lock, falling back to requirements.txt)"
Write-Host "  3. With -Download: fetch llama.cpp + stable-diffusion.cpp and the models (scripts\download_models.py),"
Write-Host "     and point config\app.json at them. Without it: point you at the manual steps"
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

if ($Download) {
    $dl = @((Join-Path $PSScriptRoot "download_models.py")) + $Groups + @("--binaries", "--backend", $Backend, "--bin-dir", (Split-Path -Parent $InstallDir), "--write-config")
    if ($ModelsDir) { $dl += @("--models-dir", $ModelsDir) }
    & $venvPy @dl
    if ($LASTEXITCODE -ne 0) { Write-Host "Some downloads failed - re-run the same command to resume." -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "Still manual: whisper-server.exe (speech to text) - see SETUP_WINDOWS.md, 'Voice'." -ForegroundColor Yellow
    Write-Host "Then: .\list_devices.ps1, set gpu_devices in config\app.json, .\start.ps1 -Port 8000"
    exit 0
}

Write-Host ""
Write-Host "Next steps (manual; or re-run this script with -Download to do 2-3 and 7 for you):" -ForegroundColor Yellow
Write-Host "  1. Go to https://github.com/ggml-org/llama.cpp/releases"
Write-Host "  2. Intel/Vulkan: download llama-<build>-bin-win-vulkan-x64.zip"
Write-Host "     NVIDIA/CUDA:  download llama-<build>-bin-win-cuda-x64.zip instead"
Write-Host "  3. Unzip it to: $InstallDir"
Write-Host "  4. Update GPU drivers (Intel Arc Control, or NVIDIA/CUDA toolkit) if stale"
Write-Host "  5. Run .\list_devices.ps1 -BinDir `"$InstallDir`" to confirm your GPU(s) show up"
Write-Host "  6. Edit config\app.json 'runtimes': set llama_bin_dir to `"$InstallDir`","
Write-Host "     backend to 'vulkan' or 'cuda', gpu_devices to your device indices, and 'runtime' to that preset"
Write-Host "  7. Models: .\.venv\Scripts\python.exe scripts\download_models.py --all --write-config"
Write-Host "     (groups: routing, vision, embedder, voice, image; name some instead of --all to save disk)"
Write-Host "  8. .\start.ps1 -Port 8000 (uses .venv), then pick a model from the UI"
