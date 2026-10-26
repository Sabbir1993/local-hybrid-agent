# Setup on a new Windows machine

Step-by-step commands to run after cloning the repo on a fresh Windows
machine. Replace every `[BRACKETED]` value with your own. Never paste real
passwords, API keys or tokens into this file or any shared document.

Linux: see [SETUP_LINUX.md](SETUP_LINUX.md). Background and config reference:
[README.md](README.md), "Setup on a new machine".

---

## Prerequisites

- Python 3.10+ from python.org (3.11 matches `requirements.lock`), with
  "Add python.exe to PATH" ticked during install
- Git for Windows
- Up-to-date GPU driver (Intel Arc Control, or NVIDIA driver + CUDA toolkit)
- Optional, for exact spreadsheet formula checking: LibreOffice (free; the app finds `soffice.exe` under `C:\Program Files\LibreOffice`
  by itself). With Microsoft Excel on a PC that runs the companion app, Excel is used first. Settings shows which engine is active
  (`GET /control/sheet_engines`).

---

## Steps

**1. Clone the repo**
```powershell
git clone [REPO_URL]
```

**2. Enter the folder**
```powershell
cd a770-dual-runtime
```

**3. Allow local scripts to run in this PowerShell window only**
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

**4. Create `.venv` and install the Python dependencies**
```powershell
.\scripts\setup_windows.ps1 -InstallDir "C:\llama-vulkan"
```

**5. Get llama.cpp.** In a browser, download `llama-<build>-bin-win-vulkan-x64.zip`
(NVIDIA: `llama-<build>-bin-win-cuda-x64.zip`) from
https://github.com/ggml-org/llama.cpp/releases, then unzip it:
```powershell
Expand-Archive -Path "$HOME\Downloads\[LLAMA_ZIP_NAME].zip" -DestinationPath "C:\llama-vulkan"
```

**6. Check that the GPUs are visible.** Note the device indices.
```powershell
.\scripts\list_devices.ps1 -BinDir "C:\llama-vulkan"
```

**6b. Download the models and helper programs.** One command fetches the models
(`routing` executor, `vision` image reader, `embedder`, `voice` speech-to-text + spoken replies, `image` local image maker;
about 20 GB in total, pick groups to save disk) and, with `--binaries`, the llama.cpp and stable-diffusion.cpp builds.
`--write-config` points `config/app.json` at everything. Re-running skips what exists and resumes partial files.
```powershell
.\.venv\Scripts\python.exe scripts/download_models.py
```
```powershell
.\.venv\Scripts\python.exe scripts/download_models.py --all --binaries --backend vulkan --write-config
```
The CPU routers (`laya`, `cactus-needle`) download from Hugging Face on first use, or with the `routing` group.

Or do steps 4-6b in one go: `.\scripts\setup_windows.ps1 -InstallDir "C:\llama-vulkan" -Download`
(`-Backend cuda` for NVIDIA; the last folder of `-InstallDir` must be `llama-<backend>`).

**Voice (speech to text).** whisper.cpp publishes no Vulkan build, so build `whisper-server.exe` once
(needs Visual Studio Build Tools, CMake and the Vulkan SDK):
```powershell
git clone https://github.com/ggml-org/whisper.cpp
```
```powershell
cmake -S whisper.cpp -B whisper.cpp\build -DGGML_VULKAN=1
```
```powershell
cmake --build whisper.cpp\build -j --config Release
```
Copy everything from `whisper.cpp\build\bin\Release` into a `whisper-vulkan` folder next to your `llama-vulkan` folder
(or set `runtimes.<name>.whisper_bin_dir`). The `ggml-large-v3*.bin` models come from the `voice` group of the download script.
Spoken replies need no extra program: `sherpa-onnx` is in `requirements.txt`, the voices come from the same group.

**7. Edit the config.** `--write-config` already set `models_dir`, `media_dirs` and the
`llama_bin_dir` / `sd_bin_dir` paths. Still yours: `common_dir`, and `gpu_devices` (the indices from step 6):
```powershell
notepad config\app.json
```

**8. Optional: run the test suite**
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

**9. Start the server.** On first run it asks you to create the super-admin
account; save the password.
```powershell
.\scripts\start.ps1 -Port 8000
```

**10.** Open http://localhost:8000, log in, then enter your cloud API keys and
connect MCP servers in Settings. API keys are stored in Windows Credential
Manager.

**10b. Web search (works out of the box; check it).** Ask something recent with the globe in the chat box turned on
("latest induction cooker prices in Bangladesh"). The answer should list sources with a line such as
`via DuckDuckGo + Bing`. Product questions also show picture cards (pictures are fetched by the server through
`/media/product-image`, so the server needs outbound HTTPS to the shops). Optional Google/SearXNG upgrade:
see "Web search engines" below.

**11. Setup the Code Runner Sandbox (WSL2)**
The chat offers `run_sandbox_python` for isolated data analysis, custom charts,
and file conversions. On Windows, execution runs inside a sandboxed WSL2 environment:

1. Install WSL2 with Ubuntu 24.04 (in an elevated PowerShell, reboot if prompted):
```powershell
wsl --install -d Ubuntu-24.04
```
2. Install the required sandbox packages into Ubuntu:
```powershell
wsl -d Ubuntu-24.04 -- bash -c "sudo apt update && sudo apt install -y python3 python3-pip && pip install -r sandbox-requirements.txt --break-system-packages"
```
3. In `config/app.json`, verify the sandbox config:
```json
"sandbox": {
  "runner_backend": "wsl2",
  "wsl_distro": "Ubuntu-24.04",
  "enabled": true
}
```

---

## Web search engines (optional: add Google)

Web search scrapes DuckDuckGo and Bing by default. For Google results, run SearXNG inside WSL2 and set
`"web": { "searxng_url": "http://[WSL_ADDRESS]:8888" }` in `config\app.json`; the steps are in
[SETUP_LINUX.md](SETUP_LINUX.md#web-search-engines-optional-add-google).

## Model Sizing & MoE VRAM Tuning

- **Context Window vs VRAM**:
  The KV cache is sized directly by context length. For large models (e.g. 26B+), a
  `96,000` (96K) context with `q8_0` KV cache requires **~23.4 GB of VRAM** for
  the KV cache alone.
- **MoE Models (e.g. Gemma-4-26B-A4B)**:
  When the KV cache consumes most of the GPU VRAM, the automated preflight will
  offload expert layers to System RAM (`--n-cpu-moe N`) to avoid an OOM crash.
  - To load **100% of the model into VRAM** on dual 16GB Arc A770s (with 0 GB offloaded to RAM):
    Set **Context size** to `32768` (32K) or `16384` (16K) in the Model Config drawer.
  - For dual GPUs, **Split mode: `row`** distributes expert matrices evenly across both cards,
    while `layer` assigns whole sequential layers.

---

## Databases

You don't need to copy any database. On first start the server creates
`auth.db`, `projects.db`, `memory.db` and `usage.db` in the repo folder, with
all tables, and asks for the super-admin account.

**Scripted first start:** set these before step 9 and the admin account is
created without a prompt. Clear them afterwards.
```powershell
$env:A770_BOOTSTRAP_USERNAME = "[ADMIN_USER]"; $env:A770_BOOTSTRAP_PASSWORD = "[ADMIN_PASSWORD]"
```
```powershell
Remove-Item Env:A770_BOOTSTRAP_USERNAME, Env:A770_BOOTSTRAP_PASSWORD
```

**To keep data from an old machine:** stop the server on both machines, then
copy these before the first start:
- `*.db`, plus any `*.db-wal` / `*.db-shm` next to them
- `knowledge_uploads/`, `compacts/`, `config/providers/`
- your `common_dir` folder

The files work on both Windows and Linux.

What doesn't move with the databases:
- **Cloud API keys and MCP OAuth tokens** live in the OS keychain, not in the
  databases. Enter them again in Settings on the new machine.
- **Memory search** needs the same embedder model; a different one gives poor
  results.

---

## Security

- The databases hold chat history, uploaded documents and password hashes.
  Move them only over an encrypted channel (scp, or an encrypted archive), not
  a shared drive or email, and delete temporary copies afterwards.
- If chats or uploads might contain card data, run `scripts/scrub_pans.py`
  before moving them (PCI-DSS / Bangladesh Bank data-handling rules).
- **LAN access:** allow the port only from your LAN. If it is reached from
  outside the host, put TLS in front (a reverse proxy such as Caddy or nginx).
```powershell
New-NetFirewallRule -DisplayName "Local Agent 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -RemoteAddress [LAN_SUBNET] -Action Allow
```
- Run only one server instance per machine; a second one kills the running
  llama servers.
