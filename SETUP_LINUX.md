# Setup on a new Linux machine

Step-by-step commands to run after cloning the repo on a fresh Linux machine
(Ubuntu / Debian; other distros need their own package names). Replace every
`[BRACKETED]` value with your own. Never paste real passwords, API keys or
tokens into this file or any shared document.

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

**5. Make the scripts executable** (only needed until they are committed with
`git add --chmod=+x`)
```bash
chmod +x scripts/start.sh scripts/setup_linux.sh
```

**6. Create `.venv` and install the Python dependencies.** It installs the
hash-locked `requirements.lock`, falling back to `requirements.txt` if a hash
was pinned to a Windows-only wheel.
```bash
./scripts/setup_linux.sh ~/llama-vulkan
```

**7. Get llama.cpp.** Download `llama-<build>-bin-ubuntu-vulkan-x64.zip` from
https://github.com/ggml-org/llama.cpp/releases:
```bash
wget -P ~/Downloads [LLAMA_RELEASE_ZIP_URL]
```
```bash
unzip ~/Downloads/[LLAMA_ZIP_NAME].zip -d ~/llama-vulkan
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

**10. Edit the config.** Change every `E:/...` path to a Linux path:
`models_dir`, `common_dir`, `runtimes.vulkan.llama_bin_dir`, `gpu_devices`
and `media_dirs`.
```bash
nano config/app.json
```

For example:
```json
"models_dir": "/home/[USER]/Models",
"common_dir": "/home/[USER]/common",
"runtimes": {
  "vulkan": { "llama_bin_dir": "[LLAMA_BIN_DIR]", "backend": "vulkan", "gpu_devices": [0, 1], "small_model_gpu": 0 }
}
```

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
./scripts/start.sh --port 8000
```

On a headless server, start it with an unlocked keyring:
```bash
dbus-run-session -- sh -c 'printf "%s" "[KEYRING_PASSWORD]" | gnome-keyring-daemon --unlock --components=secrets && ./scripts/start.sh --port 8000'
```

To reach it from other machines, bind to all interfaces (see Security below
first):
```bash
./scripts/start.sh --host 0.0.0.0 --port 8000
```

**14.** Open `http://[SERVER_IP]:8000`, log in, then enter your cloud API keys
and connect MCP servers in Settings.

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
