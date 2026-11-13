import json
import os
import sys
import threading
import time
from pathlib import Path

from .backend import BackendConfigError, get_backend, validate_runtime_env

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
STATIC_DIR = BASE_DIR / "static"
USAGE_DB_FILE = BASE_DIR / "usage.db"
PROJECTS_DB_FILE = BASE_DIR / "projects.db"
MEMORY_DB_FILE = BASE_DIR / "memory.db"
AUTH_DB_FILE = BASE_DIR / "auth.db"       # users, roles, permissions, sessions, audit log
KNOWLEDGE_UPLOADS_DIR = BASE_DIR / "knowledge_uploads"  # uploaded org knowledge-base files
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "app.json"          # main app config (models_dir, lanes, capabilities, ...)
ROLES_FILE = CONFIG_DIR / "roles.json"         # multiagent role definitions (planner/coder/reviewer)
# A770_MODEL_CONFIGS_FILE points the app at another per-GGUF launch file (benchmarks run a model at a different context
# without touching the real one; the app also writes its autotune results there, so a temp copy stays temp).
MODEL_CONFIGS_FILE = Path(os.environ.get("A770_MODEL_CONFIGS_FILE") or CONFIG_DIR / "model_configs.json")
PROVIDERS_FILE = CONFIG_DIR / "providers.json"   # legacy shared file -- migrated into PROVIDERS_DIR on first boot
PROVIDERS_DIR = CONFIG_DIR / "providers"         # per-user cloud providers + lane bindings: providers/user_<id>.json

# Local settings live in config/<name>.json and are git-ignored, so editing them never dirties the working tree or blocks a
# pull. The tracked defaults are config/<name>.example.json: a missing local file is created from its example on startup.
LOCAL_CONFIG_NAMES = ("app", "roles", "model_configs", "sampling_config", "fx_rates", "web_blocked")


def seed_local_configs(config_dir: Path = CONFIG_DIR) -> list:
    """Copy each config/<name>.example.json to config/<name>.json when the local file is missing. Returns the names created."""
    import shutil
    created = []
    for name in LOCAL_CONFIG_NAMES:
        local, example = config_dir / f"{name}.json", config_dir / f"{name}.example.json"
        if not local.exists() and example.exists():
            try:
                shutil.copyfile(example, local)
                created.append(name)
            except OSError as e:
                print(f"[server_manager] could not create {local.name} from its example: {e}", file=sys.stderr)
    if created:
        print(f"[server_manager] created local config from examples: {', '.join(created)}", file=sys.stderr)
    return created


seed_local_configs()

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

# Most GPUs one llama-server can be split across (llama.cpp has no hard limit; this bounds the
# tensor_split the config API will accept).
MAX_GPUS = 14

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
    "backend": "vulkan",  # "vulkan", "cuda", or "sycl"
}

# Machine-level llama.cpp runtime. config/app.json holds named presets:
#   "runtime": "vulkan",
#   "runtimes": {"vulkan": {"llama_bin_dir": ..., "backend": "vulkan", "gpu_devices": [1, 2]},
#                "cuda":   {"llama_bin_dir": ..., "backend": "cuda",   "gpu_devices": [0]}}
# LLAMA_RUNTIME=<name> in the environment overrides "runtime". The active
# preset replaces the llama_bin_dir/backend/gpu_devices defaults and wins over
# per-model values, since device numbering is a property of the machine.
RUNTIME_KEYS = ("llama_bin_dir", "backend", "gpu_devices")


class RuntimeConfigError(ValueError):
    """The startup-selected native llama.cpp runtime is invalid."""


def normalize_tensor_split(tensor_split, gpu_devices) -> tuple:
    """(gpu_devices, tensor_split) with zero-share and non-numeric segments removed.

    ONE implementation, because the estimator and the launcher used to disagree about it and
    the disagreement was invisible: preflight dropped non-numeric segments BEFORE comparing
    lengths, the launcher compared lengths FIRST. So for a hand-edited "9,,11" on two GPUs,
    preflight normalised to "9,11" and approved, while llama-server was handed
    `--tensor-split 9,,11`. The model that was checked is not the model that launched.

    Rule: the segment count is compared against the device count BEFORE any filtering, because
    filtering first is the bug. A count mismatch means the split and the device list genuinely
    disagree ("9,,11" on two GPUs is ambiguous - dropping either GPU is a guess), the split is
    replaced by an equal split over every device (with a warning), so launch and preflight still
    agree: a saved "10,10" on a 3-GPU runtime used to be handed to llama.cpp as-is, which
    silently leaves the third GPU idle. A zero or non-numeric segment in an otherwise-aligned
    split disables that GPU, which is the documented "0,1" behaviour.
    """
    devs = list(gpu_devices or [])
    parts = [s.strip() for s in str(tensor_split or "").split(",")]
    if len(devs) > 1 and len(parts) != len(devs):
        equal = ",".join("1" for _ in devs)
        print(f"[server_manager] tensor_split '{tensor_split}' has {len(parts)} entries but "
              f"{len(devs)} GPUs are in use; using an equal split ({equal}). Set one share "
              f"per GPU in Model Config.", file=sys.stderr)
        return devs, equal
    if len(devs) > 1 and len(parts) == len(devs):
        # No "if kept" guard: an all-zero split ("0,0") filters every device and takes the
        # single-device path, which is the behaviour core/process.py always had and which
        # tests/test_launch_command.py::test_all_zero_shares_leave_no_devices pins. Guarding
        # it would silently fall through to launching both GPUs with --tensor-split 0,0.
        kept = [(d, s) for d, s in zip(devs, parts) if s.isdigit() and int(s) > 0]
        return [d for d, _ in kept], ",".join(s for _, s in kept)
    return devs, str(tensor_split)


def select_runtime(app: dict, *, environ: dict | None = None,
                   defaults: dict | None = None) -> dict:
    """Resolve and validate exactly one runtime without reading global state.

    An explicit missing runtime or unknown backend is a startup error. The old
    behavior printed a warning and silently launched the Vulkan defaults, which
    is unsafe once Vulkan, CUDA, and SYCL installations coexist.
    """
    env = os.environ if environ is None else environ
    base = CONFIG_DEFAULTS if defaults is None else defaults
    if not isinstance(app, dict):
        raise RuntimeConfigError("config/app.json must contain a JSON object")
    runtimes = app.get("runtimes") if isinstance(app.get("runtimes"), dict) else {}
    configured_name = app.get("runtime")
    override_name = env.get("LLAMA_RUNTIME")
    name = str(override_name or configured_name or base["backend"]).strip().lower()
    explicit = override_name not in (None, "") or configured_name not in (None, "")

    if runtimes:
        preset = runtimes.get(name)
        if not isinstance(preset, dict):
            choices = ", ".join(sorted(str(k) for k in runtimes))
            raise RuntimeConfigError(
                f"runtime '{name}' is not configured in app.json runtimes "
                f"(available: {choices})"
            )
    else:
        if explicit and name != str(base["backend"]).strip().lower():
            raise RuntimeConfigError(
                f"runtime '{name}' is not configured in app.json runtimes"
            )
        preset = {}

    backend_name = str(preset.get("backend") or name or base["backend"]).strip().lower()
    try:
        get_backend(backend_name)
    except BackendConfigError as exc:
        raise RuntimeConfigError(str(exc)) from exc

    bin_dir = preset.get("llama_bin_dir") or base.get("llama_bin_dir")
    if not isinstance(bin_dir, str) or not bin_dir.strip():
        raise RuntimeConfigError(
            f"runtimes.{name}.llama_bin_dir must be a non-empty path string"
        )

    raw_devices = preset.get("gpu_devices", base.get("gpu_devices"))
    gpu_auto = isinstance(raw_devices, str) and raw_devices.strip().lower() == "auto"
    if gpu_auto:
        devices = list(base.get("gpu_devices") or [])
    elif isinstance(raw_devices, list) and raw_devices:
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0
               for v in raw_devices):
            raise RuntimeConfigError(
                f"runtimes.{name}.gpu_devices must contain non-negative integers"
            )
        if len(set(raw_devices)) != len(raw_devices):
            raise RuntimeConfigError(
                f"runtimes.{name}.gpu_devices must not contain duplicates"
            )
        devices = list(raw_devices)
    else:
        raise RuntimeConfigError(
            f"runtimes.{name}.gpu_devices must be 'auto' or a non-empty integer list"
        )

    small_gpu = preset.get("small_model_gpu")
    if small_gpu is not None and (
            isinstance(small_gpu, bool) or not isinstance(small_gpu, int) or small_gpu < 0):
        raise RuntimeConfigError(
            f"runtimes.{name}.small_model_gpu must be a non-negative integer"
        )
    try:
        runtime_env = validate_runtime_env(backend_name, preset.get("env"))
    except BackendConfigError as exc:
        raise RuntimeConfigError(str(exc)) from exc

    return {
        "name": name,
        "llama_bin_dir": bin_dir.strip(),
        "backend": backend_name,
        "gpu_devices": devices,
        "gpu_auto": gpu_auto,
        "small_model_gpu": small_gpu,
        "env": runtime_env,
        "whisper_bin_dir": preset.get("whisper_bin_dir"),
        "sd_bin_dir": preset.get("sd_bin_dir"),
    }


def _load_runtime() -> dict:
    try:
        app = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception as e:
        print(f"[server_manager] app.json unreadable for runtime: {e}", file=sys.stderr)
        app = {}
    runtime = select_runtime(app)
    for key in RUNTIME_KEYS:
        CONFIG_DEFAULTS[key] = (list(runtime[key]) if key == "gpu_devices"
                                else runtime[key])
    return runtime


ACTIVE_RUNTIME = _load_runtime()

# What the last device scan found, for the Settings screens: {configured, detected:[{index,name,total_gb}], missing, unused, auto}
GPU_STATUS: dict = {"configured": list(ACTIVE_RUNTIME["gpu_devices"]), "detected": [], "missing": [], "unused": [],
                    "auto": bool(ACTIVE_RUNTIME.get("gpu_auto")), "checked": False, "scan_error": None}


def default_small_gpu() -> int:
    """The GPU a helper lane gets when nothing says otherwise: the runtime's small_model_gpu if it is in use, else the last
    GPU in use (the main model's split usually fills the first), else 0."""
    devs = [int(d) for d in (ACTIVE_RUNTIME.get("gpu_devices") or [])]
    v = ACTIVE_RUNTIME.get("small_model_gpu")
    if v is not None and (not devs or int(v) in devs):
        return int(v)
    return devs[-1] if devs else 0


def resolve_gpu_devices(force: bool = False) -> dict:
    """Compare the GPUs the machine has with the runtime's gpu_devices, and fill the list in when it is "auto".

    "auto" = every discrete GPU llama.cpp lists (iGPUs and anything under 2 GB are left out: core/gpu.py), so 1, 2, 3 or 4
    cards work without editing app.json. A fixed list is kept as written; indices the machine does not have, and discrete GPUs
    the list leaves out, are reported (GPU_STATUS) and printed once. Never raises: when the device list cannot be read the
    configured list stays."""
    try:
        from .vram import devices as _dev
        from . import gpu as _gpu
        found = _gpu._discrete_devices(_dev.query_devices(
            ACTIVE_RUNTIME.get("llama_bin_dir"), force=force, strict=True,
            backend=ACTIVE_RUNTIME.get("backend"),
            runtime_env=ACTIVE_RUNTIME.get("env")))
    except Exception as e:
        # Not silent: the Settings screens show scan_error, and "auto" then runs on the fallback list below. That list is
        # the Windows default [1, 2], so on Linux it can hide GPUs; the usual cause is a Windows llama_bin_dir in app.json.
        GPU_STATUS.update({"checked": False, "scan_error": str(e)})
        print(f"[server_manager] GPU scan FAILED: {e}", file=sys.stderr)
        if ACTIVE_RUNTIME.get("gpu_auto"):
            print(f"[server_manager] ERROR: gpu_devices is 'auto' but the GPUs could not be listed, so the fallback list "
                  f"{ACTIVE_RUNTIME.get('gpu_devices')} is used. Check runtimes.{ACTIVE_RUNTIME.get('name')}.llama_bin_dir "
                  f"in config/app.json: it must be the folder that holds llama-bench for this OS.", file=sys.stderr)
        return dict(GPU_STATUS)
    idx = sorted(int(d["index"]) for d in found)
    if ACTIVE_RUNTIME.get("gpu_auto"):
        if idx:
            ACTIVE_RUNTIME["gpu_devices"] = idx
            CONFIG_DEFAULTS["gpu_devices"] = list(idx)
        else:
            print("[server_manager] gpu_devices is 'auto' but no discrete GPU was found; keeping the default list", file=sys.stderr)
    configured = [int(x) for x in (ACTIVE_RUNTIME.get("gpu_devices") or [])]
    missing = [i for i in configured if i not in idx]
    unused = [i for i in idx if i not in configured]
    GPU_STATUS.update({"configured": configured, "auto": bool(ACTIVE_RUNTIME.get("gpu_auto")), "checked": True, "scan_error": None,
                       "detected": [{"index": int(d["index"]), "id": d.get("id"),
                                     "backend": d.get("backend", ACTIVE_RUNTIME.get("backend")),
                                     "name": d["name"], "total_gb": round(d["total_b"] / 2 ** 30, 1)}
                                    for d in found],
                       "missing": missing, "unused": unused})
    if missing:
        print(f"[server_manager] WARNING: gpu_devices {configured} lists GPU(s) {missing} that this machine does not report "
              f"(found {idx}). Set \"gpu_devices\": \"auto\" in the runtime preset of config/app.json.", file=sys.stderr)
    if unused and not ACTIVE_RUNTIME.get("gpu_auto"):
        print(f"[server_manager] note: discrete GPU(s) {unused} are not used (gpu_devices is {configured}). "
              f"Set \"gpu_devices\": \"auto\" to use every GPU.", file=sys.stderr)
    return dict(GPU_STATUS)


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
    profile["runtime_name"] = ACTIVE_RUNTIME["name"]
    profile["runtime_env"] = dict(ACTIVE_RUNTIME.get("env") or {})
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
    "cache_ram": (-1, 49152),               # -1 = no limit
    # vLLM engine (core/engines/vllm.py); gpu_memory_utilization_pct 0 = engines.vllm default
    "tensor_parallel_size": (0, 16),         # 0 = automatic
    "pipeline_parallel_size": (1, 16),
    "gpu_memory_utilization_pct": (0, 98),
}

CONFIG_CHOICE_FIELDS = {
    "flash_attn": ("on", "off", "auto"),
    "kv_cache_type": ("f16", "bf16", "q8_0", "q5_0", "q4_0", "f32"),
    "split_mode": ("layer", "row", "tensor", "none"),
    "engine": ("llama", "vllm"),
    "vllm_dtype": ("auto", "bfloat16", "float16"),
    "vllm_quantization": ("auto", "fp8", "awq", "gptq", "gguf"),
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
    "engine": (None, "engine"),
    "tensor_parallel_size": (None, "tensor_parallel_size"),
    "pipeline_parallel_size": (None, "pipeline_parallel_size"),
    "gpu_memory_utilization_pct": (None, "gpu_memory_utilization_pct"),
    "vllm_dtype": (None, "vllm_dtype"),
    "vllm_quantization": (None, "vllm_quantization"),
}

# Persisted per-model overrides
MODEL_CONFIG_KEYS = (
    "context_size", "n_gpu_layers", "threads", "threads_batch",
    "batch_size", "ubatch_size", "n_slots", "tensor_split", "split_mode",
    "flash_attn", "kv_cache_type", "mtp_enabled", "mtp_draft_n_max",
    "keepalive_interval_s", "gpu_devices", "vision_capable",
    "kv_unified", "kv_unified_per_slot", "cache_reuse", "cache_ram",
    "use_mmap", "op_offload",
    # vLLM engine (main model only); see core/engines/vllm.py
    "engine", "tensor_parallel_size", "pipeline_parallel_size", "gpu_memory_utilization_pct",
    "vllm_dtype", "vllm_quantization", "vllm_tokenizer", "vllm_tool_parser", "vllm_reasoning_parser",
    "vllm_enforce_eager", "vllm_extra_args",
)
