import json
import os
import sys
from pathlib import Path

# Port & Network Settings
PROXY_HOST = "0.0.0.0"
PROXY_PORT = 8000
LLAMA_SERVER_PORT = 8090  # internal port, not exposed directly
HEALTH_TIMEOUT_S = 120
WATCHDOG_INTERVAL_S = 5
MAX_RESTART_BACKOFF_S = 60

# File Paths
BASE_DIR = Path(__file__).resolve().parent.parent
UI_FILE = BASE_DIR / "ui.html"
STATIC_DIR = BASE_DIR / "static"
USAGE_DB_FILE = BASE_DIR / "usage.db"
PROJECTS_DB_FILE = BASE_DIR / "projects.db"
MEMORY_DB_FILE = BASE_DIR / "memory.db"
AUTH_DB_FILE = BASE_DIR / "auth.db"       # users, roles, permissions, sessions, audit log
KNOWLEDGE_UPLOADS_DIR = BASE_DIR / "knowledge_uploads"  # uploaded org knowledge-base files
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "app.json"          # main app config (models_dir, lanes, capabilities, ...)
ROLES_FILE = CONFIG_DIR / "roles.json"         # multiagent role definitions (planner/coder/reviewer)
MODEL_CONFIGS_FILE = CONFIG_DIR / "model_configs.json"
PROVIDERS_FILE = CONFIG_DIR / "providers.json"   # legacy shared file -- migrated into PROVIDERS_DIR on first boot
PROVIDERS_DIR = CONFIG_DIR / "providers"         # per-user cloud providers + lane bindings: providers/user_<id>.json

# Cloud (OpenAI-compatible) lanes
CLOUD_LANES = ("main", "executor", "vision")
CLOUD_TIMEOUT_S = 300.0    # streaming completions can be long
CLOUD_PROBE_TIMEOUT_S = 45.0

KEEPALIVE_INTERVAL_S = 25   # 1-token ping while idle; WDDM demotes VRAM ~70s after idle
GPU_QUERY_INTERVAL_S = 4.0  # perf-counter queries are slow; cache results

IGNORED_IGPU_LUIDS = {"0x000165f7", "0x0001665a"}

# Defaults used when a profile/config field is empty or deleted by the user
CONFIG_DEFAULTS = {
    "context_size": 32768,
    "n_gpu_layers": 999,
    "threads": 0,
    "threads_batch": 0,
    "batch_size": 2048,
    "ubatch_size": 512,
    "n_slots": 1,
    "tensor_split": "9,11",
    "split_mode": "layer",
    "flash_attn": "auto",
    "kv_cache_type": "f16",
    "kv_unified": False,       # -kvu: one KV pool shared by all slots (idle slots reserve nothing)
    "kv_unified_per_slot": 0,  # cap one conversation's share of the unified pool (0 = no cap)
    "cache_reuse": 256,        # --cache-reuse: min chunk to reuse from cache via KV shifting
    "cache_ram": 0,            # -cram MiB host-RAM prompt cache (0 = llama default 8192)
    "keepalive_interval_s": 25,
    "gpu_devices": [1, 2],
    "llama_bin_dir": "E:\\AI\\vulkan-arc\\llama-vulkan",
    "backend": "vulkan",  # "vulkan" or "cuda" - selects -dev prefix + visible-devices env var
}

# Machine-level llama.cpp runtime. config/app.json holds named presets:
#   "runtime": "vulkan",
#   "runtimes": {"vulkan": {"llama_bin_dir": ..., "backend": "vulkan", "gpu_devices": [1, 2]},
#                "cuda":   {"llama_bin_dir": ..., "backend": "cuda",   "gpu_devices": [0]}}
# LLAMA_RUNTIME=<name> in the environment overrides "runtime". The active
# preset replaces the llama_bin_dir/backend/gpu_devices defaults and wins over
# per-model values, since device numbering is a property of the machine.
RUNTIME_KEYS = ("llama_bin_dir", "backend", "gpu_devices")


def _load_runtime() -> dict:
    name, preset = CONFIG_DEFAULTS["backend"], {}
    try:
        app = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception as e:
        print(f"[server_manager] app.json unreadable for runtime: {e}", file=sys.stderr)
        app = {}
    runtimes = app.get("runtimes") if isinstance(app.get("runtimes"), dict) else {}
    name = os.environ.get("LLAMA_RUNTIME") or app.get("runtime") or name
    if name in runtimes and isinstance(runtimes[name], dict):
        preset = runtimes[name]
    elif runtimes:
        print(f"[server_manager] runtime '{name}' not in app.json runtimes "
              f"({', '.join(runtimes)}); using built-in defaults", file=sys.stderr)
    for k in RUNTIME_KEYS:
        if preset.get(k) not in (None, "", []):
            CONFIG_DEFAULTS[k] = preset[k]
    return {"name": name, **{k: CONFIG_DEFAULTS[k] for k in RUNTIME_KEYS},
            "small_model_gpu": preset.get("small_model_gpu")}


ACTIVE_RUNTIME = _load_runtime()


def apply_runtime(profile: dict) -> dict:
    """Force the active runtime's bin dir / backend / device list onto a profile."""
    for k in RUNTIME_KEYS:
        profile[k] = ACTIVE_RUNTIME[k]
    return profile


# Key -> (min, max) for integer launch params; 0 = "omit, use llama default"
CONFIG_INT_FIELDS = {
    "context_size": (512, 1048576),
    "n_gpu_layers": (0, 999),
    "threads": (0, 64),
    "threads_batch": (0, 64),
    "batch_size": (0, 8192),
    "ubatch_size": (0, 4096),
    "n_slots": (0, 64),
    "mtp_draft_n_max": (1, 16),
    "kv_unified_per_slot": (0, 1048576),
    "cache_reuse": (0, 4096),
    "cache_ram": (0, 49152),
}

CONFIG_CHOICE_FIELDS = {
    "flash_attn": ("on", "off", "auto"),
    "kv_cache_type": ("f16", "bf16", "q8_0", "q5_0", "q4_0", "f32"),
    "split_mode": ("layer", "row", "tensor", "none"),
}

# Where each key lives inside the profile JSON
CONFIG_TARGETS = {
    "context_size": (None, "context_size"),
    "n_gpu_layers": ("tuned", "n_gpu_layers"),
    "tensor_split": ("tuned", "tensor_split"),
    "split_mode": (None, "split_mode"),
    "threads": (None, "threads"),
    "threads_batch": (None, "threads_batch"),
    "batch_size": (None, "batch_size"),
    "ubatch_size": (None, "ubatch_size"),
    "n_slots": (None, "n_slots"),
    "flash_attn": (None, "flash_attn"),
    "kv_cache_type": (None, "kv_cache_type"),
    "mtp_draft_n_max": (None, "mtp_draft_n_max"),
    "kv_unified_per_slot": (None, "kv_unified_per_slot"),
    "cache_reuse": (None, "cache_reuse"),
    "cache_ram": (None, "cache_ram"),
}

# Persisted per-model overrides
MODEL_CONFIG_KEYS = (
    "context_size", "n_gpu_layers", "threads", "threads_batch",
    "batch_size", "ubatch_size", "n_slots", "tensor_split", "split_mode",
    "flash_attn", "kv_cache_type", "mtp_enabled", "mtp_draft_n_max",
    "keepalive_interval_s", "gpu_devices", "vision_capable",
    "kv_unified", "kv_unified_per_slot", "cache_reuse", "cache_ram",
)
