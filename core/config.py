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
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "app.json"          # main app config (models_dir, lanes, capabilities, ...)
ROLES_FILE = CONFIG_DIR / "roles.json"         # multiagent role definitions (planner/coder/reviewer)
MODEL_CONFIGS_FILE = CONFIG_DIR / "model_configs.json"
PROVIDERS_FILE = CONFIG_DIR / "providers.json"   # cloud providers + lane bindings (UI-managed, gitignored)

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
    "keepalive_interval_s": 25,
    "gpu_devices": [1, 2],
    "llama_bin_dir": "E:\\AI\\llama-vulkan",
    "backend": "vulkan",  # "vulkan" or "cuda" - selects -dev prefix + visible-devices env var
}

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
}

CONFIG_CHOICE_FIELDS = {
    "flash_attn": ("on", "off", "auto"),
    "kv_cache_type": ("f16", "bf16", "q8_0", "q5_0", "q4_0", "f32"),
    "split_mode": ("layer", "row"),
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
}

# Persisted per-model overrides
MODEL_CONFIG_KEYS = (
    "context_size", "n_gpu_layers", "threads", "threads_batch",
    "batch_size", "ubatch_size", "n_slots", "tensor_split", "split_mode",
    "flash_attn", "kv_cache_type", "mtp_enabled", "mtp_draft_n_max",
    "keepalive_interval_s", "gpu_devices",
)
