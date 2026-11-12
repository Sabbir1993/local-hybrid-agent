"""vLLM as the main-model engine: settings, model checks and the launch command.

vLLM is installed apart from the app (its own venv or container, see docs/vllm.md); the app only runs its `vllm` program.
It serves the same OpenAI API on the same port as llama-server, so chat code is unchanged; the few llama.cpp-only request
and response fields are translated in core/engines/vllm_adapter.py.

Settings: config/app.json "engines": {"vllm": {...}} (defaults in DEFAULTS). Per model (config/model_configs.json): engine,
tensor_parallel_size, pipeline_parallel_size, gpu_memory_utilization_pct, vllm_dtype, vllm_quantization, vllm_tokenizer,
vllm_tool_parser, vllm_reasoning_parser, vllm_enforce_eager, vllm_extra_args (allowlisted flags only: EXTRA_FLAGS)."""
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
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
    "extra_args": [],                # admin-only extra arguments (list or one string); the names in DENIED_FLAGS are dropped
    "allow_pickle_weights": False,   # True = a folder with only .bin/.pt (pickle) weights may load; pickle can run code
}

# vLLM sends anonymous usage statistics to its developers unless told not to. Nothing of ours should leave the server.
# huggingface_hub has its own telemetry ping, off as well. (HF_HUB_OFFLINE is added per model, see build_env.)
PRIVACY_ENV = {"VLLM_NO_USAGE_STATS": "1", "DO_NOT_TRACK": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
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
        if not has_safetensors(p) and not settings().get("allow_pickle_weights"):
            return (f"{p.name} has only pickle weights (.bin / .pt), which can run code when they load. Use a .safetensors "
                    f"copy of the model, or set engines.vllm.allow_pickle_weights to true in config/app.json if you trust it.")
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


# ------------------------------------------------------------------ extra arguments
# The last flag on a vLLM command line wins, so a free-text "extra arguments" box would let anyone with the
# `model.local.configure` permission override what the app sets for safety: --host 0.0.0.0 (the server then answers the LAN
# without the app's login and card-number masking), --api-key, --allowed-local-media-path=/ (reads any file),
# --middleware=<module> (imports any Python module), --chat-template=<file>, --trust-remote-code, --served-model-name ...
# So a model's extra arguments are an ALLOWLIST of flag names, each with a value type. Anything else is refused.
_VALUE_CHARS = re.compile(r"^[A-Za-z0-9_./:=,{}\[\]\"'\- ]*$")
_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.]{0,39}$")
_NUMBER = re.compile(r"^[0-9]{1,9}(?:\.[0-9]{1,4})?$")
_JSON_KEY = re.compile(r"^[A-Za-z0-9_.\-]{1,60}$")
_JSON_STR = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:/\-]{0,79}$")      # no leading "/" or "-" or "~": no absolute path, no flag
EXTRA_FLAGS = {
    # flag -> "bool" (no value) | "int" | "num" (int or decimal) | "word" | ("choice", values) | "json" (one JSON object)
    "--kv-cache-dtype": "word", "--max-num-batched-tokens": "int", "--enable-chunked-prefill": "bool",
    "--no-enable-chunked-prefill": "bool", "--enable-prefix-caching": "bool", "--no-enable-prefix-caching": "bool",
    "--block-size": "int", "--swap-space": "num", "--cpu-offload-gb": "num", "--max-seq-len-to-capture": "int",
    "--disable-custom-all-reduce": "bool", "--enforce-eager": "bool", "--seed": "int", "--num-scheduler-steps": "int",
    "--scheduling-policy": "word", "--max-num-partial-prefills": "int", "--long-prefill-token-threshold": "int",
    "--speculative-config": "json", "--compilation-config": "json", "--disable-log-stats": "bool",
    "--generation-config": ("choice", ("auto", "vllm")),         # a folder here would read a file the user picks
    "--dtype": "word", "--quantization": "word", "--rope-scaling": "json", "--max-logprobs": "int",
}
# Names the admin's own engines.vllm.extra_args may not carry either (the app sets or must control them). "*" = any ending.
DENIED_FLAGS = ("--host", "--port", "--api-key", "--ssl-*", "--middleware", "--chat-template", "--allowed-*", "--lora*",
                "--trust*", "--served-model-name", "--tokenizer*", "--model", "--uds", "--root-path", "--config",
                "--enable-lora")
_MAX_EXTRA_TOKENS = 40
_MAX_JSON_CHARS = 400


def _norm_flag(tok: str) -> str:
    """"--KV_cache_dtype=fp8" -> "--kv-cache-dtype": the flag name alone, lowercase, dashes."""
    return tok.partition("=")[0].strip().lower().replace("_", "-")


def _arg_tokens(raw) -> list:
    """A str is split like a shell would (never per character); a list/tuple must hold strings. Raises ValueError."""
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, str):
        try:
            toks = shlex.split(raw)
        except ValueError:
            raise ValueError("unbalanced quotes") from None
    elif isinstance(raw, (list, tuple)):
        if not all(isinstance(t, str) for t in raw):
            raise ValueError("every argument must be text")
        toks = list(raw)
    else:
        raise ValueError("must be text or a list of text")
    if len(toks) > _MAX_EXTRA_TOKENS:
        raise ValueError(f"too many arguments (limit {_MAX_EXTRA_TOKENS})")
    return toks


def _json_check(name: str, v, depth: int = 0):
    if depth > 4:
        raise ValueError(f"{name}: JSON nested too deeply")
    if isinstance(v, dict):
        if len(v) > 32:
            raise ValueError(f"{name}: JSON object too large")
        for k, x in v.items():
            if not _JSON_KEY.match(str(k)):
                raise ValueError(f"{name}: JSON key {str(k)[:30]!r} not accepted")
            _json_check(name, x, depth + 1)
    elif isinstance(v, list):
        if len(v) > 64:
            raise ValueError(f"{name}: JSON list too long")
        for x in v:
            _json_check(name, x, depth + 1)
    elif isinstance(v, str):
        if not _JSON_STR.match(v) or ".." in v:
            raise ValueError(f"{name}: JSON text {v[:30]!r} not accepted (plain names only, no paths or '..')")
    elif isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"{name}: JSON number not accepted")
    elif v is not None and not isinstance(v, (bool, int)):
        raise ValueError(f"{name}: JSON value not accepted")


def _clean_value(name: str, kind, val: str) -> str:
    if not _VALUE_CHARS.match(val) or val.startswith("-") or val.startswith("/") or val.startswith("~") or not val.strip():
        raise ValueError(f"{name}: value {val[:40]!r} not accepted (plain letters, digits and . _ - : , only; no paths, "
                         f"nothing starting with '-')")
    if kind == "int":
        if not val.isdigit() or len(val) > 9:
            raise ValueError(f"{name}: needs a whole number")
        return val
    if kind == "num":
        if not _NUMBER.match(val):
            raise ValueError(f"{name}: needs a number")
        return val
    if kind == "word":
        if not _WORD.match(val):
            raise ValueError(f"{name}: needs a plain name such as fp8")
        return val
    if isinstance(kind, tuple):
        if val not in kind[1]:
            raise ValueError(f"{name}: must be one of {', '.join(kind[1])}")
        return val
    if kind == "json":
        if len(val) > _MAX_JSON_CHARS:
            raise ValueError(f"{name}: JSON too long")
        try:
            obj = json.loads(val)
        except ValueError:
            raise ValueError(f"{name}: needs one JSON object, e.g. '{{\"method\":\"ngram\"}}'") from None
        if not isinstance(obj, dict):
            raise ValueError(f"{name}: needs one JSON object")
        _json_check(name, obj)
        return json.dumps(obj, separators=(",", ":"))
    raise ValueError(f"{name}: unsupported")


def parse_extra_args(raw) -> list:
    """A model's "vllm_extra_args" (text or list) checked against EXTRA_FLAGS -> clean argument list, flag and value as
    separate items, names in canonical form. Raises ValueError whose text names the flag and lists the allowed ones."""
    toks = _arg_tokens(raw)
    out, i = [], 0
    allowed = ", ".join(sorted(EXTRA_FLAGS))
    while i < len(toks):
        tok = toks[i]
        i += 1
        name = _norm_flag(tok)
        if not tok.startswith("--") or name not in EXTRA_FLAGS:
            shown = (name if tok.startswith("--") else tok)[:40]
            raise ValueError(f"{shown!r} is not an allowed vLLM argument here. Allowed: {allowed}. The app sets host, port, "
                             f"name, keys and trust settings itself; an admin can add other flags in config/app.json")
        kind = EXTRA_FLAGS[name]
        has_eq = "=" in tok
        if kind == "bool":
            if has_eq:
                raise ValueError(f"{name}: takes no value")
            out.append(name)
            continue
        if has_eq:
            val = tok.partition("=")[2]
        elif i < len(toks):
            val = toks[i]
            i += 1
        else:
            raise ValueError(f"{name}: needs a value")
        out += [name, _clean_value(name, kind, val)]
    return out


def _denied(name: str) -> bool:
    """name (normalised) is a denied flag, or an abbreviation of one (argparse accepts --ho for --host)."""
    for pat in DENIED_FLAGS:
        if fnmatch.fnmatchcase(name, pat):
            return True
        lit = pat.rstrip("*")
        if len(name) >= 3 and lit.startswith(name):
            return True
    return False


def admin_extra_args(raw) -> list:
    """engines.vllm.extra_args from config/app.json: the admin may add any flag except the DENIED_FLAGS (host, port, keys,
    ssl, middleware, chat template, media paths, lora, trust-remote-code, name, tokenizer ...). Those are logged and dropped,
    with their value. Bad text (not a str/list of str) drops everything."""
    try:
        toks = _arg_tokens(raw)
    except ValueError as e:
        print(f"[server_manager] vLLM engines.vllm.extra_args ignored: {e}")
        return []
    out, i = [], 0
    while i < len(toks):
        tok = toks[i]
        i += 1
        name = _norm_flag(tok) if tok.startswith("-") else ""
        if name and _denied(name):
            print(f"[server_manager] vLLM extra_args: dropped {name} (the app controls this setting itself)")
            if "=" not in tok and i < len(toks) and not toks[i].startswith("-"):
                i += 1
            continue
        out.append(tok)
    return out


# `vllm serve --help` says which request-logging switch this vLLM has (the flag was renamed between releases). Asked once per
# program file and kept; when it cannot be asked, no switch is added.
_HELP_CACHE: dict = {}


def _serve_help(binary: str) -> str:
    try:
        st = os.stat(binary)
        key = (binary, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (binary, 0, 0)
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    text = ""
    for extra in ("--help", "--help=all"):
        try:
            r = subprocess.run([binary, "serve", extra], capture_output=True, text=True, timeout=45,
                               env={**os.environ, **PRIVACY_ENV}, stdin=subprocess.DEVNULL)
            text += (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            break
        except Exception:
            return ""                         # not runnable now: ask again next time
        if "log-requests" in text:
            break
    _HELP_CACHE[key] = text
    return text


def log_requests_off_flag(binary: str) -> list:
    """The switch that stops vLLM printing every prompt to its log, for this vLLM version ([] when it has none/unknown)."""
    h = _serve_help(binary)
    if "--disable-log-requests" in h:
        return ["--disable-log-requests"]
    if "--no-enable-log-requests" in h:
        return ["--no-enable-log-requests"]
    return []


# ------------------------------------------------------------------ weights, tokenizer, names
def has_safetensors(p) -> bool:
    try:
        return any(f.suffix.lower() == ".safetensors" and f.is_file() for f in Path(p).iterdir())
    except OSError:
        return False


_NAME_RX = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}$")
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*/[A-Za-z0-9][A-Za-z0-9_.\-]*$")   # org/name only: a bare word could be a folder


def _plain_name(label: str, v) -> str:
    v = str(v).strip()
    if not _NAME_RX.match(v):
        raise RuntimeError(f"{label}: '{v[:40]}' is not a plain name (letters, digits, . _ -)")
    return v


def resolve_tokenizer(value) -> str:
    """What to give --tokenizer: a Hugging Face repo id (org/name), or a folder inside the models directory (relative to it
    or absolute, no '..', links resolved) as an absolute path. Raises ValueError otherwise ('' for none)."""
    v = str(value or "").strip()
    if not v:
        return ""
    if "\\" in v or "\0" in v or any(seg in ("..", ".") for seg in v.split("/")):
        raise ValueError("Tokenizer: use a repo id (org/name) or a folder inside the models folder; no '..' or backslashes")
    from ..profiles.paths import models_roots
    roots = []
    for r in models_roots():
        try:
            roots.append(Path(r).resolve())
        except OSError:
            pass
    path = Path(v)
    for cand in ([path] if path.is_absolute() else [r / v for r in roots]):
        try:
            rc = cand.resolve()
        except OSError:
            continue
        if rc.is_dir() and any(rc.is_relative_to(r) for r in roots):
            return str(rc)
    if path.is_absolute():
        raise ValueError("Tokenizer: a folder must be inside the models folder")
    if _REPO_ID.match(v):
        return v
    raise ValueError("Tokenizer: use a repo id (org/name) or a folder inside the models folder")


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
    """The model's own extra vLLM arguments ("vllm_extra_args": "--kv-cache-dtype fp8"), checked against EXTRA_FLAGS again
    here because model_configs.json can be edited by hand. Raises RuntimeError (the load stops) when one is not allowed."""
    try:
        return parse_extra_args((profile or {}).get("vllm_extra_args"))
    except ValueError as e:
        raise RuntimeError(f"vllm_extra_args: {e}") from None


def build_command(profile: dict) -> list:
    problem = target_problem(profile)
    if problem:
        raise RuntimeError(problem)
    s = settings()
    tp, pp = parallel_sizes(profile)
    binary = find_vllm()
    model_path = Path(str(profile["model_path"]))
    cmd = [binary, "serve", str(profile["model_path"]),
           "--max-model-len", str(int(profile.get("context_size") or CONFIG_DEFAULTS["context_size"])),
           "--max-num-seqs", str(max_num_seqs(profile)),
           "--tensor-parallel-size", str(tp), "--pipeline-parallel-size", str(pp),
           "--gpu-memory-utilization", str(memory_utilization(profile)),
           "--enable-prefix-caching", "--enable-prompt-tokens-details"]
    # safetensors cannot run code when loaded; pickle (.bin/.pt) can. target_problem already refused a pickle-only folder
    # unless the admin allowed it.
    if model_format(model_path) == "hf" and has_safetensors(model_path):
        cmd += ["--load-format", "safetensors"]
    # prompts must not end up in the vLLM log (it prints every request unless told not to)
    cmd += log_requests_off_flag(binary)
    if profile.get("vllm_tokenizer"):
        try:
            cmd += ["--tokenizer", resolve_tokenizer(profile["vllm_tokenizer"])]
        except ValueError as e:
            raise RuntimeError(str(e)) from None
    for key, flag in (("vllm_dtype", "--dtype"), ("vllm_quantization", "--quantization")):
        v = str(profile.get(key) or "auto").strip()
        if v != "auto":
            cmd += [flag, _plain_name(key, v)]
    tool_parser = profile.get("vllm_tool_parser")
    tool_parser = s.get("tool_call_parser") if tool_parser is None else tool_parser
    if str(tool_parser or "").strip():
        cmd += ["--enable-auto-tool-choice", "--tool-call-parser", _plain_name("vllm_tool_parser", tool_parser)]
    reasoning_parser = profile.get("vllm_reasoning_parser")
    reasoning_parser = s.get("reasoning_parser") if reasoning_parser is None else reasoning_parser
    if str(reasoning_parser or "").strip():
        cmd += ["--reasoning-parser", _plain_name("vllm_reasoning_parser", reasoning_parser)]
    if profile.get("vllm_enforce_eager"):
        cmd.append("--enforce-eager")
    # never --trust-remote-code: it runs Python code shipped inside the model folder, so it is in DENIED_FLAGS for the
    # admin's extra_args too (a model that needs it has to run outside this app).
    cmd += admin_extra_args(s.get("extra_args"))
    cmd += model_extra_args(profile)
    # The app's own settings come LAST: the last flag wins, so nothing above can move the server off 127.0.0.1, change the
    # port the app talks to, or rename the model the chat code asks for. No --api-key (the loopback address is the guard).
    cmd += ["--host", "127.0.0.1", "--port", str(LLAMA_SERVER_PORT), "--served-model-name", served_name(profile)]
    return cmd


def _offline_ok(profile: dict) -> bool:
    """True when nothing has to be fetched from Hugging Face: the model is a local folder and the tokenizer (if any) is too."""
    try:
        if not Path(str(profile.get("model_path") or "")).is_dir():
            return False
        tok = str(profile.get("vllm_tokenizer") or "").strip()
        return not tok or os.path.isabs(resolve_tokenizer(tok))
    except (ValueError, OSError):
        return False


def build_env(profile: dict) -> dict:
    s = settings()
    env = dict(os.environ)
    env.update(PRIVACY_ENV)
    if _offline_ok(profile):
        env["HF_HUB_OFFLINE"] = "1"          # a local folder needs no Hugging Face call; nothing about it should leave
        env["TRANSFORMERS_OFFLINE"] = "1"
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
