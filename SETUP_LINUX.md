# Setup on a new Linux machine

Step-by-step commands to run after cloning the repo on a fresh Linux machine
(Ubuntu / Debian; other distros need their own package names). Replace every
`[BRACKETED]` value with your own. Never paste real passwords, API keys or
tokens into this file or any shared document.

A beginner-friendly version with the full model list and download URLs is in
[docs/INSTALL_LINUX.html](docs/INSTALL_LINUX.html).

Windows: see [SETUP_WINDOWS.md](SETUP_WINDOWS.md). Background and config
reference: [README.md](README.md), "Running on Linux".

---

## Prerequisites

- Python 3.10+ (3.11 matches `requirements.lock`)
- Intel Arc: kernel 6.2+ and Mesa's ANV Vulkan driver (installed in step 1).
  NVIDIA: proprietary driver + CUDA toolkit instead.

---

## Steps

**1. Install system packages**
```bash
sudo apt update
```
```bash
sudo apt install -y git unzip python3 python3-venv mesa-vulkan-drivers vulkan-tools gnome-keyring dbus-user-session
```

**2. Check that the Vulkan driver sees the GPUs**
```bash
vulkaninfo --summary
```

**3. Clone the repo**
```bash
git clone [REPO_URL]
```

**4. Enter the folder**
```bash
cd a770-dual-runtime
```

**5. Create `.venv`**
```bash
python3 -m venv .venv
```

**6. Install the Python dependencies.** Use the hash-locked `requirements.lock`,
falling back to `requirements.txt` if a hash was pinned to a Windows-only wheel.
```bash
.venv/bin/python -m pip install --upgrade pip
```
```bash
.venv/bin/python -m pip install --require-hashes -r requirements.lock || .venv/bin/python -m pip install -r requirements.txt
```

**7. Get llama.cpp.** Download `llama-<build>-bin-ubuntu-vulkan-x64.tar.gz` from
https://github.com/ggml-org/llama.cpp/releases (or let step 9b do it):
```bash
wget -P ~/Downloads [LLAMA_RELEASE_URL]
```
```bash
mkdir -p ~/llama-vulkan && tar -xzf ~/Downloads/[LLAMA_ARCHIVE_NAME].tar.gz -C ~/llama-vulkan
```

Or build it from source instead (NVIDIA: `-DGGML_CUDA=ON`):
```bash
sudo apt install -y build-essential cmake libvulkan-dev glslc
```
```bash
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
```
```bash
cmake -S ~/llama.cpp -B ~/llama.cpp/build -DGGML_VULKAN=ON
```
```bash
cmake --build ~/llama.cpp/build --config Release -j
```

**8. Find where `llama-server` ended up.** The zip may unpack into a subfolder
such as `build/bin`. Use that folder as `llama_bin_dir`.
```bash
find ~/llama-vulkan ~/llama.cpp -name llama-server 2>/dev/null
```

**9. Check that the GPUs are visible.** Note the device indices.
```bash
[LLAMA_BIN_DIR]/llama-bench --list-devices
```

**9b. Download the models and helper programs.** One command fetches the models
(`routing` executor, `vision` image reader, `embedder`, `voice` speech-to-text + spoken replies, `image` local image maker;
about 20 GB in total, pick groups to save disk) and, with `--binaries`, the llama.cpp and stable-diffusion.cpp builds
(steps 7-8 can be skipped then). `--write-config` points `config/app.json` at everything. Re-running skips what exists
and resumes partial files.
```bash
.venv/bin/python scripts/download_models.py
```
```bash
.venv/bin/python scripts/download_models.py --all --binaries --backend vulkan --write-config
```
The CPU routers (`laya`, `cactus-needle`) download from Hugging Face on first use, or with the `routing` group.

**The three helper programs at a glance** (the app finds each by folder name, so keep them side by side, for example in `~`):

| Program | What it does | How to get it | Folder the app looks in |
|---|---|---|---|
| `llama-vulkan` (llama-server, llama-bench) | runs every text / vision / embedding model | step 7, or `download_models.py --binaries` (release asset `llama-<build>-bin-ubuntu-vulkan-x64.tar.gz`) | `runtimes.<name>.llama_bin_dir` |
| `sd-vulkan` (sd-server) | local image maker | `download_models.py --binaries --backend vulkan` (release asset `sd-master-<hash>-bin-Linux-Ubuntu-24.04-x86_64-vulkan.zip` of https://github.com/leejet/stable-diffusion.cpp/releases; needs Ubuntu 24.04 or newer) | `runtimes.<name>.sd_bin_dir`, else `sd-vulkan` next to the llama folder |
| `whisper-vulkan` (whisper-server) | speech to text | build once, see **Voice** below (no Vulkan release exists) | `runtimes.<name>.whisper_bin_dir`, else `whisper-vulkan` next to the llama folder |

If `--binaries` cannot read the GitHub release list (offline or rate-limited), it prints the release page to use by hand:
unpack the archive into the folder named above and `chmod +x` the `*-server` and `*-bench` files. Check each program with
`[FOLDER]/llama-server --version`, `[FOLDER]/sd-server --help` and `[FOLDER]/whisper-server --help`.

**Voice (speech to text).** whisper.cpp publishes no Vulkan build (its Linux release is CPU only), so build `whisper-server` once.
The build tools are the same as for building llama.cpp:
```bash
sudo apt install -y build-essential cmake libvulkan-dev glslc
```
```bash
git clone https://github.com/ggml-org/whisper.cpp ~/whisper.cpp
```
A static build keeps `whisper-server` self-contained, so copying it into another folder works:
```bash
cmake -S ~/whisper.cpp -B ~/whisper.cpp/build -DGGML_VULKAN=1 -DBUILD_SHARED_LIBS=OFF && cmake --build ~/whisper.cpp/build -j --config Release
```
```bash
mkdir -p ~/whisper-vulkan && cp -r ~/whisper.cpp/build/bin/* ~/whisper-vulkan/ && ~/whisper-vulkan/whisper-server --help | head -3
```
The server finds `whisper-server` in a `whisper-vulkan` folder next to your llama.cpp folder, or in
`runtimes.<name>.whisper_bin_dir`. Spoken replies need no extra program: `sherpa-onnx` is in `requirements.txt`.

**10. Edit the config.** `--write-config` already set `models_dir`, `media_dirs` and the
`llama_bin_dir` / `sd_bin_dir` paths. Still yours: `common_dir` and `gpu_devices`.

Your settings live in `config/app.json`, which is created from `config/app.example.json` on the first start (or copy it
yourself: `cp config/app.example.json config/app.json`). The same goes for `model_configs.json`, `roles.json`,
`sampling_config.json`, `fx_rates.json` and `web_blocked.json`. These local files are git-ignored, so your edits (and the
model settings the app saves) never block a `git pull`. The example comes from a Windows PC: replace every Windows path with
a Linux one; this lists what is left:
```bash
grep -n 'E:' config/app.json
```
```bash
nano config/app.json
```
- `gpu_devices`: use `"auto"` (every discrete GPU is used, whatever their number: 1, 2, 3 or 4). A fixed list such as
  `[0, 1]` also works; the numbers are the ones step 9 printed, and they differ from Windows (an Intel iGPU often takes index 0
  there but not on Linux). At startup the log warns when a listed index does not exist or a GPU is left unused.
- If the server shows fewer GPUs than the machine has, look for `GPU scan FAILED` or `GPU scan skipped` in the start log. It means
  `llama_bin_dir` is wrong for Linux (a Windows `E:/...` path, or a folder without `llama-bench`), so `"auto"` could not list the
  GPUs and fell back to the Windows default `[1, 2]`. Fix the path, check `[LLAMA_BIN_DIR]/llama-bench --list-devices` lists every
  GPU, then restart. The same error is shown in Settings under the GPU status (`scan_error`).
- After the first start, open Settings -> Models and re-check the GPU of each helper model: the shipped file names Windows
  indices (1 and 2), and a GPU that does not exist here is moved to the default one automatically.
- `runtime`: the preset to use (`vulkan`, or `cuda` for NVIDIA).

For example:
```json
"models_dir": "/home/[USER]/Models",
"common_dir": "/home/[USER]/common",
"runtimes": {
  "vulkan": { "llama_bin_dir": "[LLAMA_BIN_DIR]", "backend": "vulkan", "gpu_devices": "auto", "small_model_gpu": 0 }
}
```

**10b. Create the data folders and set their permissions.** The server runs as a normal user (never root) and creates what it
needs, but it can only do that where that user may write. `common_dir` holds every user's generated files and uploads
(one private `user_<id>/` sub-folder each, created on first use), so it may contain customer data: keep it private.
```bash
mkdir -p [COMMON_DIR] && chmod 700 [COMMON_DIR]
```
If the folder is on another disk or under `/srv` or `/mnt`, create it with `sudo` once and hand it to the server's user:
```bash
sudo mkdir -p [COMMON_DIR] && sudo chown "$USER":"$USER" [COMMON_DIR] && sudo chmod 700 [COMMON_DIR]
```
Rules of thumb:
- the server user needs **write** access to the repo folder (databases, `config/`, `knowledge_uploads/`), `common_dir`,
  `workspace_dir` and the folder the downloader fills (`media_dirs`, model folders for downloads), and **read + execute**
  on the `llama-vulkan`, `sd-vulkan` and `whisper-vulkan` folders;
- the model folder only needs read access once the models are downloaded;
- a separate Unix account per app user is **not** needed: per-user separation is done by the app (`user_<id>/`);
- if you run the server under systemd with `ProtectHome` or `ProtectSystem=strict`, add the folders above to
  `ReadWritePaths=`;
- no other local account should be able to read `common_dir` or the `*.db` files (`chmod 600 *.db`, see Databases).

**11. Check the keychain backend.** It should print `SecretService`, not
`fail` or `keyrings.alt`. On a headless server, run this inside the
`dbus-run-session` from step 13.
```bash
.venv/bin/python -c "import keyring; print(keyring.get_keyring())"
```

**12. Optional: run the test suite**
```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

**13. Start the server.** On first run it asks you to create the super-admin
account; save the password.

On a desktop session:
```bash
.venv/bin/python server_manager.py --port 8000
```

On a headless server, start it with an unlocked keyring:
```bash
dbus-run-session -- sh -c 'printf "%s" "[KEYRING_PASSWORD]" | gnome-keyring-daemon --unlock --components=secrets && .venv/bin/python server_manager.py --port 8000'
```

To reach it from other machines, bind to all interfaces (see Security below
first):
```bash
.venv/bin/python server_manager.py --host 0.0.0.0 --port 8000
```

**14.** Open `http://[SERVER_IP]:8000`, log in, then enter your cloud API keys
and connect MCP servers in Settings.

**14b. Web search (works out of the box; check it).** Ask something recent with the globe in the chat box turned on
("latest induction cooker prices in Bangladesh"). The answer should list sources with a line such as
`via DuckDuckGo + Bing`. Product questions also show picture cards (pictures are fetched by the server through
`/media/product-image`, so the server needs outbound HTTPS to the shops). Optional Google/SearXNG upgrade:
see "Web search engines" below.

**15. Setup the Code Runner Sandbox (Bubblewrap / Isolated Runner)**
The chat offers `run_sandbox_python` for isolated data analysis, custom charts,
and calculations. On Linux, execution uses unprivileged **Bubblewrap (`bwrap`)**
namespaces:

1. Install Bubblewrap and sandbox Python packages:
```bash
sudo apt install -y bubblewrap python3-pip
pip install -r sandbox-requirements.txt --break-system-packages
```
2. Test the sandbox containment and isolation:
```bash
python3 tools/runner/verify.py
```
3. In `config/app.json`:
   - For a local runner on the same box: set `"runner_backend": "local"` (or `"wsl2"` if running inside WSL2).
   - For an isolated dedicated VM: run `python3 tools/runner/runner_server.py serve` on the runner VM, then pair in Settings -> Code Runner.

---

## Web search engines (optional: add Google)

Web search asks a local SearXNG first (Google, Brave, Startpage, Mojeek, Wikipedia, Bing, DuckDuckGo in one request) and
scrapes DuckDuckGo and Bing when it is not running. The shipped `config/app.json` already points at
`http://localhost:8888`; with nothing listening there, search simply uses the fallback. No Docker needed.

The steps below work on a plain Linux server and inside WSL. The server stays in the foreground: run it in `tmux`, or as a
systemd service for the server's user.

```bash
sudo apt install -y python3-venv python3-dev build-essential git libxslt-dev zlib1g-dev libffi-dev libssl-dev
```
```bash
git clone https://github.com/searxng/searxng ~/searxng && cd ~/searxng && python3 -m venv .venv
```
```bash
.venv/bin/pip install -U pip setuptools wheel pyyaml msgspec typing_extensions && .venv/bin/pip install --use-pep517 --no-build-isolation -e .
```

Create `~/searxng/settings.yml` (use your own random secret, never commit it):
```yaml
use_default_settings: true
server:
  bind_address: "127.0.0.1"
  port: 8888
  secret_key: "[SEARXNG_SECRET]"
  limiter: false
search:
  formats: [html, json]
engines:
  - name: google
    disabled: false
  - name: bing
    disabled: false
  - name: duckduckgo
    disabled: false
```
```bash
SEARXNG_SETTINGS_PATH=~/searxng/settings.yml ~/searxng/.venv/bin/python -m searx.webapp
```
Then add a `web` section to `config/app.json` and restart the app:
```json
"web": { "searxng_url": "http://127.0.0.1:8888" }
```
SearXNG is asked first; DuckDuckGo and Bing are used when it returns fewer than `web.searxng_min_results` (5) hits
or is down. **Check that Google really answers** before relying on it:
```bash
curl -s "http://127.0.0.1:8888/search?q=test&format=json&engines=google" | head -c 300
```
If `unresponsive_engines` says `access denied` / `Suspended`, Google is blocking that machine's IP (tested on the
Arc server's connection: HTTP 403 on the first request). SearXNG then still serves its other engines (Brave,
DuckDuckGo), but nothing comes from Google; a different network or a proxy may help, or use Google's paid
Programmable Search API instead (not built in yet). Search queries leave your network
(card-like numbers and e-mail addresses are stripped first); keep SearXNG bound to `127.0.0.1` or the LAN only.
On Windows run SearXNG inside WSL2 (same steps) and use the WSL address in `searxng_url`.

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

**Scripted first start:** set these before step 13 and the admin account is
created without a prompt. The leading space keeps the line out of bash
history (with the default `HISTCONTROL`). Clear them afterwards.
```bash
 export A770_BOOTSTRAP_USERNAME=[ADMIN_USER] A770_BOOTSTRAP_PASSWORD=[ADMIN_PASSWORD]
```
```bash
unset A770_BOOTSTRAP_USERNAME A770_BOOTSTRAP_PASSWORD
```

**To keep data from an old machine:** stop the server on both machines, then
copy these before the first start:
- `*.db`, plus any `*.db-wal` / `*.db-shm` next to them
- `knowledge_uploads/`, `compacts/`, `config/providers/`
- your `common_dir` folder

The files work on both Windows and Linux. For example, from the old machine:
```bash
scp auth.db projects.db memory.db usage.db [USER]@[SERVER_IP]:~/a770-dual-runtime/
```

Then lock down permissions on the new machine:
```bash
chmod 600 ~/a770-dual-runtime/*.db
```

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
- Do **not** install `keyrings.alt`; it stores secrets in a plaintext file.
  The server logs `WARNING: insecure keyring backend` at startup if no OS
  keychain is available.
- Run the server as a normal user, not root.
- **LAN access:** allow the port only from your LAN. If it is reached from
  outside the host, put TLS in front (nginx or Caddy).
```bash
sudo ufw allow from [LAN_SUBNET] to any port 8000 proto tcp
```
- Run only one server instance per machine; a second one kills the running
  llama servers.

---

## Linux differences

- The GPU panel shows VRAM in use per Vulkan device but no per-process
  compute %. Cards using under 1 GB (idle) are hidden, same as on Windows.
- On a headless box the "browse folder" button returns nothing; type the path
  instead.
