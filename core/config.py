import json
import os
import sys
import threading
import time
from pathlib import Path

# Port & Network Settings
# Loopback by default. This platform holds cloud API keys, org knowledge and an agent that can
# run shell commands, and it was binding every interface: over plain HTTP the session cookie
# is sniffable by anything on the LAN (PCI DSS 4.2.1). Reach it from another machine with
# --host 0.0.0.0 or A770_HOST=0.0.0.0, deliberately, with TLS in front.
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8000
LLAMA_SERVER_PORT = 8090  # internal port, not exposed directly
HEALTH_TIMEOUT_S = 120
WATCHDOG_INTERVAL_S = 5
MAX_RESTART_BACKOFF_S = 60
# Crash-loop ceiling (core/state.py watchdog). Previously there was none at all: a model that
# could not launch retried once a minute for the life of the process, each attempt burning up
# to HEALTH_TIMEOUT_S. Hitting this sets `degraded`, which stops the retries and is cleared by
# the next successful load or a manual model switch.
MAX_RESTART_COUNT = 5
# How long a process must stay up before its death stops counting toward the crash-loop
# breaker. Reaching /health is not enough: a model that is too big for the card reports
# healthy and then OOM-crashes on the first real generation, so resetting the counter there
# would mean the breaker never trips in precisely the case it exists for.
RESTART_RESET_AFTER_S = 300

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

# llama-server logging. The main model's stdout used to go to print() and nowhere else, so the
# OOM/allocator trace from a failed load existed only in console scrollback and was gone on
# restart. Small-model lanes already kept a ring buffer and surfaced the error; this does the
# same for the main server, plus an on-disk daily log.
LOG_DIR = BASE_DIR / "logs"
LOG_TAIL_LINES = 2000        # in-memory ring, what /control/status greps for the failure reason
LOG_KEEP_DAYS = 7            # how many daily log files to retain
LOG_WRITE_FILE = True         # set false to keep the ring buffer only and stay print-only

# httpx timeouts for the local llama-server clients (core/state.py, core/small_model/instance.py).
# These were timeout=None, which meant a wedged generation held the SSE stream and a GPU slot
# for as long as it liked: the agent's wall-clock budget is only checked between steps, so
# nothing downstream could ever end an in-flight request.
#
# httpx applies `read` per chunk, not to the whole stream, so it fires only when the server
# goes SILENT. A generation that is still producing tokens never trips it, however long it
# takes -- so this is a wedge detector, not a throughput cap, and it does not make slow
# models look broken. 600s of complete silence is far past any legitimate pause.
LOCAL_HTTP_CONNECT_TIMEOUT_S = 5.0
LOCAL_HTTP_READ_TIMEOUT_S = 600.0
LOCAL_HTTP_WRITE_TIMEOUT_S = 30.0
LOCAL_HTTP_POOL_TIMEOUT_S = 10.0

# Upper bound on a single tool call inside an agent step (routes/agent/permissions.py keepalive).
# Generous enough for a long shell command or a media job; finite so a tool that never returns
# releases its slot instead of pinning it for the rest of the run.
TOOL_MAX_S = 1800
# How long to wait for a cancelled tool task to actually land before giving up on it.
TOOL_CANCEL_GRACE_S = 5

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
    "cache_ram": 0,            # -cram MiB host-RAM prompt cache (0 = 2048 default, see process.MAIN_CACHE_RAM_MB; -1 = no limit)
    "use_mmap": True,          # memory-map model file (True = default/auto, False = --load-mode none to free system RAM)
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


def normalize_tensor_split(tensor_split, gpu_devices) -> tuple:
    """(gpu_devices, tensor_split) with zero-share and non-numeric segments removed.

    ONE implementation, because the estimator and the launcher used to disagree about it and
    the disagreement was invisible: preflight dropped non-numeric segments BEFORE comparing
    lengths, the launcher compared lengths FIRST. So for a hand-edited "9,,11" on two GPUs,
    preflight normalised to "9,11" and approved, while llama-server was handed
    `--tensor-split 9,,11`. The model that was checked is not the model that launched.

    Rule: the segment count is compared against the device count BEFORE any filtering, because
    filtering first is the bug. A count mismatch means the split and the device list genuinely
    disagree ("9,,11" on two GPUs is ambiguous - dropping either GPU is a guess), so both are
    passed through untouched for llama.cpp to reject loudly. A zero or non-numeric segment in
    an otherwise-aligned split disables that GPU, which is the documented "0,1" behaviour.
    """
    devs = list(gpu_devices or [])
    parts = [s.strip() for s in str(tensor_split or "").split(",")]
    if len(devs) > 1 and len(parts) == len(devs):
        # No "if kept" guard: an all-zero split ("0,0") filters every device and takes the
        # single-device path, which is the behaviour core/process.py always had and which
        # tests/test_launch_command.py::test_all_zero_shares_leave_no_devices pins. Guarding
        # it would silently fall through to launching both GPUs with --tensor-split 0,0.
        kept = [(d, s) for d, s in zip(devs, parts) if s.isdigit() and int(s) > 0]
        return [d for d, _ in kept], ",".join(s for _, s in kept)
    return devs, str(tensor_split)


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
            "small_model_gpu": preset.get("small_model_gpu"),
            # optional folder holding whisper-server(.exe) for speech to text
            "whisper_bin_dir": preset.get("whisper_bin_dir"),
            # optional folder holding sd-server(.exe) (stable-diffusion.cpp) for local images
            "sd_bin_dir": preset.get("sd_bin_dir")}


ACTIVE_RUNTIME = _load_runtime()


# One lock for every read-modify-write of config/app.json, so two saves can't
# drop each other's change; the write itself goes through a temp file + rename
# so a crash mid-save never leaves a truncated app.json behind.
CONFIG_LOCK = threading.RLock()


def atomic_write_json(path: Path, data) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows refuses the rename while another handle has the file open
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_app_config(path: Path = None) -> dict:
    return json.loads(Path(path or CONFIG_FILE).read_text(encoding="utf-8"))


def write_app_config(cfg: dict, path: Path = None) -> None:
    # Callers pass their own module-level CONFIG_FILE so tests that patch it
    # never touch the real config/app.json.
    with CONFIG_LOCK:
        atomic_write_json(path or CONFIG_FILE, cfg)
    try:
        from . import cloud
        cloud.reload()
    except Exception:
        pass


def update_app_config(mutator, path: Path = None) -> dict:
    """Re-read app.json, apply mutator(cfg) in place, write it back atomically."""
    with CONFIG_LOCK:
        cfg = read_app_config(path)
        mutator(cfg)
        write_app_config(cfg, path)
    return cfg


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
    "use_mmap",
)
