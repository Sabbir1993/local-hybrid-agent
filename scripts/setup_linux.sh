#!/usr/bin/env bash
# One-time setup helper, Linux counterpart of setup_windows.ps1. Doesn't do
# anything you couldn't do by hand - review before running.
#
# Usage: ./scripts/setup_linux.sh [llama-bin-dir]            (default: ~/llama-vulkan; Python deps only)
#        DOWNLOAD=1 ./scripts/setup_linux.sh [llama-bin-dir]  also fetch llama.cpp, stable-diffusion.cpp and every model group
#        BACKEND=cuda (default vulkan), GROUPS_TO_FETCH="routing vision" (default all), MODELS_DIR=/path
# Also works on macOS (Metal build; BACKEND is ignored there): see SETUP_MACOS.md.
set -euo pipefail

install_dir="${1:-$HOME/llama-vulkan}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "This script will:"
echo "  1. Check for Python 3.10+ (with the venv module)"
echo "  2. Create $root/.venv and install the Python deps into it"
echo "     (hash-locked requirements.lock, falling back to requirements.txt)"
echo "  3. Check the Vulkan driver and point you at the llama.cpp build"
echo

py="$(command -v python3 || true)"
if [ -z "$py" ]; then
    echo "python3 not found. Install Python 3.10+ (e.g. sudo apt install python3 python3-venv)." >&2
    exit 1
fi
if ! "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 3.10+ is required, found $("$py" --version)." >&2
    exit 1
fi
echo "Found $("$py" --version)"

if [ ! -x "$root/.venv/bin/python" ]; then
    "$py" -m venv "$root/.venv" || {
        echo "venv creation failed - install python3-venv (Debian/Ubuntu) and retry." >&2
        exit 1
    }
fi
vpy="$root/.venv/bin/python"
"$vpy" -m pip install --upgrade pip
if ! "$vpy" -m pip install --require-hashes -r "$root/requirements.lock"; then
    echo "Locked install failed (lock was compiled on Windows) - falling back to requirements.txt"
    "$vpy" -m pip install -r "$root/requirements.txt"
fi

if [ "${DOWNLOAD:-0}" = "1" ]; then
    args=(${GROUPS_TO_FETCH:-routing vision embedder voice image} --binaries --backend "${BACKEND:-vulkan}"
          --bin-dir "$(dirname "$install_dir")" --write-config)
    [ -n "${MODELS_DIR:-}" ] && args+=(--models-dir "$MODELS_DIR")
    "$vpy" "$root/scripts/download_models.py" "${args[@]}" || echo "Some downloads failed - re-run the same command to resume."
    echo "Still manual: whisper-server (speech to text) - build it, see SETUP_LINUX.md 'Voice'."
    echo "Then: set gpu_devices in config/app.json and ./scripts/start.sh --port 8000"
    exit 0
fi

echo
if [ "$(uname)" = "Darwin" ]; then
    echo "macOS: llama.cpp uses Metal, no Vulkan driver needed."
elif command -v vulkaninfo >/dev/null 2>&1; then
    vulkaninfo --summary 2>/dev/null | grep -E "deviceName|driverName" || true
else
    echo "vulkaninfo not found - install vulkan-tools and mesa-vulkan-drivers (Intel Arc: Mesa ANV)."
fi

echo
echo "Next steps (manual):"
echo "  1. Get llama.cpp with Vulkan: https://github.com/ggml-org/llama.cpp/releases"
echo "     download llama-<build>-bin-ubuntu-vulkan-x64.tar.gz, or build it:"
echo "     cmake -B build -DGGML_VULKAN=ON && cmake --build build -j"
echo "  2. Put llama-server / llama-bench in: $install_dir"
echo "  3. $install_dir/llama-bench --list-devices   (one device per physical GPU)"
echo "  3b. Models: $vpy scripts/download_models.py --all --write-config"
echo "  4. Edit config/app.json: runtimes.<preset>.llama_bin_dir = \"$install_dir\","
echo "     models_dir, common_dir and media.*_models to Linux paths"
echo "  5. Keyring: API keys / MCP tokens need gnome-keyring or KWallet running and"
echo "     unlocked in the server's session. Do NOT install keyrings.alt (plaintext)."
echo "  6. ./scripts/start.sh --port 8000, then pick a model from the UI"
