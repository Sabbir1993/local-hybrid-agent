# download_models.ps1 - fetch small models for the agent runtime into E:\AI\Models
# Sources verified 2026-09-10. Re-run safely; skips files that already exist.
$ErrorActionPreference = "Stop"
$dest = "E:\AI\Models"
[void](New-Item -ItemType Directory -Force -Path $dest)

$files = @(
    # MiniCPM5-1B-Agentic executor (hudsongouge v8 - highest downloads of the agentic GGUF line)
    @{ url = "https://huggingface.co/hudsongouge/minicpm5-1B-GLM-5.2-Agentic-v8/resolve/main/MiniCPM5-1B-Agentic-v8-q4_k_m.gguf"
       out = "MiniCPM5-1B-Agentic-Q4_K_M.gguf" },
    # SmolVLM-500M vision + its mmproj projector (ggml-org official)
    @{ url = "https://huggingface.co/ggml-org/SmolVLM-500M-Instruct-GGUF/resolve/main/SmolVLM-500M-Instruct-Q8_0.gguf"
       out = "SmolVLM-500M-Instruct-Q8_0.gguf" },
    @{ url = "https://huggingface.co/ggml-org/SmolVLM-500M-Instruct-GGUF/resolve/main/mmproj-SmolVLM-500M-Instruct-Q8_0.gguf"
       out = "mmproj-SmolVLM-500M-Instruct-Q8_0.gguf" },
    # Nomic Embed v1.5 (nomic-ai official GGUF)
    @{ url = "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf"
       out = "nomic-embed-text-v1.5-Q8_0.gguf" }
)

foreach ($f in $files) {
    $out = Join-Path $dest $f.out
    if (Test-Path -LiteralPath $out) {
        Write-Host "SKIP (exists): $($f.out)"
        continue
    }
    Write-Host "Downloading $($f.out) ..."
    & curl.exe -L --fail --progress-bar -o $out $f.url
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $($f.out)" -ForegroundColor Red }
}

Write-Host ""
Write-Host "Done. Files in $dest :"
Get-ChildItem $dest -Filter *.gguf | ForEach-Object { "  {0}  {1:N1} MB" -f $_.Name, ($_.Length/1MB) }
Write-Host ""
Write-Host "Next: pip install cactus-needle   (Needle CPU router)"
