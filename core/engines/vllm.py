"""vLLM as the main-model engine: settings, model checks and the launch command.

vLLM is installed apart from the app (its own venv or container, see docs/vllm.md); the app only runs its `vllm` program.
It serves the same OpenAI API on the same port as llama-server, so chat code is unchanged; the few llama.cpp-only request
and response fields are translated in core/engines/vllm_adapter.py.

Settings: config/app.json "engines": {"vllm": {...}} (defaults in DEFAULTS). Per model (config/model_configs.json): engine,
tensor_parallel_size, pipeline_parallel_size, gpu_memory_utilization_pct, vllm_dtype, vllm_quantization, vllm_tokenizer,
vllm_tool_parser, vllm_reasoning_parser, vllm_enforce_eager."""
import json
import os
import re
import shutil
from pathlib import Path

from ..config import ACTIVE_RUNTIME, CONFIG_DEFAULTS, LLAMA_SERVER_PORT

DEFAULTS = {
    "bin": "vllm",                   # path of the `vllm` program (e.g. /opt/vllm/.venv/bin/vllm), or a name on PATH
    "device": "xpu",                 # "xpu" (Intel Arc) or "cuda"
    "gpu_devices": "auto",           # "auto" = the llama.cpp runtime's GPUs, or a list such as [0, 1]
    "gpu_memory_utilization": 0.90,  # share of each GPU vLLM reserves for weights + KV cache
    "health_timeout_s": 900,         # vLLM loads weights and compiles graphs: minutes, not seconds
    "max_num_seqs": 8,               # requests run at once when the model's "Parallel slots" is 0 or 1 (vLLM batches them)
    "tool_call_parser": "hermes",    # default when a model sets none ("" = no automatic tool calls)
    "reasoning_parser": "",          # e.g. "qwen3", "deepseek_r1"; "" = none
    "grammar_param": "structured_outputs",   # how a GBNF grammar is sent: "structured_outputs" (new) or "guided_grammar" (old)
    "env": {},                       # extra environment variables for the vLLM process
    "extra_args": [],                # extra command-line arguments, appended last
}

# vLLM sends anonymous usage statistics to its developers unless told not to. Nothing of ours should leave the server.
PRIVACY_ENV = {"VLLM_NO_USAGE_STATS": "1", "DO_NOT_TRACK": "1"}
VISIBLE_ENV = {"xpu": "ZE_AFFINITY_MASK", "cuda": "CUDA_VISIBLE_DEVICES"}
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt")


def settings() -> dict:
    """config/app.json engines.vllm over DEFAULTS."""
    try:
        from ..small_model import APP_CONFIG
        cfg = (APP_CONFIG.get("engines") or {}).get("vllm") or {}
    except Exception:
        cfg = {}
    return {**DEFAULTS, **{k: v for k, v in cfg.items() if v is not None}}


# ------------------------------------------------------------------ model files
def is_hf_folder(p) -> bool:
    """A Hugging Face model folder: config.json plus weight files (safetensors / bin)."""
    p = Path(p)
    try:
        if not p.is_dir() or not (p / "config.json").is_file():
            return False
        return any(f.suffix.lower() in WEIGHT_SUFFIXES and f.is_file() for f in p.iterdir())
    except OSError:
        return False


def folder_size_bytes(p) -> int:
    total = 0
    try:
        for f in Path(p).iterdir():
            if f.is_file() and f.suffix.lower() in WEIGHT_SUFFIXES:
                total += f.stat().st_size
    except OSError:
        pass
    return total


def model_format(p) -> str:
    """"hf" for a model folder, "gguf" for a .gguf file, "" otherwise."""
    p = Path(p)
    if p.suffix.lower() == ".gguf":
        return "gguf"
    return "hf" if is_hf_folder(p) else ""


GENERIC_FOLDER_NAMES = {"hf", "huggingface", "vllm", "model", "models", "safetensors", "snapshot", "snapshots", "main"}


def display_name(path) -> str:
    """The model's real name for a Hugging Face folder: the folder name, unless that is generic (hf, models, a snapshot
    hash...). Then the last part of `_name_or_path` in config.json ("Qwen/Qwen3-14B" -> "Qwen3-14B"). Never raises."""
    p = Path(str(path))
    folder = p.name
    if folder.lower() not in GENERIC_FOLDER_NAMES and not re.fullmatch(r"[0-9a-fA-F]{40}", folder):
        return folder
    try:
        with open(p / "config.json", "r", encoding="utf-8") as fh:
            ref = json.load(fh).get("_name_or_path")
        if isinstance(ref, str):
            last = ref.strip().replace("\\", "/").rstrip("/").split("/")[-1].strip()
            if last and last != ".":
                return last
    except Exception:
        pass
    return folder


def engines_for(p) -> list:
    """Engines that can run this model: a folder only on vLLM; a GGUF on llama.cpp, and on vLLM (experimental)."""
    fmt = model_format(p)
    return ["vllm"] if fmt == "hf" else (["llama", "vllm"] if fmt == "gguf" else [])


def target_problem(profile: dict):
    """Why this model cannot run on vLLM as configured, or None."""
    from ..vram.gguf_parser import model_files
    p = Path(str(profile.get("model_path") or ""))
    fmt = model_format(p)
    if fmt == "hf":
        return None
    if fmt == "gguf":
        if len(model_files(p)) > 1:
            return ("vLLM cannot load a GGUF split into several files. Merge it first (llama-gguf-split --merge "
                    "<part-00001> <merged.gguf>), or run this model on llama.cpp.")
        if not str(profile.get("vllm_tokenizer") or "").strip():
            return ("vLLM needs the original model's tokenizer to run a GGUF. Set 'Tokenizer' in the model's vLLM settings "
                    "to its Hugging Face repo (e.g. Qwen/Qwen3-8B) or a local folder with tokenizer files.")
        return None
    return f"{p.name} is not a Hugging Face model folder (config.json + .safetensors) or a .gguf file."


# ------------------------------------------------------------------ GPUs
def gpu_devices(profile: dict) -> list:
    """The GPUs vLLM may use: the first tensor x pipeline parallel GPUs of engines.vllm.gpu_devices (auto = runtime's)."""
    cfg = settings().get("gpu_devices")
    devs = list(cfg) if isinstance(cfg, list) and cfg else list(ACTIVE_RUNTIME.get("gpu_devices") or [0])
    need = parallel_sizes(profile, len(devs))
    return [int(d) for d in devs[:need[0] * need[1]]]


def parallel_sizes(profile: dict, n_gpus: int = None) -> tuple:
    """(tensor, pipeline) parallel sizes. Unset tensor parallel = the largest power of two that fits the GPUs: attention
    heads are almost always a power-of-two count, which 3 GPUs do not divide, so 3 cards default to 2 (or set pipeline 3)."""
    if n_gpus is None:
        cfg = settings().get("gpu_devices")
        n_gpus = len(cfg) if isinstance(cfg, list) and cfg else len(ACTIVE_RUNTIME.get("gpu_devices") or [0])
    n_gpus = max(1, int(n_gpus))
    pp = max(1, int(profile.get("pipeline_parallel_size") or 1))
    tp = int(profile.get("tensor_parallel_size") or 0)
    if tp <= 0:
        tp = 1
        while tp * 2 * pp <= n_gpus:
            tp *= 2
    return tp, pp


def helper_gpus() -> set:
    """GPUs the llama.cpp helper models (executor, vision, embedder, ...) are set to use."""
    from ..config import default_small_gpu
    try:
        from ..small_model import APP_CONFIG
        lanes = APP_CONFIG.get("small_models") or {}
    except Exception:
        lanes = {}
    out = set()
    for cfg in lanes.values():
        if not isinstance(cfg, dict) or not (cfg.get("model") or cfg.get("diffusion_model")):
            continue
        g = cfg.get("gpu", default_small_gpu())
        try:
            if int(g) >= 0:
                out.add(int(g))
        except (TypeError, ValueError):
            pass
    return out


def memory_utilization(profile: dict) -> float:
    pct = int(profile.get("gpu_memory_utilization_pct") or 0)
    if pct > 0:
        return round(min(98, pct) / 100, 2)
    return float(settings().get("gpu_memory_utilization") or 0.9)


def preflight(profile: dict) -> dict:
    """The /control/preflight answer for vLLM: no VRAM math (vLLM reserves its share itself), only the checks that matter."""
    problem = target_problem(profile)
    devs = gpu_devices(profile)
    tp, pp = parallel_sizes(profile)
    warnings = []
    shared = sorted(set(devs) & helper_gpus())
    if shared:
        warnings.append(f"GPU {', '.join(map(str, shared))} is also used by helper models; vLLM reserves "
                        f"{int(memory_utilization(profile) * 100)}% of it, so lower 'GPU memory share' or move the helpers.")
    if tp * pp > len(devs):
        warnings.append(f"Tensor x pipeline parallel = {tp * pp} GPUs, but only {len(devs)} are available to vLLM.")
    status = "nofit" if problem else ("tight" if warnings else "ok")
    return {"engine": "vllm", "status": status, "error": problem, "warnings": warnings, "gpu_devices": devs,
            "tensor_parallel_size": tp, "pipeline_parallel_size": pp, "suggestions": []}


# ------------------------------------------------------------------ launch
def find_vllm() -> str:
    b = str(settings().get("bin") or "vllm")
    found = b if Path(b).is_file() else shutil.which(b)
    if not found:
        raise RuntimeError(f"vLLM program '{b}' not found. Install vLLM (docs/vllm.md) and set engines.vllm.bin "
                           f"in config/app.json to its path.")
    return found


def served_name(profile: dict) -> str:
    from ..profiles.paths import model_display_name
    mp = Path(str(profile.get("model_path") or "model"))
    if model_format(mp) == "hf":
        return display_name(mp) or "local"
    return model_display_name(mp.name) or "local"


def max_num_seqs(profile: dict) -> int:
    """Requests vLLM runs at once. llama.cpp's default of one slot would waste vLLM's batching, so 0 or 1 means the
    engines.vllm default; 2 or more is used as set."""
    n = int((profile or {}).get("n_slots") or 0)
    if n >= 2:
        return n
    try:
        return max(1, int(settings().get("max_num_seqs") or 8))
    except (TypeError, ValueError):
        return 8


def model_extra_args(profile: dict) -> list:
    """The model's own extra vLLM arguments ("vllm_extra_args": "--kv-cache-dtype fp8"), split on spaces."""
    import shlex
    try:
        return shlex.split(str((profile or {}).get("vllm_extra_args") or ""))
    except ValueError:
        return []


def build_command(profile: dict) -> list:
    problem = target_problem(profile)
    if problem:
        raise RuntimeError(problem)
    s = settings()
    tp, pp = parallel_sizes(profile)
    cmd = [find_vllm(), "serve", str(profile["model_path"]),
           "--host", "127.0.0.1", "--port", str(LLAMA_SERVER_PORT),
           "--served-model-name", served_name(profile),
           "--max-model-len", str(int(profile.get("context_size") or CONFIG_DEFAULTS["context_size"])),
           "--max-num-seqs", str(max_num_seqs(profile)),
           "--tensor-parallel-size", str(tp), "--pipeline-parallel-size", str(pp),
           "--gpu-memory-utilization", str(memory_utilization(profile)),
           "--enable-prefix-caching", "--enable-prompt-tokens-details"]
    if profile.get("vllm_tokenizer"):
        cmd += ["--tokenizer", str(profile["vllm_tokenizer"]).strip()]
    for key, flag in (("vllm_dtype", "--dtype"), ("vllm_quantization", "--quantization")):
        v = str(profile.get(key) or "auto")
        if v != "auto":
            cmd += [flag, v]
    tool_parser = profile.get("vllm_tool_parser")
    tool_parser = s.get("tool_call_parser") if tool_parser is None else tool_parser
    if str(tool_parser or "").strip():
        cmd += ["--enable-auto-tool-choice", "--tool-call-parser", str(tool_parser).strip()]
    reasoning_parser = profile.get("vllm_reasoning_parser")
    reasoning_parser = s.get("reasoning_parser") if reasoning_parser is None else reasoning_parser
    if str(reasoning_parser or "").strip():
        cmd += ["--reasoning-parser", str(reasoning_parser).strip()]
    if profile.get("vllm_enforce_eager"):
        cmd.append("--enforce-eager")
    # never --trust-remote-code by default: it runs Python code shipped inside the model folder. An admin who trusts a
    # model can add it to engines.vllm.extra_args.
    cmd += [str(a) for a in (s.get("extra_args") or [])]
    cmd += model_extra_args(profile)
    return cmd


def build_env(profile: dict) -> dict:
    s = settings()
    env = dict(os.environ)
    env.update(PRIVACY_ENV)
    var = VISIBLE_ENV.get(str(s.get("device") or "xpu").lower())
    devs = gpu_devices(profile)
    if var and devs:
        env[var] = ",".join(map(str, devs))
    env.update({str(k): str(v) for k, v in (s.get("env") or {}).items()})
    return env


def health_timeout_s() -> int:
    try:
        return max(60, int(settings().get("health_timeout_s") or 900))
    except (TypeError, ValueError):
        return 900
