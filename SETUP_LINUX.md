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
- `runtime`: the single preset selected before startup (`vulkan`, `cuda` for
  NVIDIA, or `sycl` for an Intel oneAPI/Level Zero llama.cpp build). Changing
  it requires a restart. See [SYCL Linux installation](docs/SYCL_LINUX_INSTALL.md)
  for the `/var/web/html_new/local_ai/llama-sycl` deployment.

For example:
```json
"models_dir": "/home/[USER]/Models",
"common_dir": "/home/[USER]/common",
"runtimes": {
  "vulkan": { "llama_bin_dir": "[LLAMA_BIN_DIR]", "backend": "vulkan", "gpu_devices": "auto", "small_model_gpu": 0 },
  "sycl": { "llama_bin_dir": "/var/web/html_new/local_ai/llama-sycl", "backend": "sycl", "gpu_devices": "auto", "small_model_gpu": 0,
            "env": { "ONEAPI_DEVICE_SELECTOR": "level_zero:gpu", "SYCL_CACHE_PERSISTENT": "1", "SYCL_CACHE_DIR": "/var/cache/local-ai/sycl" } }
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

SearXNG is installed in `/opt/searxng` and runs as its own `searxng` user with no login, as a systemd service. Keep
it **outside** the app folder and **outside** the app's venv: it pins its own library versions, and a separate user
means a flaw in SearXNG cannot read the app, `config/app.json` or the database.

**1. System packages** (run in a normal shell, not inside any venv; run `deactivate` first if one is active):
```bash
sudo apt install -y python3-venv python3-dev build-essential git libxslt-dev zlib1g-dev libffi-dev libssl-dev
```

**2. Service user and source:**
```bash
sudo useradd --system --create-home --home-dir /opt/searxng --shell /usr/sbin/nologin searxng
```
```bash
sudo -u searxng git clone https://github.com/searxng/searxng /opt/searxng/src
```

**3. SearXNG's own venv.** `/opt/searxng` is private to `searxng`, so `cd /opt/searxng/src` as your own user
fails with *Permission denied*. That is expected; run the whole step as `searxng` (do not `chmod` the folder, it
holds the secret key):
```bash
sudo -u searxng -H bash -c 'cd /opt/searxng/src && python3 -m venv .venv && .venv/bin/pip install -U pip setuptools wheel pyyaml msgspec typing_extensions && .venv/bin/pip install --use-pep517 --no-build-isolation -e .'
```

**4. Settings** (a random secret is generated; never commit or share it):
```bash
sudo -u searxng tee /opt/searxng/settings.yml >/dev/null <<EOF
use_default_settings: true
server:
  bind_address: "127.0.0.1"
  port: 8888
  secret_key: "$(openssl rand -hex 32)"
  limiter: false
search:
  formats: [html, json]
EOF
sudo chmod 600 /opt/searxng/settings.yml
```

**5. Check port 8888 is free** (no output = free). If it is taken, change `port` in `settings.yml` and
`web.searxng_url` in `config/app.json` to a free port, e.g. 8890:
```bash
sudo ss -ltnp | grep ':8888'
```

**6. systemd service.** This only adds a new unit file; `daemon-reload` does not restart any running service.
```bash
sudo tee /etc/systemd/system/searxng.service >/dev/null <<'EOF'
[Unit]
Description=SearXNG
After=network-online.target
[Service]
User=searxng
WorkingDirectory=/opt/searxng/src
Environment=SEARXNG_SETTINGS_PATH=/opt/searxng/settings.yml
ExecStart=/opt/searxng/src/.venv/bin/python -m searx.webapp
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/searxng
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now searxng
```
```bash
systemctl status searxng --no-pager
```
It should say `active (running)`. If not: `sudo journalctl -u searxng -n 50 --no-pager`.

**7. Test which engines answer from this server:**
```bash
for e in google startpage brave mojeek bing duckduckgo qwant; do printf '%-11s ' $e; curl -s "http://127.0.0.1:8888/search?q=bangladesh+bank&format=json&engines=$e" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d["results"]), "results", d["unresponsive_engines"] or "")'; done
```
`access denied` / `Suspended` means that engine blocks the server's IP. On the Arc server (2026-11) Google and Qwant
were blocked; Startpage (serves Google's results), Brave, Mojeek, Bing and DuckDuckGo answered. Do not try to get
around a block with proxies; if real Google results are required, the approved route is Google's paid search API
(not built in yet).

**8. `config/app.json`.** List only the engines that answered in step 7 under `searxng_engines`, so blocked ones are
never asked:
```json
"web": {
  "searxng_url": "http://localhost:8888",
  "backends": ["searxng", "duckduckgo", "bing"],
  "searxng_min_results": 5,
  "searxng_engines": ["startpage", "brave", "mojeek", "bing", "duckduckgo"],
  "expand_queries": true,
  "max_results": 10,
  "auto_fetch_top": 5,
  "boost_hosts": ["bb.org.bd", "gov.bd"],
  "allow_hosts": ["wikipedia.org", "bb.org.bd", "gov.bd", "pcisecuritystandards.org"]
}
```
```bash
python3 -m json.tool config/app.json >/dev/null && echo OK
```
Restart the app. Order of use: SearXNG first; DuckDuckGo and Bing are scraped directly only when SearXNG is down or
returns fewer than `searxng_min_results` hits.

- `max_results`: keep it around 10. The tool output has a fixed size cap, so 25 results only crowd out the
  excerpts of the pages actually read and slow local models.
- `allow_hosts`: every site is allowed already. This list only stops the app from skipping a site for 24 h (1 h
  after HTTP 429) once it has refused twice. Use it for important sites that are sometimes slow or briefly refuse
  (gov.bd). Do not list sites that always refuse (paywalled news, social media): each search would keep trying them.
  Already-skipped sites are in `config/web_blocked.json`.
- Host names are matched as a domain plus all its subdomains. Write `sslcommerz.com`, **not** `*.sslcommerz.com`
  (the `*` form matches nothing). Internal/private addresses stay blocked by the network guard either way.
- `boost_hosts` ranks those sites higher; `allow_hosts` does not.

Search queries leave your network (card-like numbers and e-mail addresses are stripped first). Keep SearXNG bound to
`127.0.0.1`; if the app runs on another machine, firewall port 8888 to the app server only.
On Windows run SearXNG inside WSL2 (same steps) and use the WSL address in `searxng_url`.

Re-check Google later (add `"google"` back to `searxng_engines` if it returns results):
```bash
curl -s 'http://127.0.0.1:8888/search?q=test&format=json&engines=google' | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d["results"]), d["unresponsive_engines"])'
```
Update SearXNG: `sudo -u searxng -H bash -c 'cd /opt/searxng/src && git pull && .venv/bin/pip install --use-pep517 --no-build-isolation -e .' && sudo systemctl restart searxng`

Remove it: `sudo systemctl disable --now searxng && sudo rm /etc/systemd/system/searxng.service && sudo systemctl daemon-reload && sudo userdel -r searxng`

## Voice collection page (optional)

A public page, `https://<your-domain>/voice?k=<code>`, where colleagues read lines aloud for the Bangla speech corpus.
Recordings and their database go to a folder **outside** the app, so it can be copied to the training PC as a whole.
It needs no web-server change: the page is the app's own page and its API is under `/media/voice-collect/`, which an nginx
in front already forwards along with the rest of `/media`.

1. Create the folder, owned by the user the app runs as, readable only by that user:
```bash
sudo mkdir -p /srv/ssl-voice-corpus && sudo chown "$USER": /srv/ssl-voice-corpus && sudo chmod 700 /srv/ssl-voice-corpus
```
2. In `config/app.json` add (or edit) the block below, then restart the app:
```json
"speech_corpus": { "enabled": false, "dir": "/srv/ssl-voice-corpus" }
```
3. In **Settings → Voice Collection** (needs the `speech.manage` permission; admins have it):
   - **Create link**, then copy it. It is shown once; creating a new one stops the old link.
   - Import the lines people read: `scripts/stt_corpus_prompts/starter.jsonl` to start, or your own `.jsonl`
     (`{"text": "...", "category": "standard|domain|banglish|numbers"}` per line). Lines with card-like numbers are refused.
   - Switch **Enable voice collection page** on. Switching it off makes the page and its API answer "not found" at once.

Each person enters name, employee ID and how they speak at home, then records 15 random lines. Clips are stored as
`<dir>/audio/<name>_<employee_id>/<id>.wav` (16 kHz mono 16-bit) and listed in `<dir>/corpus.db`. To delete a
person's recordings, use **Delete a speaker** in the same panel. That needs a fresh two-factor check.

To copy the corpus to the training PC (safe while the app runs):
```bash
sqlite3 /srv/ssl-voice-corpus/corpus.db ".backup /tmp/corpus-copy.db"
```
Then copy `/tmp/corpus-copy.db` and `/srv/ssl-voice-corpus/audio/` over SSH (`scp -r` or `rsync`). The recordings are
personal data: never put them in git or a cloud service.

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

## Serving 15 users: Qwen3.8-27B on 2x Arc Pro B70 (option A)

One llama-server across two 32 GB Arc Pro B70 cards serves 15 people at once. A third 32 GB card holds the helper
models. Each chat can grow to 128K tokens; the 15 chats share one KV pool sized for realistic use (3-4 chats at 128K
plus the rest at ~40K).

**Hardware and OS**
- Both main cards in CPU-attached slots, Resizable BAR on, PSU sized for three cards, 128 GB RAM.
- Recent kernel with `xe` support for Battlemage, Intel compute-runtime / Level Zero, oneAPI runtime (SYCL), recent Mesa.
- Swap off or encrypted, core dumps off: the prompt cache keeps users' conversations in RAM.
- `llama-bench --list-devices` shows three ~32 GB devices. Note the index of the helper card (2 below).

**Measure before going live** (spare port 8099, nothing else on the cards):
1. SYCL vs Vulkan with `llama-batched-bench` at 1, 4, 8 and 15 parallel users:
   `python -m core.llama_bench --batched -m <model> -npl 1,4,8,15 -npp 1024 -ntg 128 -ctk q8_0 -ctv q8_0 -fa on -kvu`
   (add `--bin-dir` / `-dev` for the other backend). Check the log has no "Flash Attention ... assigned to device CPU".
   Watch total decode tok/s from 8 to 15 users. On the A770 box (2026-11-27, b12810, Qwen3.5-4B Q4_K_M, one card):
   Vulkan 60 / 148 / 164 / 96 tok/s at 1 / 4 / 8 / 15 users; SYCL 54 / 64 / 65 / 67. SYCL barely scales for
   generation; Vulkan drops past 8 users. If the B70 shows the same drop, two servers with 8 users each (option B)
   can beat one server with 15.
2. MTP draft on/off and draft tokens 2/3/4 at 15 users; ubatch 256/512.
3. KV cache quality f16 vs q8_0 on 20 fixed Bangla + English prompts, plus a fact placed at ~100K tokens.
4. Fill the pool to its planned size: each card must keep ≥ 1 GiB free.

**Model Config (Settings → Model Config, Qwen3.8-27B-Q4_K_M)**

| Field | Value | Why |
|---|---|---|
| GPU devices / split | cards 0 and 1, `layer`, `1,1` | the helper card stays out |
| Context size | `1000000` | the shared pool for all slots (q8_0: ~1.0M fits next to the weights) |
| Parallel slots | `16` | 15 users + 1 helper slot |
| Unified KV / Per-slot cap | on / `131072` | each chat up to 128K |
| KV cache / Flash attention | `q8_0` / `on` | quantized KV needs flash attention |
| MTP draft | on (draft tokens from step 2) | |
| Cache reuse | `0` | hybrid model: llama.cpp cannot use it (the app leaves it out anyway) |
| Checkpoints / slot | `0` (automatic = 4) | ~150 MiB each in RAM: 16 x 4 x 150 MiB ≈ 9.6 GB |
| Prompt RAM (MiB) | `24576` | restores a chat in ~1 s instead of re-reading it |
| Requests per user / Helper slot | `1` / On | |

The preflight shows weights, KV, recurrent state, MTP and compute per card. Load only when it fits.

**`config/app.json` on that box** (merge into the existing sections)

```json
{
  "runtime": "sycl",
  "runtimes": { "sycl": { "gpu_devices": [0, 1], "small_model_gpu": 2,
    "env": { "ONEAPI_DEVICE_SELECTOR": "level_zero:gpu", "GGML_SYCL_ENABLE_VMM": "0",
             "SYCL_CACHE_PERSISTENT": "1", "SYCL_CACHE_DIR": "/var/cache/local-ai/sycl" } } },
  "serving": { "queue_enabled": true, "slot_affinity": true, "max_inflight_per_user": 1, "helper_slot": 1 },
  "supervision": { "health_timeout_s": 600 },
  "small_models": {
    "executor": { "gpu": 2, "fallback_enabled": false },
    "vision":   { "gpu": 2, "fallback_enabled": false },
    "embedder": { "gpu": 2 }
  }
}
```

- `gpu_devices: [0, 1]`, not `"auto"`: auto would put the main model on all three cards.
- Helper lanes on card 2 with `fallback_enabled: false`, so titles, search rewrites, summaries, the input guard and
  answer checks never land on the main model. Set the same `gpu: 2` for Whisper and image models.
- The first SYCL load compiles kernels for several minutes (hence the 600 s limit); later loads use the cache.

**Static files through nginx**: serve the built frontend directly and keep the app for API calls:

```nginx
location /assets/ {
    alias [APP_FOLDER]/frontend/dist/assets/;   # what routes/pages.py serves at /assets/
    expires 30d;
    add_header Cache-Control "public, immutable";
}
```

**Roll out**
1. Run `scripts/load_test_chat.py` (see `--help`) with 5, 10, then 15 test users. Pass: no errors, no "context full",
   queue wait under 2 s, at least 15 tok/s each at 15 users.
2. Pilot for a week. Watch the dashboard's serving section: queue depth, wait and time to first token, pool use,
   per-slot context fill, VRAM per card, RAM.

**What the app does for 15 users**
- **Sticky slots** (`core/serving_slots.py`): each user keeps a slot, so their cached conversation survives other
  users' turns.
- **Helper slot**: the app's own calls to the main model (image reading, keepalive, any job without a helper lane) use
  the last slot.
- **Pool guard**: when the running chats would overflow the shared pool, the newest long one waits with a message
  ("Waiting for model memory"). Without it, llama-server fails every running request ("Context size has been
  exceeded").
- **Shared reads**: the queue position updates while waiting, and telemetry and `/slots` are computed once per tick
  for all viewers.

### Several servers: one llama-server per GPU group (option B)

**Settings → Model Config → Server mode → Several servers** runs a full copy of the main model on each GPU group,
and the app sends each chat to the least busy copy. Each server gets at least one GPU, no GPU is shared, and each has
its own parallel slot count. Server 1 uses port 8090, and servers 2-8 use 8191-8197 (127.0.0.1 only; helper lanes
cannot take 8190-8199). The list is saved per runtime, because Vulkan and SYCL number the GPUs differently. One server
stays the default.

**When to use it:** many people generate at the same time and the model fits on one card. With `-sm layer`, one
server across two cards uses only one card at a time while generating; two servers generate on both at once.

Measured on the A770 box (2026-11-28, Vulkan, Qwen3.5-9B Q4_K_M, 1,024-token prompts, `llama-batched-bench`),
total generation tok/s:

| Users | One server, 2 cards | Two servers, 1 card each |
|---|---|---|
| 4 | 95 | 153 |
| 8 | 111 | 261 |
| 16 | 80 | 310 |

Prompt reading gains less (about 1.15-1.5x). One user is not faster.

**Trade-offs:** each server holds its own copy of the weights, so each chat has less KV room than in one shared pool.
Context size is per server. A chat moved to another server reads its prompt once more.

**Example for the B70 box** (helper card = GPU 2): server 1 on GPU 0 with 8 slots, server 2 on GPU 1 with 7 slots.

**What the app does**
- **Routing** (`core/main_router.py`): a chat stays on its server and slot, keeping its prompt cache. When its server
  is full it moves to the one with free slots. When all are full it queues, and the first freed slot takes it.
  Background jobs go to the least busy server.
- **Lifecycle** (`core/main_server.py`, `core/state.py`): every server's VRAM is checked before any starts. Servers
  start one at a time; on SYCL the second reuses the kernel cache the first compiled. If server 1 crashes, only it
  restarts and the others keep answering. If another server crashes, only that one restarts, with its own crash-loop
  limit.
- **Status**: Settings shows each server's state, and the dashboard shows slots per server. `/control/log?instance=N`
  shows one server's log, where N is 0-based.

**Check on the Linux box before going live**
1. Run `llama-batched-bench` on one card at 8 users, then run two at the same time, one per card. Two servers are
   worth it only if the total is clearly higher.
2. Pick Several servers, apply, and load. The log shows `launching main server #2 on port 8191` and both servers become
   healthy. `ss -ltnp | grep -E '8090|8191'` lists both.
3. Run `scripts/load_test_chat.py` at 15 users and confirm both servers get work in the dashboard.
4. Kill server 2 (`kill <pid>` from Settings → status). Chats keep answering on server 1, and server 2 comes back
   within a few seconds.

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
