"""
core/lanes.py - lane registry + job ("role") routing.

A *lane* is one model endpoint:
  main                      the big local llama-server (port 8090), optionally cloud-bound
  executor / vision / embedder
                            built-in small local lanes (app.json small_models), the first
                            two optionally cloud-bound per user (core/cloud.py)
  <custom local lane>       any extra app.json small_models entry (admin, shared hardware)
  <custom cloud lane>       a user's own lane bound to one of their cloud models
                            (config/providers/user_<id>.json "lanes")

A *job* is a fixed kind of work the code does (summarize a chat, write a commit
message, check an answer...). Call sites never name a lane: they ask
`targets(job)` / `post_chat(job, ...)`, which look the job up in the role map
(app.json "role_map", then the user's own "role_map"), check the lane can do that
kind of work, and return the lane plus its fallback chain:

  mapped lane (cloud binding, then its local model) -> lane "fallback" -> ... ->
  the job's default lane -> main

Unmapped jobs use their default lane, so behavior is unchanged until a user maps one.
"""

import re
import sys
import time
from typing import Optional

from .config import CLOUD_LANES

# fixed at code level: what each job needs and where it runs by default
#   kind        chat | vision | embed -- the lane must be able to do this
#   local_only  never sent to a cloud provider (privacy / index compatibility)
JOBS = {
    "agent.reason": {"kind": "chat", "default": "main", "label": "Thinking & planning",
                     "hint": "Agent steps that need the strongest model: planning, hard reasoning, recovering when stuck."},
    "agent.tool_step": {"kind": "chat", "default": "executor", "label": "Routine tool calls",
                        "hint": "Quick agent steps like reading files, searching and small edits."},
    "summarize": {"kind": "chat", "default": "executor", "label": "Summarizing chats",
                  "hint": "Writes the summary when a long chat is compacted (/compact)."},
    "commit_msg": {"kind": "chat", "default": "executor", "label": "Commit & PR messages",
                   "hint": "Writes git commit messages and pull-request descriptions from a diff."},
    "subagent": {"kind": "chat", "default": "executor", "label": "Sub-agents",
                 "hint": "Helper agents the main agent starts for a focused sub-task (when no role picks a model)."},
    "verify": {"kind": "chat", "default": "main", "label": "Checking answers",
               "hint": "Double-checks an answer against the question and sources before or after you see it."},
    "search_rewrite": {"kind": "chat", "default": "executor", "label": "Web search queries",
                       "hint": "Turns your question into a short web search. Only used when that model is already running.",
                       "local_only": True},
    "input_guard": {"kind": "chat", "default": "executor", "label": "Policy checks",
                    "hint": "Checks messages against your organization's policy rules. Always on this PC.",
                    "local_only": True},
    "vision": {"kind": "vision", "default": "vision", "label": "Reading images",
               "hint": "Describes pictures and screenshots you attach."},
    "embed": {"kind": "embed", "default": "embedder", "label": "Search memory",
              "hint": "Turns documents into vectors for memory and knowledge search. Always on this PC; "
                      "switching models needs a re-index.",
              "local_only": True},
    # media jobs: no default model - off until someone adds one (core/media.py)
    "image_gen": {"kind": "image_gen", "default": None, "label": "Making images", "media": True,
                  "hint": "Creates pictures from a description (/image in chat, or the agent's generate_image tool)."},
    "video_gen": {"kind": "video_gen", "default": None, "label": "Making videos", "media": True,
                  "hint": "Creates short video clips from a description (/video in chat, or the agent)."},
    "transcribe": {"kind": "stt", "default": None, "label": "Speech to text", "media": True,
                   "hint": "Turns your voice (the mic button) and audio files into text. Stays on this PC "
                           "unless an admin allows cloud speech.",
                   "local_first": True},
}

BUILTIN_LANES = {
    "main": {"kind": "chat", "label": "Main brain"},
    "executor": {"kind": "chat", "label": "Fast helper"},
    "vision": {"kind": "vision", "label": "Image reader"},
    "embedder": {"kind": "embed", "label": "Search memory"},
}

# a vision lane is a chat model too (it answers text-only prompts fine)
_KIND_OK = {"chat": ("chat", "vision"), "vision": ("vision",), "embed": ("embed",),
            "image_gen": ("image_gen",), "video_gen": ("video_gen",), "stt": ("stt",)}
MEDIA_KINDS = ("image_gen", "video_gen", "stt")
KIND_NEED = {"chat": "a text model", "vision": "an image model", "embed": "an embedding model",
             "image_gen": "an image maker", "video_gen": "a video maker", "stt": "a speech-to-text model"}

LANE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def kind_ok(job_kind: str, lane_kind: str) -> bool:
    return lane_kind in _KIND_OK.get(job_kind, (job_kind,))


# ---------------- registry ----------------

def registry(user_id: Optional[int] = None) -> dict:
    """name -> {name, kind, label, fallback, builtin, local, cloud_key, owner}.
    `local`: a local model backs it; `cloud_key`: its effective cloud binding."""
    from . import cloud
    from .small_model import APP_CONFIG, lane_kind_of, lane_engine_of
    out = {}
    sm = APP_CONFIG.get("small_models") or {}
    for name, meta in BUILTIN_LANES.items():
        cfg = sm.get(name) or {}
        out[name] = {"name": name, "kind": meta["kind"],
                     "label": str(cfg.get("label") or meta["label"]),
                     "fallback": cfg.get("fallback") or (None if name == "main" else "main"),
                     "builtin": True, "local": True, "owner": "shared"}
    for name, cfg in sm.items():
        if name in out or not isinstance(cfg, dict):
            continue
        kind = lane_kind_of(name, cfg)
        out[name] = {"name": name, "kind": kind,
                     "label": str(cfg.get("label") or name),
                     # media lanes never fall back to the (text) main model
                     "fallback": cfg.get("fallback") or (None if kind in MEDIA_KINDS else "main"),
                     "builtin": False, "local": True, "owner": "shared",
                     "engine": lane_engine_of(name, cfg)}
    for name, d in cloud.user_lanes(user_id).items():
        if name in out:
            continue                      # a user can never shadow a shared lane
        k = str(d.get("kind") or "chat")
        k = k if k in _KIND_OK else "chat"
        out[name] = {"name": name, "kind": k,
                     "label": str(d.get("label") or name),
                     "fallback": d.get("fallback") or (None if k in MEDIA_KINDS else "main"),
                     "builtin": False, "local": False, "owner": "user", "engine": "cloud"}
    for name, d in out.items():
        d.setdefault("engine", "llama")
        cm = None
        if d["kind"] != "embed":
            try:
                cm = cloud.cloud_lane(name, user_id)
            except Exception:
                cm = None
        d["cloud_key"] = cm.key if cm else None
        d["cloud_display"] = cm.display if cm else None
    return out


def role_map(user_id: Optional[int] = None) -> dict:
    """Effective job -> lane for every job (defaults filled in)."""
    from . import cloud
    rm = cloud.role_map(user_id)
    out = {}
    for job, spec in JOBS.items():
        v = rm.get(job) or spec["default"]
        out[job] = str(v) if v else None      # None: a media job nobody has set up
    return out


def allow_cloud_audio() -> bool:
    from .small_model import APP_CONFIG
    return bool((APP_CONFIG.get("media") or {}).get("allow_cloud_audio", False))


def validate_mapping(job: str, lane: str, user_id: Optional[int] = None) -> Optional[str]:
    """None when `lane` can do `job`, else a plain-language reason."""
    spec = JOBS.get(job)
    if spec is None:
        return f"unknown job '{job}'"
    d = registry(user_id).get(lane)
    if d is None:
        return f"there is no model called '{lane}'"
    if not kind_ok(spec["kind"], d["kind"]):
        return f"'{spec['label']}' needs {KIND_NEED[spec['kind']]}"
    if spec.get("local_only") and not d["local"]:
        return f"'{spec['label']}' always runs on this PC, and '{d['label']}' is a cloud model"
    if spec.get("local_first") and not d["local"] and not allow_cloud_audio():
        # audio can't be scanned for card numbers before it leaves (PCI DSS)
        return (f"'{spec['label']}' stays on this PC until an admin allows cloud speech "
                "(Settings -> Models & Jobs)")
    if job == "agent.reason" and d["local"] and lane not in ("main",) and not d["cloud_key"]:
        return "'Thinking & planning' needs the main model or a cloud model"
    return None


# ---------------- resolution ----------------

class Target:
    """One step of a job's route: a lane served by the cloud (cm set) or locally."""

    def __init__(self, lane: str, cm=None):
        self.lane = lane
        self.cm = cm

    @property
    def is_cloud(self) -> bool:
        return self.cm is not None

    @property
    def source(self) -> str:
        return "cloud" if self.cm else "local"

    def describe(self) -> str:
        if self.cm:
            return f"cloud-{self.lane}:{self.cm.display}"
        if self.lane == "main":
            return "main"
        inst = _inst(self.lane)
        if inst is not None and hasattr(inst, "describe"):
            return f"{self.lane}:{inst.describe()}"
        return f"{self.lane}:{inst.model_path.name if inst and inst.model_path else '?'}"

    @property
    def inst(self):
        """The local instance behind this step (None for cloud / main)."""
        return None if (self.cm or self.lane == "main") else _inst(self.lane)

    def available(self) -> bool:
        """Cheap check - no model is loaded."""
        if self.cm:
            return True
        if self.lane == "main":
            from .state import state
            return state.client is not None and state.process is not None and state.process.poll() is None
        inst = _inst(self.lane)
        return bool(inst and inst.available)

    def is_up(self) -> bool:
        if self.cm:
            return True
        if self.lane == "main":
            return self.available()
        inst = _inst(self.lane)
        return bool(inst and inst.is_up())

    async def client(self):
        """A client with .post()/.stream(); loads a local model on demand."""
        from . import cloud
        if self.cm:
            return cloud.CloudClient(self.cm)
        if self.lane == "main":
            from .state import state
            if not self.available():
                raise RuntimeError("main model is not running")
            return state.client
        inst = _inst(self.lane)
        if not inst or not inst.available:
            raise RuntimeError(f"model '{self.lane}' is not configured or its files are missing")
        await inst.ensure_loaded()
        inst.last_used = time.time()
        return inst.client

    def __repr__(self) -> str:
        return f"<Target {self.lane} {'cloud:' + self.cm.key if self.cm else 'local'}>"


def _inst(lane: str):
    from .small_model import small_models
    return small_models.instances.get(lane)


def targets(job: str, user_id: Optional[int] = None, force_local: bool = False) -> list:
    """Ordered route for a job: mapped lane first, then its fallback chain."""
    from . import cloud
    spec = JOBS[job]
    reg = registry(user_id)
    mapped = role_map(user_id).get(job) or spec["default"]
    if mapped and validate_mapping(job, mapped, user_id):
        mapped = spec["default"]
    if not mapped:
        return []                      # a media job nobody has set up
    local_only = force_local or bool(spec.get("local_only"))
    no_cloud_audio = bool(spec.get("local_first")) and not allow_cloud_audio()

    chain, seen = [], set()
    lane = mapped
    while lane and lane not in seen and lane in reg:
        seen.add(lane)
        chain.append(lane)
        lane = reg[lane].get("fallback")
    for extra in (spec["default"], "main"):
        if extra and extra not in seen and extra in reg:
            seen.add(extra)
            chain.append(extra)

    fb_local = bool(cloud.cloud_bindings(user_id).get("fallback_local", True))
    out = []
    for name in chain:
        d = reg[name]
        if not kind_ok(spec["kind"], d["kind"]):
            continue
        if d["cloud_key"] and not local_only and not no_cloud_audio:
            cm = cloud.cloud_lane(name, user_id)
            if cm:
                out.append(Target(name, cm))
        if d["local"]:
            out.append(Target(name))
    if out and out[0].is_cloud and not fb_local:
        return out[:1]                 # user turned local fallback off
    return out


def primary(job: str, user_id: Optional[int] = None, force_local: bool = False) -> Optional[Target]:
    """First usable step of a job's route, without loading anything."""
    for t in targets(job, user_id, force_local):
        if t.available():
            return t
    return None


async def post_chat(job: str, payload: dict, user_id: Optional[int] = None,
                    force_local: bool = False, only_if_running: bool = False,
                    timeout=None) -> tuple:
    """Non-streaming chat completion for `job`, walking the fallback chain.
    -> (response json, Target used). Raises RuntimeError when nothing can serve it."""
    errors = []
    for t in targets(job, user_id, force_local):
        if only_if_running and not t.is_up():
            continue
        if not t.available():
            continue
        try:
            c = await t.client()
            r = await c.post("/v1/chat/completions", json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json(), t
        except Exception as e:
            errors.append(f"{t.describe()}: {type(e).__name__}: {e}")
            print(f"[lanes] {job} via {t.describe()} failed - trying next ({e})", file=sys.stderr)
    raise RuntimeError(f"no model available for '{JOBS[job]['label']}'"
                       + (f" ({'; '.join(errors)})" if errors else ""))


def message_text(data: dict) -> str:
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


# ---------------- editing ----------------

def public_view(user_id: Optional[int] = None, is_admin: bool = False) -> dict:
    """Everything Settings -> Models needs, in plain language."""
    from .small_model import small_models, APP_CONFIG
    reg = registry(user_id)
    status = small_models.status()
    sm = APP_CONFIG.get("small_models") or {}
    lanes = []
    for name, d in reg.items():
        row = dict(d)
        if d["local"] and name != "main":
            st = status.get(name) or {}
            cfg = sm.get(name) or {}
            row.update({"model": cfg.get("model"), "mmproj": cfg.get("mmproj"), "port": cfg.get("port"),
                        "gpu": cfg.get("gpu"), "ctx": cfg.get("ctx"), "idle_unload_s": cfg.get("idle_unload_s"),
                        "loaded": st.get("loaded"), "files_ok": st.get("available"), "error": st.get("error")})
            if d.get("engine") == "whisper":
                row.update({"threads": cfg.get("threads"), "language": cfg.get("language")})
            elif d.get("engine") == "sdcpp":
                row.update({k: cfg.get(k) for k in SD_FILE_KEYS + (
                    "steps", "cfg_scale", "flow_shift", "sampler", "offload_to_cpu", "vae_tiling", "timeout_s",
                    "edit_refs", "max_refs", "default_size")})
                row.update({"state": st.get("state"), "loading_since": st.get("loading_since")})
                inst = small_models.instances.get(name)
                if inst is not None and hasattr(inst, "edit_caps"):
                    row["edit_caps"] = inst.edit_caps()
        elif name == "main":
            from .state import state
            row.update({"loaded": Target("main").available(),
                        "model": (state.profile or {}).get("model_path") if state.profile else None})
        if not d["local"]:
            from . import cloud
            row["cloud_binding"] = (cloud.user_lanes(user_id).get(name) or {}).get("cloud")
        row["status"] = _status_text(row)
        row["used_by"] = [j for j, ln in role_map(user_id).items() if ln == name]
        lanes.append(row)
    return {
        "lanes": lanes,
        "jobs": [{"job": j, **{k: v for k, v in s.items()}} for j, s in JOBS.items()],
        "role_map": role_map(user_id),
        "defaults": {j: s["default"] for j, s in JOBS.items()},
        "can_edit_local": is_admin,
        "media": _media_view(),
    }


def _media_view() -> dict:
    from .small_model import (find_whisper_server, whisper_search_dirs, find_sd_server,
                              sd_search_dirs, APP_CONFIG)
    from . import profiles
    m = APP_CONFIG.get("media") or {}
    exe = find_whisper_server()
    return {"allow_cloud_audio": allow_cloud_audio(),
            "whisper_found": exe is not None,
            "whisper_dirs": [str(p) for p in whisper_search_dirs()],
            "sd_found": find_sd_server() is not None,
            "sd_dirs": [str(p) for p in sd_search_dirs()],
            "helper_dir": str(profiles.helper_root()),
            "media_dirs": {k: str(profiles.media_root(k)) for k in profiles.MEDIA_DIR_NAMES},
            "limits": m.get("limits") or {}}


def _status_text(row: dict) -> dict:
    """{level: ok|idle|warn|error, text} for the status badge."""
    if row.get("cloud_key"):
        return {"level": "ok", "text": f"Cloud: {row.get('cloud_display') or row['cloud_key']}"}
    if not row.get("local"):
        return {"level": "warn", "text": "Cloud model not set or removed - using its backup model"}
    if row["name"] == "main":
        return ({"level": "ok", "text": "Running"} if row.get("loaded")
                else {"level": "idle", "text": "Not loaded (start it from the top bar)"})
    if row.get("error"):
        return {"level": "error", "text": f"Failed to start: {row['error']}"}
    if row.get("engine") == "sdcpp":
        if not row.get("files_ok"):
            from .small_model import find_sd_server
            if find_sd_server() is None:
                return {"level": "error", "text": "stable-diffusion.cpp isn't installed yet - see the setup steps"}
            return {"level": "error", "text": "A model file is missing - pick the files again"}
        st = row.get("state")
        if st == "loaded":
            return {"level": "ok", "text": "Loaded - ready to make images" if row["kind"] == "image_gen"
                    else "Loaded - ready to make videos"}
        if st == "loading":
            return {"level": "idle", "text": "Loading..."}
        if row.get("error"):
            return {"level": "error", "text": f"Failed to load: {row['error']}"}
        return {"level": "idle", "text": "Not loaded - press Load to use it"}
    if row.get("engine") == "whisper" and not row.get("files_ok"):
        from .small_model import find_whisper_server
        if find_whisper_server() is None:
            return {"level": "error", "text": "Whisper isn't installed yet - see the setup steps"}
        return {"level": "error", "text": "Whisper model file missing - pick a .bin model"}
    if not row.get("files_ok"):
        return {"level": "error", "text": "Model file missing - pick a model file"}
    return ({"level": "ok", "text": "Running"} if row.get("loaded")
            else {"level": "idle", "text": "Sleeping (starts on first use)"})


def _check_fallback(name: str, fallback: Optional[str], reg: dict) -> Optional[str]:
    if not fallback:
        return None
    if fallback == name:
        return "a model can't be its own backup"
    if fallback not in reg:
        return f"backup model '{fallback}' doesn't exist"
    seen, cur = {name}, fallback
    while cur:
        if cur in seen:
            return "that backup choice would loop back to this model"
        seen.add(cur)
        cur = (reg.get(cur) or {}).get("fallback")
    return None


def _apply_fallback(name: str, kind: str, cur: dict, fallback: Optional[str], reg: dict) -> None:
    """Validate + set a lane's backup. Media lanes can only back up to the same kind."""
    reg = dict(reg)
    reg.setdefault(name, {"fallback": None, "kind": kind})
    err = _check_fallback(name, fallback, reg)
    if err:
        raise ValueError(err)
    if fallback and kind in MEDIA_KINDS and reg[fallback].get("kind") != kind:
        raise ValueError(f"the backup must also be {KIND_NEED[kind]}")
    if fallback:
        cur["fallback"] = fallback
    else:
        cur.pop("fallback", None)


def _set_port_gpu(name: str, cur: dict, data: dict, sm: dict, cpu_ok: bool = False) -> None:
    """Validate + set a local lane's port (free, not the app's) and GPU (-1 = CPU when allowed)."""
    from .config import LLAMA_SERVER_PORT, ACTIVE_RUNTIME
    port = int(data.get("port") or cur.get("port") or suggest_port())
    if not (1024 < port < 65536):
        raise ValueError("port must be between 1025 and 65535")
    if port in (LLAMA_SERVER_PORT, 8000):
        raise ValueError(f"port {port} is used by the main server")
    for other, ocfg in sm.items():
        if other != name and isinstance(ocfg, dict) and int(ocfg.get("port") or 0) == port:
            raise ValueError(f"port {port} is already used by '{(ocfg.get('label') or other)}'")
    cur["port"] = port
    if data.get("gpu") is not None:
        gpu = int(data["gpu"])
        if gpu not in (ACTIVE_RUNTIME.get("gpu_devices") or []) and not (cpu_ok and gpu == -1):
            raise ValueError(f"GPU {gpu} isn't available in the active runtime")
        cur["gpu"] = gpu
    cur.setdefault("gpu", ACTIVE_RUNTIME.get("small_model_gpu") or 1)


SD_FILE_KEYS = ("diffusion_model", "llm", "vae", "llm_vision")
SD_SAMPLER_RE = re.compile(r"^[a-z0-9_+]{2,24}$")


def _save_sdcpp_lane(name: str, kind: str, cur: dict, data: dict) -> dict:
    """Image/video maker run on this PC by stable-diffusion.cpp (sd-server, Vulkan).
    Every file must be in Models/orchestrator/image-models (or video-models)."""
    from .config import update_app_config
    from . import profiles
    from .small_model import APP_CONFIG, small_models
    from pathlib import Path
    sm = APP_CONFIG.get("small_models") or {}
    base = Path(APP_CONFIG.get("models_dir") or "E:/AI/Models")
    folder = profiles.media_root(kind)
    what = {"diffusion_model": "diffusion model", "llm": "text encoder", "vae": "VAE",
            "llm_vision": "vision weights"}

    def _file(v, key):
        p = Path(str(v))
        full = p if p.is_absolute() else base / p
        if full.suffix.lower() not in (".gguf", ".safetensors"):
            raise ValueError(f"the {what[key]} must be a .gguf or .safetensors file")
        if not profiles.in_media_dir(full, kind):
            raise ValueError(f"the {what[key]} must be in {folder}")
        if not full.exists():
            raise ValueError(f"{what[key]} not found: {full.name}")
        try:
            return str(full.relative_to(base)).replace("\\", "/")
        except ValueError:
            return str(full)

    for key in SD_FILE_KEYS:
        if key in data:
            v = str(data.get(key) or "").strip()
            if v:
                cur[key] = _file(v, key)
            else:
                cur.pop(key, None)
    if not cur.get("diffusion_model"):
        raise ValueError("pick the diffusion model file")
    _set_port_gpu(name, cur, data, sm, cpu_ok=True)
    for key, lo, hi, typ in (("steps", 1, 150, int), ("cfg_scale", 0.0, 30.0, float),
                             ("flow_shift", 0.0, 20.0, float), ("timeout_s", 30, 7200, int)):
        if data.get(key) is not None and data.get(key) != "":
            v = typ(data[key])
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            cur[key] = v
        elif key in data:
            cur.pop(key, None)
    if data.get("sampler") is not None:
        smp = str(data.get("sampler") or "").strip().lower()
        if smp and not SD_SAMPLER_RE.match(smp):
            raise ValueError("sampler must be a name like euler, euler_a or dpm++2m")
        if smp:
            cur["sampler"] = smp
        else:
            cur.pop("sampler", None)
    for key in ("offload_to_cpu", "vae_tiling", "edit_refs"):
        if data.get(key) is not None:
            cur[key] = bool(data[key])
    if data.get("default_size") is not None:
        ds = str(data.get("default_size") or "").strip().lower()
        if ds and ds not in ("small", "medium", "large", "xlarge"):
            raise ValueError("default size must be small, medium, large or xlarge")
        if ds and cur.get("kind", data.get("kind")) != "video_gen":
            cur["default_size"] = ds
        else:
            cur.pop("default_size", None)
    if data.get("max_refs") not in (None, ""):
        mr = int(data["max_refs"])
        if not 1 <= mr <= 10:
            raise ValueError("max reference pictures must be between 1 and 10")
        cur["max_refs"] = mr
    # keys of other engines (and of the old "own service" engine) don't apply here
    for k in ("url", "auth_ref", "body_template", "model", "mmproj", "ctx", "np", "idle_unload_s"):
        cur.pop(k, None)
    cur.update({"kind": kind, "engine": "sdcpp"})
    if data.get("label") is not None:
        cur["label"] = str(data["label"]).strip()[:40] or name
    if "fallback" in data:
        _apply_fallback(name, kind, cur, data.get("fallback") or None, registry())

    def _mut(cfg):
        cfg.setdefault("small_models", {})[name] = cur
    update_app_config(_mut)
    small_models.reconfigure(name, cur)
    return cur


def save_local_lane(name: str, data: dict) -> dict:
    """Create/update a shared local lane (admin). Validates, persists, applies live."""
    from .config import update_app_config, LLAMA_SERVER_PORT, ACTIVE_RUNTIME
    from . import profiles
    from .small_model import APP_CONFIG, small_models, BUILTIN_KINDS
    from pathlib import Path
    if name == "main":
        raise ValueError("the main model is configured from the top bar")
    sm = APP_CONFIG.get("small_models") or {}
    is_new = name not in sm
    if is_new and not LANE_NAME_RE.match(name):
        raise ValueError("name must be 2-32 lowercase letters, digits, '-' or '_', starting with a letter")
    if is_new and name in registry():
        raise ValueError(f"'{name}' is already used")
    cur = dict(sm.get(name) or {})
    kind = BUILTIN_KINDS.get(name) or str(data.get("kind") or cur.get("kind") or "chat")
    if kind not in _KIND_OK:
        raise ValueError("type must be chat, vision, embed, image_gen, video_gen or stt")
    if not is_new and cur.get("kind") and cur.get("kind") != kind:
        raise ValueError("a model's type can't be changed - add a new one instead")
    if kind in ("image_gen", "video_gen"):
        if str(data.get("engine") or "sdcpp") != "sdcpp":
            raise ValueError("images and videos on this PC are made by stable-diffusion.cpp (engine sdcpp)")
        return _save_sdcpp_lane(name, kind, cur, data)
    models_base = Path(APP_CONFIG.get("models_dir") or "E:/AI/Models")
    # whisper.cpp models are ggml .bin files; everything else is llama.cpp .gguf
    model_ext = ".bin" if kind == "stt" else ".gguf"

    def _model_path(v, what):
        if not v:
            return None
        p = Path(str(v))
        full = p if p.is_absolute() else models_base / p
        if full.suffix.lower() != model_ext:
            raise ValueError(f"{what} must be a {model_ext} file")
        # every model except the main one comes from Models/orchestrator; whisper
        # models from its voice-models folder, text helpers from outside the media folders
        if kind == "stt":
            if not profiles.in_media_dir(full, "stt"):
                raise ValueError(f"{what} must be in the voice models folder ({profiles.media_root('stt')})")
        elif not profiles.in_helper_dir(full) or any(
                profiles.in_media_dir(full, k) for k in profiles.MEDIA_DIR_NAMES):
            raise ValueError(f"{what} must be in the orchestrator folder ({profiles.helper_root()})")
        if not full.exists():
            raise ValueError(f"{what} not found: {full.name}")
        try:
            return str(full.relative_to(models_base)).replace("\\", "/")
        except ValueError:
            return str(full)

    if "model" in data:
        cur["model"] = _model_path(data.get("model"), "model file")
    if kind == "vision" and "mmproj" in data:
        cur["mmproj"] = _model_path(data.get("mmproj"), "image projector (mmproj)")
    if not cur.get("model"):
        raise ValueError("pick a model file")
    if kind == "vision" and not cur.get("mmproj"):
        raise ValueError("an image model also needs its mmproj file")

    _set_port_gpu(name, cur, data, sm, cpu_ok=(kind == "stt"))   # whisper can also run on the CPU
    for k, lo, hi in (("ctx", 512, 262144), ("idle_unload_s", 0, 86400), ("np", 1, 16), ("threads", 1, 64)):
        if data.get(k) is not None:
            v = int(data[k])
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            cur[k] = v
    if kind == "stt":
        cur["engine"] = "whisper"
        cur.pop("ctx", None)
        if "language" in data:
            lang = str(data.get("language") or "auto").strip().lower()
            if lang != "auto" and not re.match(r"^[a-z]{2,3}$", lang):
                raise ValueError("language must be 'auto' or a language code like 'en' or 'bn'")
            cur["language"] = lang
        cur.setdefault("language", "auto")
    else:
        cur.setdefault("ctx", 8192)
    if data.get("label") is not None:
        cur["label"] = str(data["label"]).strip()[:40] or name
    if name not in BUILTIN_KINDS:
        cur["kind"] = kind
    if "fallback" in data:
        _apply_fallback(name, kind, cur, data.get("fallback") or None, registry())

    def _mut(cfg):
        cfg.setdefault("small_models", {})[name] = cur
    update_app_config(_mut)
    small_models.reconfigure(name, cur)
    return cur


def delete_local_lane(name: str, move_jobs_to: Optional[str] = None) -> None:
    from .config import update_app_config
    from .small_model import small_models, BUILTIN_KINDS
    if name in BUILTIN_LANES or name in BUILTIN_KINDS:
        raise ValueError("built-in models can't be removed, only reconfigured")
    # jobs can only move to a model that can do them
    movable = set()
    if move_jobs_to:
        kind = (registry().get(name) or {}).get("kind")
        tgt = (registry().get(move_jobs_to) or {}).get("kind")
        if kind and tgt and (kind == tgt or kind_ok(kind, tgt)):
            movable.add(move_jobs_to)

    def _mut(cfg):
        (cfg.get("small_models") or {}).pop(name, None)
        rm = cfg.get("role_map") or {}
        for j, ln in list(rm.items()):
            if ln == name:
                if move_jobs_to and move_jobs_to in movable:
                    rm[j] = move_jobs_to
                else:
                    rm.pop(j)      # the new model can't do this job: back to its default
        for other in (cfg.get("small_models") or {}).values():
            if isinstance(other, dict) and other.get("fallback") == name:
                other.pop("fallback", None)
    update_app_config(_mut)
    small_models.reconfigure(name, None)


def save_user_lane(user_id: int, name: str, data: dict) -> dict:
    """Create/update one of this user's own cloud lanes."""
    from . import cloud
    own = dict(cloud.user_lanes(user_id))
    reg = registry(user_id)
    is_new = name not in own
    if is_new:
        if not LANE_NAME_RE.match(name):
            raise ValueError("name must be 2-32 lowercase letters, digits, '-' or '_', starting with a letter")
        if name in reg:
            raise ValueError(f"'{name}' is already used")
    cur = dict(own.get(name) or {})
    kind = str(data.get("kind") or cur.get("kind") or "chat")
    if kind not in ("chat", "vision", "image_gen", "video_gen", "stt"):
        raise ValueError("cloud models can read or write text, read images, make images or videos, "
                         "or turn speech into text (embeddings stay on this PC)")
    if kind == "stt" and not allow_cloud_audio():
        raise ValueError("speech to text stays on this PC until an admin allows cloud speech")
    if not is_new and cur.get("kind") and cur.get("kind") != kind:
        raise ValueError("a model's type can't be changed - add a new one instead")
    cur["kind"] = kind
    key = str(data.get("cloud") or cur.get("cloud") or "").strip()
    if not key or cloud.get_cloud(key, user_id) is None:
        raise ValueError("pick one of your cloud models (add a provider first if the list is empty)")
    cur["cloud"] = key
    if data.get("label") is not None:
        cur["label"] = str(data["label"]).strip()[:40] or name
    cur.setdefault("label", name)
    if "fallback" in data:
        _apply_fallback(name, kind, cur, data.get("fallback") or None, reg)
    if kind not in MEDIA_KINDS:
        cur.setdefault("fallback", "main")
    own[name] = cur
    cloud.write_user_section(user_id, "lanes", own)
    return cur


def delete_user_lane(user_id: int, name: str, move_jobs_to: Optional[str] = None) -> None:
    from . import cloud
    own = dict(cloud.user_lanes(user_id))
    if name not in own:
        raise ValueError(f"'{name}' is not one of your models")
    own.pop(name)
    for d in own.values():
        if d.get("fallback") == name:
            if d.get("kind") in MEDIA_KINDS:
                d.pop("fallback", None)
            else:
                d["fallback"] = "main"
    cloud.write_user_section(user_id, "lanes", own)
    rm = dict(_own_role_map(user_id))
    for j, ln in list(rm.items()):
        if ln == name:
            if move_jobs_to and not validate_mapping(j, move_jobs_to, user_id):
                rm[j] = move_jobs_to
            else:
                rm.pop(j)      # the new model can't do this job: back to its default
    cloud.write_user_section(user_id, "role_map", rm)


def _own_role_map(user_id: int) -> dict:
    from . import cloud
    d = cloud._read_json(cloud._provider_file(user_id)).get("role_map")
    return d if isinstance(d, dict) else {}


def set_role_map(user_id: int, updates: dict, as_default: bool = False) -> dict:
    """Map jobs to lanes. None / "" resets a job to its default. `as_default`
    (admin) writes app.json so it applies to everyone without their own choice."""
    reg = registry(user_id)
    for job, lane in updates.items():
        if job not in JOBS:
            raise ValueError(f"unknown job '{job}'")
        if lane:
            err = validate_mapping(job, lane, user_id)
            if err:
                raise ValueError(err)
            if as_default and reg[lane]["owner"] == "user":
                # someone's own cloud model doesn't exist for other users
                raise ValueError(f"'{reg[lane]['label']}' is your own cloud model, so it can't be "
                                 "the default for everyone")
    if as_default:
        from .config import update_app_config

        def _mut(cfg):
            rm = cfg.setdefault("role_map", {})
            for job, lane in updates.items():
                if lane:
                    rm[job] = lane
                else:
                    rm.pop(job, None)
        update_app_config(_mut)
    else:
        from . import cloud
        rm = dict(_own_role_map(user_id))
        for job, lane in updates.items():
            if lane:
                rm[job] = lane
            else:
                rm.pop(job, None)
        cloud.write_user_section(user_id, "role_map", rm)
    return role_map(user_id)


def suggest_port() -> int:
    from .small_model import APP_CONFIG
    used = {int((c or {}).get("port") or 0) for c in (APP_CONFIG.get("small_models") or {}).values()
            if isinstance(c, dict)}
    p = 8094
    while p in used:
        p += 1
    return p
