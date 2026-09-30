import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from .config import APP_CONFIG
from .discovery import find_sd_server, find_whisper_server
from .instance import SmallModelInstance


def _get_sd_server():
    mod = sys.modules.get("core.small_model")
    fn = getattr(mod, "find_sd_server", find_sd_server) if mod else find_sd_server
    return fn()


def _get_whisper_server():
    mod = sys.modules.get("core.small_model")
    fn = getattr(mod, "find_whisper_server", find_whisper_server) if mod else find_whisper_server
    return fn()


LANE_KINDS = ("chat", "vision", "embed", "image_gen", "video_gen", "stt")


def lane_kind_of_spec(name: str, cfg: Optional[dict] = None) -> str:
    """Return 'chat' / 'embed' / 'vision' / 'image_gen' / 'video_gen' / 'stt'."""
    c = cfg if isinstance(cfg, dict) else (APP_CONFIG.get("small_models", {}).get(name) or {})
    k = str(c.get("kind") or "").strip().lower()
    if not k:
        k = "embed" if name == "embedder" else "vision" if name == "vision" else "chat"
    # an unknown kind ("IMAGE_GEN" is fine, "foo" is not) must not leak through as its own
    # engine/kind: treat it as a chat lane
    return k if k in LANE_KINDS else "chat"


def lane_engine_of_spec(name: str, cfg: Optional[dict] = None) -> str:
    """What serves a local lane: llama, whisper, or sdcpp."""
    kind = lane_kind_of_spec(name, cfg)
    if kind in ("image_gen", "video_gen"):
        return "sdcpp"
    if kind == "stt":
        return "whisper"
    return "llama"


class WhisperInstance(SmallModelInstance):
    """On-demand whisper.cpp server (speech to text) for an "stt" lane."""

    def __init__(self, role: str, cfg: dict):
        super().__init__(role, cfg)
        if int(cfg.get("gpu", 0) if cfg.get("gpu") is not None else 0) < 0:
            self.gpu = -1
        self.threads = int(cfg.get("threads") or 4)
        self.language = str(cfg.get("language") or "auto")

    @property
    def available(self) -> bool:
        return bool(self.model_path and self.model_path.exists() and _get_whisper_server())

    def describe(self) -> str:
        return f"whisper {self.model_path.name if self.model_path else '?'}"

    _health_path = "/"

    def _healthy(self, r) -> bool:
        return r.status_code < 500

    async def _preflight(self, loop) -> None:
        return None

    def _launch_cmd(self) -> tuple:
        exe = _get_whisper_server()
        if exe is None:
            raise RuntimeError("whisper-server not found - install whisper.cpp (see Settings -> Models)")
        cmd = [str(exe), "-m", str(self.model_path), "--host", "127.0.0.1", "--port", str(self.port),
               "-t", str(self.threads), "--inference-path", "/v1/audio/transcriptions"]
        env = dict(os.environ)
        if self.gpu < 0:
            cmd += ["-ng"]
        else:
            env["GGML_VK_VISIBLE_DEVICES"] = str(self.gpu)
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
    """stable-diffusion.cpp's sd-server for a local image / video lane (Vulkan)."""

    _health_path = "/sdcpp/v1/capabilities"
    _start_timeout = 600

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
        self.llm_vision_path = _p("llm_vision")
        self.mmproj_path = None
        if cfg.get("gpu") is not None and int(cfg.get("gpu")) < 0:
            self.gpu = -1
        self.offload_to_cpu = bool(self.cfg.get("offload_to_cpu", False))
        self.vae_tiling = bool(self.cfg.get("vae_tiling", True))
        self.timeout_s = int(self.cfg.get("timeout_s") or (900 if self.kind == "image_gen" else 3600))
        self.loading_since: Optional[float] = None
        self.busy = 0
        self.step: Optional[tuple] = None

    def files(self) -> list:
        return [p for p in (self.model_path, self.llm_path, self.vae_path, self.llm_vision_path) if p]

    def edit_caps(self) -> dict:
        refs = bool(self.llm_vision_path) or bool(self.cfg.get("edit_refs"))
        return {"img2img": self.kind == "image_gen", "refs": refs and self.kind == "image_gen",
                "max_refs": max(1, min(10, int(self.cfg.get("max_refs") or 4)))}

    @property
    def available(self) -> bool:
        return bool(self.model_path and all(p.exists() for p in self.files()) and _get_sd_server())

    def describe(self) -> str:
        return f"sd.cpp {self.model_path.name if self.model_path else '?'}"

    def est_bytes(self) -> int:
        if self.offload_to_cpu or self.gpu < 0:
            return 0
        total = 0
        for p in self.files():
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return int(total * 1.05) + int(1.5 * 1024 ** 3)

    _STEP_RE = re.compile(r"\|\s*(\d+)/(\d+)\s*-\s*([\d.]+)\s*(s/it|it/s)")

    def _on_log_line(self, line: str) -> bool:
        m = self._STEP_RE.search(line)
        if not m:
            return False
        i, n, v = int(m.group(1)), int(m.group(2)), float(m.group(3))
        spi = v if m.group(4) == "s/it" else (1 / v if v > 0 else 0.0)
        self.step = (i, n, spi, time.time())
        if i >= n:
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
        from .. import vram
        devs = await loop.run_in_executor(None, vram.query_devices)
        dev = next((d for d in devs or [] if d.get("index") == self.gpu), None)
        if dev and dev.get("free_b") is not None and dev["free_b"] < need:
            gb = 1024 ** 3
            raise vram.PreflightError(
                f"Needs about {need / gb:.1f} GB, GPU {self.gpu} has {dev['free_b'] / gb:.1f} GB free - "
                "turn on 'Keep weights in RAM' or unload other models on that GPU", {})

    def _launch_cmd(self) -> tuple:
        exe = _get_sd_server()
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
        env["GGML_VK_VISIBLE_DEVICES"] = str(self.gpu) if self.gpu >= 0 else ""
        return cmd, env

    async def load(self) -> None:
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
        if not self.is_up():
            raise RuntimeError("the image model isn't loaded")

    async def unload_if_idle(self) -> bool:
        return False


def make_instance(name: str, cfg: dict):
    engine = lane_engine_of_spec(name, cfg)
    if engine == "sdcpp":
        return SdCppInstance(name, cfg)
    if engine == "whisper":
        return WhisperInstance(name, cfg)
    return SmallModelInstance(name, cfg)
