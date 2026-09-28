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

**7. Edit the config.** Set `models_dir`, `common_dir`,
`runtimes.vulkan.llama_bin_dir`, `gpu_devices` and `media_dirs`:
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
