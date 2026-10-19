# Deploying on a Mac server

Step-by-step commands to run the app on a macOS machine (Apple Silicon
recommended). Replace every `[BRACKETED]` value with your own. Never paste real
passwords, API keys or tokens into this file or any shared document.

Related: [SETUP_LINUX.md](SETUP_LINUX.md), [SETUP_WINDOWS.md](SETUP_WINDOWS.md),
[README.md](README.md).

> **Read this first: macOS is not a tested platform for this repo.**
> The code was written for Windows (Vulkan/CUDA) and has a Linux path. On a Mac,
> the only GPU backend is **Metal**, and the app does not know about it yet.
> Section 5 lists the small code change needed so the GPU/VRAM layer works. I
> have not run this on a Mac, so treat the steps as a checklist to verify, not a
> guarantee. Without the Section 5 change the app still starts, but VRAM
> preflight and the GPU panel cannot read devices and loading a model can be
> blocked.

---

## 0. What you are deploying

| Piece | What it is | Port |
|---|---|---|
| `server_manager.py` | FastAPI web app + UI + process manager | `8000` (default) |
| `llama-server` | llama.cpp inference process(es), started by the app | `8090`+ (local only) |
| embedder / helper models | extra `llama-server` instances (e.g. nomic-embed on `8093`) | local only |
| SQLite DBs | `auth.db`, `projects.db`, `memory.db`, `usage.db` in the repo folder | n/a |
| Optional companion app | Electron app on the *user's* device for agent file/shell tools | n/a |

The agent file/shell tools run on the user's own device through the companion,
never on the server disk (see the "No server workspace" policy). Nothing to
install for that on the Mac server.

---

## 1. Prerequisites

- macOS 13+ on Apple Silicon (M1/M2/M3/M4). Intel Macs work only for small
  CPU-bound models.
- **Memory matters most:** Apple Silicon uses unified memory, so the model, KV
  cache and OS share the same RAM. A 9B Q4 model needs roughly 8-10 GB free; a
  27B Q4 needs about 20+ GB. Pick the Mac by the largest model you plan to load.
- An admin account on the Mac, and a dedicated **non-admin** user to run the
  service (Section 9).
- Xcode command-line tools and Homebrew.

```bash
xcode-select --install
```
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

## 2. Install system packages

```bash
brew install python@3.11 git cmake wget
```

Confirm Python is 3.10+ (3.11 matches `requirements.lock`):

```bash
python3.11 --version
```

## 3. Get the code

```bash
git clone [REPO_URL] ~/a770-dual-runtime
```
```bash
cd ~/a770-dual-runtime
```

## 4. Python environment

`scripts/setup_linux.sh` is Linux-flavoured (it probes `vulkaninfo`), so create
the venv by hand:

```bash
python3.11 -m venv .venv
```
```bash
.venv/bin/python -m pip install --upgrade pip
```

`requirements.lock` was compiled on Windows, so a hash can fail on macOS wheels.
Try the locked install first, then fall back:

```bash
.venv/bin/python -m pip install --require-hashes -r requirements.lock
```
```bash
.venv/bin/python -m pip install -r requirements.txt
```

Run only the second command if the first fails.

Make the start script executable:

```bash
chmod +x scripts/start.sh
```

## 5. Install llama.cpp with Metal

Either download a prebuilt macOS build (`llama-<build>-bin-macos-arm64.zip`) from
https://github.com/ggml-org/llama.cpp/releases, or build it (Metal is on by
default on Apple Silicon):

```bash
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
```
```bash
cmake -S ~/llama.cpp -B ~/llama.cpp/build -DGGML_METAL=ON -DCMAKE_BUILD_TYPE=Release
```
```bash
cmake --build ~/llama.cpp/build --config Release -j
```

Binaries land in `~/llama.cpp/build/bin`. Use that as `llama_bin_dir`.

If you downloaded a zip, remove the quarantine flag or macOS will refuse to run
the binaries:

```bash
xattr -dr com.apple.quarantine [LLAMA_BIN_DIR]
```

List the devices and **note the device name** (on Metal it is `MTL0`):

```bash
[LLAMA_BIN_DIR]/llama-bench --list-devices
```

### Required code change for Metal (not in the repo yet)

`core/backend.py` only knows `vulkan` and `cuda`, and `core/vram/devices.py`
only parses `Vulkan<N>:` / `CUDA<N>:` lines. On a Mac that means
`DeviceQueryError`, an unknown VRAM verdict, and with `preflight.mode = "block"`
a refused model load.

Edit `core/backend.py`:

```python
DEVICE_PREFIX = {"vulkan": "Vulkan", "cuda": "CUDA", "metal": "MTL"}
VISIBLE_DEVICES_ENV = {"vulkan": "GGML_VK_VISIBLE_DEVICES", "cuda": "CUDA_VISIBLE_DEVICES",
                       "metal": "GGML_METAL_NOOP"}  # Metal has no visible-devices variable
```

`core/vram/devices.py` builds its regex from `DEVICE_PREFIX`, so it picks `MTL` up
automatically. Then verify the parse against your real output:

```bash
.venv/bin/python -c "from core import vram; print(vram.query_devices(strict=True))"
```

If that prints your Apple GPU, the layer works. Two further things to check:

- `llama-bench --list-devices` on Apple Silicon reports unified memory as the
  device size; "free" may be approximate. Keep `preflight.headroom_gb` generous
  (3-4 GB) so macOS itself is not starved.
- The GPU panel on non-Windows reads the same list (`core/gpu.py`); it will show
  memory in use but no compute %.

Fast fallback if you do not want to patch yet: set `preflight.mode` to `"warn"`
in `config/app.json`. This turns off the safety net, so prefer the patch.

Please run the unit tests after the change (Section 7).

## 6. Models and config

Create the folders:

```bash
mkdir -p ~/Models ~/common
```

Copy your `.gguf` files into `~/Models` (use `scp`/`rsync` over SSH, not a shared
drive).

```bash
rsync -avP [USER]@[SOURCE_HOST]:[PATH_TO_MODELS]/ ~/Models/
```

Edit `config/app.json` (every `E:/...` path must change):

```bash
nano config/app.json
```

```json
"models_dir": "/Users/[USER]/Models",
"common_dir": "/Users/[USER]/common",
"runtime": "metal",
"runtimes": {
  "metal": {
    "llama_bin_dir": "/Users/[USER]/llama.cpp/build/bin",
    "backend": "metal",
    "gpu_devices": [0],
    "small_model_gpu": 0
  }
},
"media_dirs": {
  "voice_models": "/Users/[USER]/Models/orchestrator/voice-models",
  "image_models": "/Users/[USER]/Models/orchestrator/image-models",
  "video_models": "/Users/[USER]/Models/orchestrator/video-models"
}
```

Notes:

- There is **one** Metal device, so the dual-GPU tensor split and the
  "run Ornith on Vulkan2 only" layout do not apply. Use `gpu_devices: [0]`.
- Keep the big-model + helper/embedder models within unified memory. Lower
  `serving` concurrency and context size in the UI if the Mac swaps.
- `config/model_configs.json` holds per-model overrides keyed by filename; the
  existing entries were tuned for Arc cards. Re-run `autotune.py` on the Mac
  rather than reusing them.
- Voice (TTS) needs `sherpa-onnx` (in requirements) plus the voice models
  from your Windows machine; copy them under `media_dirs.voice_models`.
- `LLAMA_RUNTIME=<name>` in the environment overrides `runtime` if needed.

## 7. Smoke test (before making it a service)

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

Some Windows-only tests may fail. Anything about `backend`, `vram`, `gpu` or
`process` on the Mac is worth a look.

Scripted first start (creates the super-admin without a prompt). The leading
space keeps the line out of zsh history when `HIST_IGNORE_SPACE` is on:

```bash
 export A770_BOOTSTRAP_USERNAME=[ADMIN_USER] A770_BOOTSTRAP_PASSWORD=[ADMIN_PASSWORD]
```
```bash
./scripts/start.sh --host 127.0.0.1 --port 8000
```
```bash
unset A770_BOOTSTRAP_USERNAME A770_BOOTSTRAP_PASSWORD
```

Open `http://127.0.0.1:8000`, log in, pick a model, send a chat message. Watch the
terminal for `[vram]` and `llama-server` lines. Then stop it with `Ctrl+C`.

Only run **one** server instance per Mac: a second `server_manager` kills the
running `llama-server` processes (`pkill -x llama-server` on startup).

### 7.1. Isolated Code Runner Sandbox on macOS

The chat offers `run_sandbox_python` for isolated data analysis, custom charts,
and conversions. Note that **Bubblewrap (`bwrap`) is Linux-only** and does not run on macOS Darwin.

You have two options for the sandbox runner on macOS:

- **Option A (Recommended for production): Remote Linux Runner VM**
  Set up an isolated Ubuntu/Alpine Linux VM or external machine with Bubblewrap:
  1. On the Linux VM: `sudo apt install bubblewrap python3-pip && pip install -r sandbox-requirements.txt`
  2. Start the runner: `python3 tools/runner/runner_server.py serve --port 8470`
  3. Generate a pairing code: `python3 tools/runner/runner_server.py pair-code`
  4. In the Mac app UI, go to **Settings -> Code Runner**, enter the address `https://[RUNNER_IP]:8470` and the pairing code.
  5. In `config/app.json`: `"runner_backend": "remote"`.

- **Option B: Local Fallback Process Runner**
  Runs jobs in an isolated temporary folder with process timeouts and clean environment:
  1. Install sandbox dependencies in your Mac virtual environment:
     ```bash
     .venv/bin/python -m pip install -r sandbox-requirements.txt
     ```
  2. In `config/app.json`:
     ```json
     "sandbox": {
       "runner_backend": "local",
       "enabled": true
     }
     ```

### 7.2. Unified Memory & Context Sizing (MoE Models)

Apple Silicon uses unified RAM for both the OS and GPU (Metal).
- **Context Size Impact**: In large models (e.g. 26B+ MoE), setting context to `96,000` (96K)
  reserves **~23+ GB** for the KV cache alone.
- **MoE Expert Offloading**: When VRAM/unified memory is insufficient, the preflight
  automatically offloads expert layers to CPU RAM (`--n-cpu-moe N`).
- **To avoid swap or RAM spill**: For models like `Gemma-4-26B-A4B`, set **Context size**
  to `32768` (32K) or `16384` (16K) in the Model Config drawer.

## 8. Secrets and the keychain

API keys and MCP OAuth tokens are stored with the `keyring` package, which uses
the **macOS Keychain** natively. Points to know:

- The Keychain is tied to a *logged-in user session*. A LaunchDaemon (system
  service) cannot reach a login keychain. Use a **LaunchAgent** under the
  service user with auto-login (Section 9), or that user must be logged in.
- The first time Python reads a stored secret, macOS may show a "allow access"
  prompt. Click **Always Allow** once while you are at the machine (or over
  screen sharing), otherwise a headless start hangs.
- Do **not** install `keyrings.alt` (plaintext file store).
- Cloud API keys and MCP tokens do not live in the databases, so re-enter them
  in **Settings** on the new machine.

## 9. Run it as a service (launchd)

1. Create a standard user for the service (System Settings > Users), e.g.
   `localagent`. Enable auto-login for it if the Mac is unattended (needed for
   the Keychain), and turn on FileVault only if you can enter the password after
   a reboot.
2. Put the app in that user's home and make sure the user owns it:

```bash
sudo chown -R [SERVICE_USER] /Users/[SERVICE_USER]/a770-dual-runtime
```

3. Create `~/Library/LaunchAgents/com.sslwireless.localagent.plist` as the
   service user:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.sslwireless.localagent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/[SERVICE_USER]/a770-dual-runtime/scripts/start.sh</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/[SERVICE_USER]/a770-dual-runtime</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/[SERVICE_USER]/a770-dual-runtime/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/[SERVICE_USER]/a770-dual-runtime/logs/launchd.err.log</string>
</dict>
</plist>
```

4. Load it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sslwireless.localagent.plist
```

Manage it:

```bash
launchctl kickstart -k gui/$(id -u)/com.sslwireless.localagent
```
```bash
launchctl bootout gui/$(id -u)/com.sslwireless.localagent
```

5. Keep the Mac awake and auto-restarting:

```bash
sudo pmset -a sleep 0 disksleep 0 displaysleep 10 autorestart 1
```

## 10. Network access and TLS

The app serves plain HTTP and prints a warning if bound beyond localhost. The
recommended setup is: app on `127.0.0.1:8000`, a reverse proxy with TLS in
front. This matters here because the databases hold chat history, uploads and
password hashes (PCI-DSS / Bangladesh Bank data-handling expectations).

Caddy example:

```bash
brew install caddy
```

`/opt/homebrew/etc/Caddyfile`:

```
[HOSTNAME] {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
brew services start caddy
```

For an internal CA use `tls internal` (or your own certificate files). The app
already trusts `X-Forwarded-*` headers (`proxy_headers=True`).

Firewall: macOS Settings > Network > Firewall > on; allow only Caddy inbound
(ports 80/443). Do not open port 8000 or the `llama-server` ports.

If you must bind the app directly to the LAN (trusted network only):

```bash
./scripts/start.sh --host 0.0.0.0 --port 8000
```

Also: after first login, set the session idle timeout, API tokens and MFA in
Settings as your policy requires.

## 11. Moving data from an existing install

Stop the server on **both** machines first, then copy:

- `auth.db projects.db memory.db usage.db` and any `*.db-wal` / `*.db-shm`
- `knowledge_uploads/`, `compacts/`, `config/providers/`
- your `common_dir` folder

```bash
rsync -avP [USER]@[OLD_HOST]:[OLD_REPO_PATH]/*.db ~/a770-dual-runtime/
```
```bash
chmod 600 ~/a770-dual-runtime/*.db
```

Rules:

- Transfer only over an encrypted channel (SSH/rsync), never email or a shared
  drive; delete temporary copies.
- If chats or uploads might contain card numbers, run
  `scripts/scrub_pans.py` **before** moving them.
- Keep the same embedder model, or memory/KB search quality drops (re-index if
  you change it).
- Windows paths stored inside the databases or `config/` (`E:/...`) will not
  resolve; fix them in Settings.
- Fresh installs need no copy: the DBs are created on first start.

## 12. Backups and updates

Backup (stop the app or use SQLite's online backup; the repo also keeps
`db_backups/`):

```bash
sqlite3 auth.db ".backup '[BACKUP_DIR]/auth.db'"
```

Repeat for `projects.db`, `memory.db`, `usage.db`, and copy
`knowledge_uploads/` and `config/`. Encrypt backups at rest (Time Machine with
encryption, or `age`/`gpg`).

Update:

```bash
cd ~/a770-dual-runtime && git pull
```
```bash
.venv/bin/python -m pip install -r requirements.txt
```
```bash
launchctl kickstart -k gui/$(id -u)/com.sslwireless.localagent
```

## 13. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "llama-server not found in ..." | `llama_bin_dir` wrong; run `find ~/llama.cpp -name llama-server` |
| "cannot be opened because the developer cannot be verified" | `xattr -dr com.apple.quarantine [LLAMA_BIN_DIR]` |
| `[vram] cannot verify VRAM` / model refused | Section 5 Metal patch missing, or `llama-bench --list-devices` output not parsed |
| Hangs at startup reading secrets | Keychain prompt waiting; click Always Allow, or run as a logged-in user |
| Model loads then the Mac crawls | Memory pressure; use a smaller quant/context, fewer parallel slots |
| UI works locally, not remotely | Bound to 127.0.0.1; use the Caddy proxy, firewall allows 443 |
| Second instance killed the model | Only one `server_manager` per Mac |
| `pip` hash mismatch | Fall back to `requirements.txt` (Section 4) |

## 14. Deployment checklist

- [ ] Python 3.11 venv created, dependencies installed
- [ ] llama.cpp built/downloaded with Metal, quarantine cleared
- [ ] `core/backend.py` Metal patch applied and `query_devices(strict=True)` works
- [ ] `config/app.json` paths and `metal` runtime set, `autotune.py` re-run
- [ ] Models in `~/Models`, voice models copied if voice is used
- [ ] Tests run, first login and a chat succeed
- [ ] Admin account created, bootstrap env vars unset
- [ ] API keys re-entered in Settings, Keychain "Always Allow" granted
- [ ] LaunchAgent loaded, `pmset` set, auto-login configured
- [ ] Caddy TLS in front, firewall allows only 443, port 8000 not exposed
- [ ] Backups scheduled and encrypted; PAN scrub done on migrated data
