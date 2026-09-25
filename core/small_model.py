import asyncio
import base64
import collections
import json
import subprocess
import sys
import time
import re
from pathlib import Path
from typing import Optional, Union

import httpx

from .backend import device_prefix
from .config import ACTIVE_RUNTIME, BASE_DIR, LLAMA_SERVER_PORT, CONFIG_FILE, ROLES_FILE, CONFIG_DEFAULTS
from .process import find_llama_server
from . import vram


# No server-side workspace root: project folders live on users' machines and
# are reached only through the companion app (core/agent_tools.py
# require_device_workspace). The old WORKSPACE_ROOT (E:\AI\workspace) is gone.


def _load_common_root() -> Path:
    cfg = CONFIG_FILE
    try:
        if cfg.exists():
            d = json.loads(cfg.read_text())
            v = d.get("common_dir")
            if v:
                p = Path(v)
                p.mkdir(parents=True, exist_ok=True)
                return p.resolve()
    except Exception:
        pass
    p = Path("E:\\AI\\common")
    if not p.parent.exists():
        p = BASE_DIR / "common"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


COMMON_ROOT = _load_common_root()


# Fallback role definitions, used when config/roles.json is missing entirely.
_DEFAULT_ROLES = {
    "planner": {
        "lane": "main", "max_steps": 6,
        "tools": ["list_files", "read_file", "grep", "search_memory", "list_skills",
                  "read_skill", "web_fetch", "web_search"],
        "system_prompt": "You are a planning sub-agent. Investigate read-only, then return a concrete numbered plan. Do not write or edit files.",
    },
    "coder": {
        "lane": "executor", "max_steps": 10,
        "tools": ["write_file", "read_file", "edit_file", "list_files", "grep", "run_python"],
        "system_prompt": "You are a focused implementation sub-agent. Make the exact edits described in your task, then report what changed.",
    },
    "reviewer": {
        "lane": "main", "max_steps": 6,
        "tools": ["list_files", "read_file", "grep"],
        "system_prompt": "You are a code-review sub-agent. Read the referenced files/diff and report concrete issues found -- do not modify anything.",
    },
}


def _load_app_config() -> dict:
    cfg = CONFIG_FILE
    base = {
        "models_dir": None,
        "workspace_dir": None,
        "common_dir": None,
        "llama_bin_dir": ACTIVE_RUNTIME["llama_bin_dir"],
        "backend": ACTIVE_RUNTIME["backend"],
        "small_models": {
            "executor": {"model": None, "port": 8091, "gpu": 1, "ctx": 8192},
            "vision": {"model": None, "mmproj": None, "port": 8092, "gpu": 1, "ctx": 4096},
            "embedder": {"model": None, "port": 8093, "gpu": 1},
        },
        "router": {
            "enabled": True,
            "confidence_threshold": 0.7,
            "engine": "cactus_needle",
            "executor_grammar": True,
            "laya": {
                "checkpoint": "convaiinnovations/laya",
                "subfolder": None,
                "device": "cpu",
                "preload": False,
            },
        },
        "agent": {"exec_timeout_s": 120, "max_steps": 60, "idle_unload_s": 120},
        "roles": dict(_DEFAULT_ROLES),
        # cloud providers / lane bindings: kept here so /control/models can report
        # them without importing the UI-managed providers.json directly
        # (core.cloud is the authority; each user's config/providers/user_<id>.json overrides config/app.json)
        "provider": {},
        "cloud": {},
        "capabilities": {
            "web": True, "web_search_api_key": "", "skills": True,
            "mcp": True, "mcp_servers": {}, "plugins": True,
            "shell": {"enabled": True, "ask_first": True, "timeout_s": 60,
                      "allow_patterns": ["git *", "npx *", "npm *", "pip *", "python *"]},
        },
        "input_guard": {
            "enabled": False,
            "rules": [],
        },
        "output_guard": {
            "enabled": False,
            "rules": [],
        },
        # images / videos / speech (core/media.py). Audio can't be scanned for
        # card numbers, so cloud speech-to-text is off until an admin allows it.
        "media": {
            "allow_cloud_audio": False,
            "limits": {"image_per_day": 50, "video_per_day": 5},
            "max_audio_mb": 25,
        },
    }
    # Multiagent role definitions live in their own file so they're easy to find
    # and edit independently of the general app config.
    try:
        if ROLES_FILE.exists():
            roles_d = json.loads(ROLES_FILE.read_text())
            if isinstance(roles_d, dict):
                base["roles"].update(roles_d)
    except Exception as e:
        print(f"[server_manager] roles.json unreadable: {e}", file=sys.stderr)

    try:
        if cfg.exists():
            d = json.loads(cfg.read_text())
            for k in ("models_dir", "workspace_dir", "common_dir"):
                if d.get(k):
                    base[k] = d[k]
            # built-in lanes merge over their defaults; any other entry is a
            # custom local lane added from Settings -> Models (core/lanes.py)
            for k, v in (d.get("small_models") or {}).items():
                if isinstance(v, dict):
                    base["small_models"].setdefault(k, {}).update(v)
            # "roles" is included here as a legacy override: an un-migrated
            # app.json that still has a "roles" block wins over config/roles.json.
            for k in ("router", "agent", "capabilities", "provider", "cloud", "roles",
                      "input_guard", "output_guard", "preflight", "media"):
                if isinstance(d.get(k), dict):
                    sub = base.get(k)
                    if isinstance(sub, dict):
                        sub.update(d[k])
                    else:
                        base[k] = dict(d[k])
            # Preserve any other top-level keys present in app.json
            for k, v in d.items():
                if k not in base:
                    base[k] = v
    except Exception as e:
        print(f"[server_manager] config.json unreadable: {e}", file=sys.stderr)

    return base


APP_CONFIG = _load_app_config()


# kind of each built-in lane; custom lanes carry "kind" in their config entry
BUILTIN_KINDS = {"executor": "chat", "vision": "vision", "embedder": "embed"}


LANE_KINDS = ("chat", "vision", "embed", "image_gen", "video_gen", "stt")


def lane_kind_of(name: str, cfg: Optional[dict] = None) -> str:
    k = str((cfg or {}).get("kind") or BUILTIN_KINDS.get(name) or "chat").lower()
    return k if k in LANE_KINDS else "chat"


def lane_engine_of(name: str, cfg: Optional[dict] = None) -> str:
    """What serves a local lane: llama (llama-server), whisper (whisper-server)
    or sdcpp (stable-diffusion.cpp's sd-server, for images and videos)."""
    kind = lane_kind_of(name, cfg)
    if kind in ("image_gen", "video_gen"):
        return "sdcpp"
    if kind == "stt":
        return "whisper"
    return "llama"


def whisper_search_dirs() -> list:
    """Where whisper-server(.exe) is looked for, first match wins. Only config /
    fixed locations: the UI can't point the server at an arbitrary program."""
    out = []
    v = ACTIVE_RUNTIME.get("whisper_bin_dir")
    if v:
        out.append(Path(v))
    # programs live next to llama-vulkan (E:\AI\vulkan-arc\...), never in the models folders
    llama = Path(ACTIVE_RUNTIME["llama_bin_dir"])
    out += [llama.parent / "whisper-vulkan", llama.parent / "whisper", llama]
    return out


def sd_search_dirs() -> list:
    """Where sd-server(.exe) (stable-diffusion.cpp) is looked for. Config / fixed
    locations only, like whisper-server."""
    out = []
    v = ACTIVE_RUNTIME.get("sd_bin_dir")
    if v:
        out.append(Path(v))
    llama = Path(ACTIVE_RUNTIME["llama_bin_dir"])
    out += [llama.parent / "sd-vulkan", llama.parent / "sd"]
    return out


def find_sd_server() -> Optional[Path]:
    for d in sd_search_dirs():
        for n in ("sd-server.exe", "sd-server"):
            p = d / n
            if p.is_file():
                return p
    return None


def find_whisper_server() -> Optional[Path]:
    for d in whisper_search_dirs():
        for n in ("whisper-server.exe", "whisper-server"):
            p = d / n
            if p.is_file():
                return p
    return None


class SmallModelInstance:
    """One on-demand llama-server child for a small model (executor/vision/embedder
    or a custom local lane)."""

    def __init__(self, role: str, cfg: dict, client_hint=None):
        self.role = role
        self.cfg = cfg or {}
        self.kind = lane_kind_of(role, self.cfg)
        models_base = Path(APP_CONFIG.get("models_dir") or "E:/AI/Models")
        
        raw_m = cfg.get("model")
        if raw_m:
            p = Path(raw_m)
            self.model_path = p if p.is_absolute() else (models_base / p)
        else:
            self.model_path = None

        raw_mm = cfg.get("mmproj")
        if raw_mm:
            p = Path(raw_mm)
            self.mmproj_path = p if p.is_absolute() else (models_base / p)
        else:
            self.mmproj_path = None

        self.port = int(cfg.get("port", 8091))
        self.gpu = int(cfg.get("gpu", 1))
        # a GPU index from another machine's numbering (e.g. Vulkan 1 on a
        # single-GPU CUDA box) falls back to the active runtime's small_model_gpu
        fallback = ACTIVE_RUNTIME.get("small_model_gpu")
        if fallback is not None and self.gpu not in ACTIVE_RUNTIME["gpu_devices"]:
            self.gpu = int(fallback)
        self.ctx = int(cfg.get("ctx", 4096))
        # parallel slots share one unified KV pool of -c tokens (-kvu), so
        # several users' agent steps / embeddings run concurrently
        self.n_slots = max(1, int(cfg.get("np", 1)))
        self.kv_cache_type = str(cfg.get("kv_cache_type") or "")
        self.process: Optional[subprocess.Popen] = None
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", timeout=None)
        self.last_used = 0.0
        self.lock = asyncio.Lock()
        self.load_error: Optional[str] = None
        self._log_tail = collections.deque(maxlen=60)   # last server lines, to explain a failed start

    @property
    def available(self) -> bool:
        if not self.model_path or not self.model_path.exists():
            return False
        if self.kind == "vision" and (not self.mmproj_path or not self.mmproj_path.exists()):
            return False
        return True

    def is_up(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def ensure_loaded(self) -> None:
        if self.is_up():
            self.last_used = time.time()
            return
        async with self.lock:
            if self.is_up():
                self.last_used = time.time()
                return
            if not self.available:
                raise RuntimeError(f"{self.role} model not configured or files missing: {self.model_path}")
            prefix = device_prefix(ACTIVE_RUNTIME["backend"])
            loop = asyncio.get_event_loop()
            await self._preflight(loop)
            cmd, env = self._launch_cmd()

            print(f"[{self.role}] auto-loading on {prefix}{self.gpu} (port {self.port})...")
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            self.load_error = None
            self._log_tail.clear()
            asyncio.create_task(self._pump_logs())

            deadline = time.time() + self._start_timeout
            while time.time() < deadline:
                if self.process.poll() is not None:
                    await asyncio.sleep(0.3)          # let the log pump read the last lines
                    self.load_error = f"exited code {self.process.returncode}"
                    why = self._exit_reason()
                    if why:
                        self.load_error += f" - {why}"
                    self.process = None
                    raise RuntimeError(f"[{self.role}] failed to start ({self.load_error})")
                try:
                    r = await self.client.get(self._health_path, timeout=2.0)
                    if self._healthy(r):
                        print(f"[{self.role}] ready on port {self.port}")
                        self.last_used = time.time()
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            self._stop()
            self.load_error = f"timed out after {self._start_timeout}s"
            raise RuntimeError(f"[{self.role}] health check timed out")

    _health_path = "/health"
    _start_timeout = 90

    def _healthy(self, r) -> bool:
        return r.status_code == 200

    async def _preflight(self, loop) -> None:
        # refuse to spawn if this small model wouldn't fit on its target
        # Vulkan device (prevents the WDDM OOM desktop hang)
        await loop.run_in_executor(
            None,
            lambda: vram.check_small_model_or_raise(
                self.role, self.model_path, self.ctx, self.gpu,
                mmproj_path=self.mmproj_path),
        )

    def _launch_cmd(self) -> tuple:
        """(argv, env or None) for this lane's server process."""
        prefix = device_prefix(ACTIVE_RUNTIME["backend"])
        server_bin = find_llama_server(ACTIVE_RUNTIME["llama_bin_dir"])
        cmd = [
            str(server_bin),
            "-m", str(self.model_path),
            "-c", str(self.ctx),
            "-ngl", "999",
            "-dev", f"{prefix}{self.gpu}",
            "--port", str(self.port),
            "--host", "127.0.0.1",
            "-np", str(self.n_slots),
            "-fa", "on",
            "--jinja",
        ]
        if self.n_slots > 1:
            cmd += ["-kvu"]
        if self.kv_cache_type:
            cmd += ["-ctk", self.kv_cache_type, "-ctv", self.kv_cache_type]
        if self.mmproj_path and self.mmproj_path.exists():
            cmd += ["--mmproj", str(self.mmproj_path)]
        if self.kind == "embed":
            cmd += ["--embedding"]
        return cmd, None

    def _stop(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                               capture_output=True, timeout=10)
            except Exception:
                self.process.terminate()
        self.process = None

    async def unload_if_idle(self) -> bool:
        if not self.is_up():
            return False
        idle_limit = int(self.cfg.get("idle_unload_s") or APP_CONFIG["agent"].get("idle_unload_s", 120))
        if idle_limit <= 0:
            return False
        if time.time() - self.last_used > idle_limit:
            async with self.lock:
                if self.is_up() and (time.time() - self.last_used > idle_limit):
                    print(f"[{self.role}] idle for {idle_limit}s — unloading to reclaim VRAM")
                    self._stop()
                    return True
        return False

    async def _pump_logs(self) -> None:
        loop = asyncio.get_event_loop()
        proc = self.process
        # read to EOF (not just while running) so the lines explaining a crash are kept
        while proc is not None and proc.stdout is not None:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line:
                break
            line = line.rstrip()
            if self._on_log_line(line):
                continue                   # a progress-bar update: used, not logged
            self._log_tail.append(line)
            print(f"[{self.role}] {line}")

    def _on_log_line(self, line: str) -> bool:
        """A server output line. True = it was a progress update (don't log it)."""
        return False

    def _exit_reason(self) -> Optional[str]:
        """A short reason from the server's last error line (card numbers masked)."""
        errs = [l for l in self._log_tail if re.search(r"\berror\b|failed|not found", l, re.I)]
        if not errs:
            return None
        from . import pan
        return pan.mask_pans(re.sub(r"\s+", " ", errs[-1]).strip()[-200:])[0]


class WhisperInstance(SmallModelInstance):
    """On-demand whisper.cpp server (speech to text) for an "stt" lane. Same
    lifecycle as a llama small model: loads on first use, idle reaper unloads it.
    Only 16 kHz mono WAV is ever sent to it, so it needs no ffmpeg."""

    def __init__(self, role: str, cfg: dict):
        super().__init__(role, cfg)
        if int(cfg.get("gpu", 0) if cfg.get("gpu") is not None else 0) < 0:
            self.gpu = -1          # CPU (the base class would remap it to a GPU)
        self.threads = int(cfg.get("threads") or 4)
        self.language = str(cfg.get("language") or "auto")

    @property
    def available(self) -> bool:
        return bool(self.model_path and self.model_path.exists() and find_whisper_server())

    def describe(self) -> str:
        return f"whisper {self.model_path.name if self.model_path else '?'}"

    # whisper-server has no /health on every build: any HTTP answer means it's listening
    _health_path = "/"

    def _healthy(self, r) -> bool:
        return r.status_code < 500

    async def _preflight(self, loop) -> None:
        return None          # ggml .bin files aren't gguf; whisper models are small (<2 GB)

    def _launch_cmd(self) -> tuple:
        import os
        exe = find_whisper_server()
        if exe is None:
            raise RuntimeError("whisper-server not found - install whisper.cpp (see Settings -> Models)")
        cmd = [str(exe), "-m", str(self.model_path), "--host", "127.0.0.1", "--port", str(self.port),
               "-t", str(self.threads), "--inference-path", "/v1/audio/transcriptions"]
        env = dict(os.environ)
        if self.gpu < 0:
            cmd += ["-ng"]                                   # CPU only
        else:
            env["GGML_VK_VISIBLE_DEVICES"] = str(self.gpu)   # pin to one Vulkan device
        return cmd, env

    async def transcribe(self, wav: bytes, language: Optional[str] = None) -> str:
        await self.ensure_loaded()
        self.last_used = time.time()
        lang = (language or self.language or "auto").strip().lower()
        r = await self.client.post("/v1/audio/transcriptions",
                                   files={"file": ("audio.wav", wav, "audio/wav")},
                                   data={"response_format": "json", "language": lang, "temperature": "0"},
                                   timeout=600)
        r.raise_for_status()
        self.last_used = time.time()
        try:
            return str(r.json().get("text") or "").strip()
        except ValueError:
            return r.text.strip()


class SdCppInstance(SmallModelInstance):
    """stable-diffusion.cpp's sd-server for a local image / video lane (Vulkan).
    Unlike the other helpers it is loaded and unloaded by hand (Settings -> Models
    & Jobs or the chat card): the weights are big, so a request never starts it
    and the idle reaper never stops it."""

    _health_path = "/sdcpp/v1/capabilities"
    _start_timeout = 600          # large diffusion + text-encoder weights

    def __init__(self, role: str, cfg: dict):
        super().__init__(role, cfg)
        base = Path(APP_CONFIG.get("models_dir") or "E:/AI/Models")

        def _p(key):
            v = self.cfg.get(key)
            if not v:
                return None
            q = Path(str(v))
            return q if q.is_absolute() else base / q
        self.model_path = _p("diffusion_model")
        self.llm_path = _p("llm")
        self.vae_path = _p("vae")
        self.llm_vision_path = _p("llm_vision")     # the text encoder's vision part (editing)
        self.mmproj_path = None
        if cfg.get("gpu") is not None and int(cfg.get("gpu")) < 0:
            self.gpu = -1
        self.offload_to_cpu = bool(self.cfg.get("offload_to_cpu", False))
        self.vae_tiling = bool(self.cfg.get("vae_tiling", True))
        self.timeout_s = int(self.cfg.get("timeout_s") or (900 if self.kind == "image_gen" else 3600))
        self.loading_since: Optional[float] = None
        self.busy = 0                 # generations in flight (a load/unload waits for 0)
        # the running job's step, from sd.cpp's console progress bar: (step, total, secs/step, when)
        self.step: Optional[tuple] = None

    def files(self) -> list:
        return [p for p in (self.model_path, self.llm_path, self.vae_path, self.llm_vision_path) if p]

    def edit_caps(self) -> dict:
        """What it can do with pictures: change one (init image, any model) and combine
        references (edit models: Qwen Image 2.1 with its vision weights, FLUX Kontext...)."""
        refs = bool(self.llm_vision_path) or bool(self.cfg.get("edit_refs"))
        return {"img2img": self.kind == "image_gen", "refs": refs and self.kind == "image_gen",
                "max_refs": max(1, min(10, int(self.cfg.get("max_refs") or 4)))}

    @property
    def available(self) -> bool:
        return bool(self.model_path and all(p.exists() for p in self.files()) and find_sd_server())

    def describe(self) -> str:
        return f"sd.cpp {self.model_path.name if self.model_path else '?'}"

    def est_bytes(self) -> int:
        """GPU memory the weights need (0 when they stay in RAM)."""
        if self.offload_to_cpu or self.gpu < 0:
            return 0
        total = 0
        for p in self.files():
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return int(total * 1.05) + int(1.5 * 1024 ** 3)     # + compute / VAE decode

    _STEP_RE = re.compile(r"\|\s*(\d+)/(\d+)\s*-\s*([\d.]+)\s*(s/it|it/s)")

    def _on_log_line(self, line: str) -> bool:
        # sd.cpp redraws "  |=====>     | 12/20 - 5.12s/it" with a carriage return;
        # the pipe is in text mode, so every redraw arrives as its own line
        m = self._STEP_RE.search(line)
        if not m:
            return False
        i, n, v = int(m.group(1)), int(m.group(2)), float(m.group(3))
        spi = v if m.group(4) == "s/it" else (1 / v if v > 0 else 0.0)
        self.step = (i, n, spi, time.time())
        if i >= n:                          # one line per finished bar in the console
            print(f"[{self.role}] {line.replace(chr(27) + '[K', '').strip()}")
        return True

    def _exit_reason(self) -> Optional[str]:
        tail = "\n".join(self._log_tail).lower()
        if "vae tensor" in tail and "not in model metadata" in tail:
            return "the VAE file is missing or isn't this model's VAE - pick its own VAE (.safetensors)"
        if re.search(r"(conditioner|llm|text encoder|cond_stage|text_encoders?)[^\n]*(not in model metadata|not found|missing)", tail):
            return "the text encoder is missing or doesn't match this model - pick its own text encoder"
        if "out of memory" in tail or "erroroutofdevicememory" in tail:
            return "not enough GPU memory - turn on 'Keep weights in RAM' or use another GPU"
        return super()._exit_reason()

    def state(self) -> str:
        if self.is_up():
            return "loaded"
        if self.loading_since:
            return "loading"
        return "failed" if self.load_error else "not_loaded"

    async def _preflight(self, loop) -> None:
        need = self.est_bytes()
        if not need:
            return
        from . import vram
        devs = await loop.run_in_executor(None, vram.query_devices)
        dev = next((d for d in devs or [] if d.get("index") == self.gpu), None)
        if dev and dev.get("free_b") is not None and dev["free_b"] < need:
            gb = 1024 ** 3
            raise vram.PreflightError(
                f"Needs about {need / gb:.1f} GB, GPU {self.gpu} has {dev['free_b'] / gb:.1f} GB free - "
                "turn on 'Keep weights in RAM' or unload other models on that GPU", {})

    def _launch_cmd(self) -> tuple:
        import os
        exe = find_sd_server()
        if exe is None:
            raise RuntimeError("sd-server not found - install stable-diffusion.cpp (see Settings -> Models)")
        cmd = [str(exe), "--diffusion-model", str(self.model_path),
               "--listen-ip", "127.0.0.1", "--listen-port", str(self.port), "--diffusion-fa"]
        if self.llm_path:
            cmd += ["--llm", str(self.llm_path)]
        if self.vae_path:
            cmd += ["--vae", str(self.vae_path)]
        if self.llm_vision_path:
            cmd += ["--llm_vision", str(self.llm_vision_path)]
        if self.offload_to_cpu:
            cmd += ["--offload-to-cpu"]
        if self.vae_tiling:
            cmd += ["--vae-tiling"]
        env = dict(os.environ)
        # pin to one Vulkan device ("" = no GPU: CPU only)
        env["GGML_VK_VISIBLE_DEVICES"] = str(self.gpu) if self.gpu >= 0 else ""
        return cmd, env

    async def load(self) -> None:
        """Start sd-server (the only way it starts)."""
        if self.is_up():
            return
        self.loading_since = time.time()
        self.load_error = None
        try:
            await SmallModelInstance.ensure_loaded(self)
        except Exception as e:
            self.load_error = self.load_error or str(e)[:300]
            raise
        finally:
            self.loading_since = None

    async def ensure_loaded(self) -> None:
        # a request never loads the image model: it stays a manual action
        if not self.is_up():
            raise RuntimeError("the image model isn't loaded")

    async def unload_if_idle(self) -> bool:
        return False                  # stays loaded until someone unloads it


def make_instance(name: str, cfg: dict):
    engine = lane_engine_of(name, cfg)
    if engine == "sdcpp":
        return SdCppInstance(name, cfg)
    if engine == "whisper":
        return WhisperInstance(name, cfg)
    return SmallModelInstance(name, cfg)


class SmallModelManager:
    """Owns the small-model instances + the idle reaper task."""

    def __init__(self):
        sm = APP_CONFIG["small_models"]
        self.instances = {name: make_instance(name, cfg)
                          for name, cfg in sm.items() if isinstance(cfg, dict)}
        self.reaper_task: Optional[asyncio.Task] = None

    def reconfigure(self, name: str, cfg: Optional[dict]) -> None:
        """Apply an edited/added/removed local lane live (Settings -> Models).
        A running server for that lane is stopped; the next request reloads it."""
        old = self.instances.pop(name, None)
        if old is not None and old.is_up():
            old._stop()
        if cfg is None:
            APP_CONFIG["small_models"].pop(name, None)
            return
        APP_CONFIG["small_models"][name] = dict(cfg)
        self.instances[name] = make_instance(name, APP_CONFIG["small_models"][name])

    def start_reaper(self) -> None:
        if self.reaper_task is None:
            self.reaper_task = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            for inst in self.instances.values():
                try:
                    await inst.unload_if_idle()
                except Exception:
                    pass

    def unload_all(self) -> None:
        for role, inst in self.instances.items():
            if inst.is_up():
                print(f"[server_manager] {role}: unloading to free GPU VRAM")
                try:
                    inst._stop()
                except Exception:
                    pass

    def status(self) -> dict:
        out = {}
        for role, inst in self.instances.items():
            out[role] = {
                "kind": inst.kind,
                "engine": lane_engine_of(role, getattr(inst, "cfg", None)),
                "model": inst.model_path.name if inst.model_path else None,
                "available": inst.available,
                "loaded": inst.is_up(),
                "port": inst.port,
                "error": inst.load_error,
                "state": inst.state() if hasattr(inst, "state") else None,
                "loading_since": getattr(inst, "loading_since", None),
            }
        return out


small_models = SmallModelManager()


# ---------------- vision (SmolVLM locally, or a cloud VLM) ----------------
def image_mime(data: bytes) -> Optional[str]:
    """Sniff the real image type from magic bytes (never trust the file extension)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


async def describe_image_bytes(data: bytes, question: str = "Describe this image in detail.") -> str:
    """Send one image through the vision lane (cloud when bound, else local)."""
    mime = image_mime(data)
    if mime is None:
        return "error: not a PNG, JPEG, WEBP or GIF image"
    b64 = base64.b64encode(data).decode()
    payload = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": 400,
        "temperature": 0.1,
    }
    # 1. Main model if vision-capable -- unless the user picked a model for the
    #    "Reading images" job in Settings -> Models (core/lanes.py)
    from . import cloud, lanes
    explicit = "vision" in cloud.role_map()
    try:
        from .state import state
        main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
        if not explicit and main_ready and bool((state.profile or {}).get("vision_capable")):
            r = await state.client.post("/v1/chat/completions", json=payload, timeout=None)
            state.last_activity = time.time()
            data = r.json()
            return ((data.get("choices") or [{}])[0].get("message", {}).get("content")
                    or "(main vision model returned no text)")
    except Exception as e:
        print(f"[vision] main model vision failed: {e} - falling back to vision lane/model")

    # 2. the job's route: cloud vision binding, then the local vision model
    try:
        data, _t = await lanes.post_chat("vision", payload)
    except RuntimeError as e:
        return f"error: no image model available ({e})"
    return lanes.message_text(data) or "(vision model returned no text)"


# ---------------- Dual CPU Routers: Needle-2 & Laya (0 VRAM) ----------------
_needle_agent = None
_needle_tools_names = None
_needle_failed = False

_laya_router = None
_laya_failed = False


def reset_router_failures() -> None:
    """Re-arm both CPU routers after an admin edits the router config
    (a failure otherwise disables a router until restart)."""
    global _needle_failed, _laya_failed
    _needle_failed = False
    _laya_failed = False


def router_engine_name() -> str:
    """Active router engine configured in app.json: 'cactus_needle' or 'laya'."""
    eng = str(APP_CONFIG.get("router", {}).get("engine", "cactus_needle")).strip().lower()
    if "laya" in eng:
        return "laya"
    return "cactus_needle"


def needle_available() -> bool:
    global _needle_failed
    if _needle_failed or not APP_CONFIG.get("router", {}).get("enabled", True):
        return False
    if _needle_agent is not None:
        return True
    try:
        import needle  # noqa: F401
    except ImportError:
        _needle_failed = True
        return False
    return True


def needle_route(query: str, tools: list) -> Optional[dict]:
    if not needle_available():
        return None
    global _needle_agent, _needle_tools_names
    try:
        plain = [t["function"] for t in tools if isinstance(t, dict) and "function" in t]
        tool_names = tuple(sorted(t.get("name", "") for t in plain))
        if _needle_agent is None or _needle_tools_names != tool_names:
            import needle as _nd
            _needle_agent = _nd.Needle(tools=plain)
            _needle_tools_names = tool_names
        resp = _needle_agent.complete(query)
        if resp.get("type") != "call" or not resp.get("function_calls"):
            return None
        conf = float(resp.get("confidence") or 0.0)
        threshold = float(APP_CONFIG.get("router", {}).get("confidence_threshold", 0.7))
        if conf < threshold:
            return None
        fc = resp["function_calls"][0]
        return {"name": fc["name"], "args": fc.get("arguments") or {},
                "confidence": conf, "reasoning": resp.get("reasoning") or ""}
    except Exception as e:
        print(f"[server_manager] needle route failed: {e}", file=sys.stderr)
        _needle_failed = True
        return None


def laya_available() -> bool:
    global _laya_failed
    if _laya_failed or not APP_CONFIG.get("router", {}).get("enabled", True):
        return False
    if _laya_router is not None:
        return True
    try:
        import laya  # noqa: F401
    except ImportError:
        _laya_failed = True
        return False
    return True


def _extract_simple_args(tool_name: str, query: str) -> dict:
    """Extract common parameters from query deterministically for non-generative routers like Laya."""
    q_clean = query.strip()
    if tool_name == "list_files":
        m_pat = re.search(r'(\*\.[\w]+|\*\*[\w/.*]+|\*\w+)', q_clean)
        if m_pat:
            return {"pattern": m_pat.group(1)}
        return {}
    elif tool_name == "read_file":
        m_path = re.search(r'[\'"`]([^\'"`]+)[\'"`]', q_clean)
        if m_path:
            return {"path": m_path.group(1)}
        m_file = re.search(r'([A-Za-z0-9_\-\\/]+\.[A-Za-z0-9]{1,6})', q_clean)
        if m_file:
            return {"path": m_file.group(1)}
        return {}
    elif tool_name == "grep":
        m_q = re.search(r'[\'"`]([^\'"`]+)[\'"`]', q_clean)
        if m_q:
            return {"pattern": m_q.group(1)}
        m_word = re.search(r'(?:grep(?:\s+for)?|search\s+for|find)\s+([^\s]+)', q_clean, re.IGNORECASE)
        if m_word:
            return {"pattern": m_word.group(1)}
        return {"pattern": q_clean}
    return {}


def laya_route(query: str, tools: list) -> Optional[dict]:
    """Route query using Laya System 1 decision engine strictly on CPU (0 VRAM)."""
    if not laya_available():
        return None
    global _laya_router, _laya_failed
    try:
        laya_cfg = APP_CONFIG.get("router", {}).get("laya", {})
        # Enforce CPU execution to ensure zero VRAM impact on Arc A770
        target_device = "cpu"
        
        if _laya_router is None:
            import laya
            ckpt = laya_cfg.get("checkpoint", "convaiinnovations/laya")
            subfolder = laya_cfg.get("subfolder")
            preload = bool(laya_cfg.get("preload", False))
            
            try:
                if hasattr(laya, "Router"):
                    _laya_router = laya.Router(preload=preload, device=target_device)
                else:
                    _laya_router = laya.load(ckpt, subfolder=subfolder, device=target_device)
            except Exception:
                _laya_router = laya.load(ckpt, subfolder=subfolder, device=target_device)

        plain = [t["function"] for t in tools if isinstance(t, dict) and "function" in t]
        if not plain:
            return None

        # Build criteria for Laya question
        criteria = {}
        for t in plain:
            name = t.get("name")
            desc = t.get("description", name)
            if name:
                criteria[name] = desc[:150]
        criteria["none"] = "None of the above tools apply or the user wants general conversational assistance"

        questions = {
            "selected_tool": {
                "type": "choice",
                "instructions": "Which tool should be invoked to satisfy the user's immediate request?",
                "criteria": criteria
            }
        }
        state_input = {"query": query, "body": query}

        if hasattr(_laya_router, "predict"):
            pred = _laya_router.predict(state_input, questions)
        else:
            pred = _laya_router(state_input, questions)

        ans = pred.get("answers", {}).get("selected_tool", {})
        chosen = ans.get("choice") or ans.get("answer")
        conf = float(ans.get("confidence") or ans.get("probability") or 0.0)

        threshold = float(APP_CONFIG.get("router", {}).get("confidence_threshold", 0.75))
        if not chosen or chosen == "none" or chosen not in criteria or conf < threshold:
            return None

        args = _extract_simple_args(chosen, query)
        reasoning = f"Laya CPU classified request to {chosen} (confidence: {conf:.2f})"
        return {
            "name": chosen,
            "args": args,
            "confidence": conf,
            "reasoning": reasoning
        }
    except Exception as e:
        print(f"[server_manager] laya route failed: {e}", file=sys.stderr)
        _laya_failed = True
        return None


def router_available() -> bool:
    """Check if the currently active router engine is available."""
    eng = router_engine_name()
    if eng == "laya":
        return laya_available() or needle_available()
    return needle_available() or laya_available()


def router_route(query: str, tools: list) -> Optional[dict]:
    """Unified route dispatcher according to app.json router.engine with fallback."""
    eng = router_engine_name()
    if eng == "laya":
        res = laya_route(query, tools)
        if res is not None:
            return res
        if _laya_failed and needle_available():
            return needle_route(query, tools)
        return None
    res = needle_route(query, tools)
    if res is not None:
        return res
    if needle_available() and laya_available():
        return laya_route(query, tools)
    return None

