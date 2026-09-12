import asyncio
import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Union

import httpx

from .config import BASE_DIR, LLAMA_SERVER_PORT
from .process import find_llama_server


def _load_workspace_root() -> Path:
    cfg = BASE_DIR / "config.json"
    try:
        if cfg.exists():
            d = json.loads(cfg.read_text())
            v = d.get("workspace_dir")
            if v:
                p = Path(v)
                p.mkdir(parents=True, exist_ok=True)
                return p.resolve()
    except Exception:
        pass
    p = Path("E:\\AI\\workspace")
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


WORKSPACE_ROOT = _load_workspace_root()


def _load_app_config() -> dict:
    cfg = BASE_DIR / "config.json"
    base = {
        "models_dir": None,
        "workspace_dir": None,
        "small_models": {
            "executor": {"model": None, "port": 8091, "gpu": 1, "ctx": 8192},
            "vision": {"model": None, "mmproj": None, "port": 8092, "gpu": 1, "ctx": 4096},
            "embedder": {"model": None, "port": 8093, "gpu": 1},
        },
        "router": {"enabled": True, "confidence_threshold": 0.7},
        "agent": {"exec_timeout_s": 120, "max_steps": 30, "idle_unload_s": 120},
    }
    try:
        if cfg.exists():
            d = json.loads(cfg.read_text())
            for k in ("models_dir", "workspace_dir"):
                if d.get(k):
                    base[k] = d[k]
            for k, sub in base["small_models"].items():
                if isinstance(d.get("small_models", {}).get(k), dict):
                    sub.update(d["small_models"][k])
            for k in ("router", "agent"):
                if isinstance(d.get(k), dict):
                    base[k].update(d[k])
    except Exception as e:
        print(f"[server_manager] config.json unreadable: {e}", file=sys.stderr)
    return base


APP_CONFIG = _load_app_config()


class SmallModelInstance:
    """One on-demand llama-server child for a small model (executor/vision/embedder)."""

    def __init__(self, role: str, cfg: dict, client_hint=None):
        self.role = role
        self.cfg = cfg or {}
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
        self.ctx = int(cfg.get("ctx", 4096))
        self.process: Optional[subprocess.Popen] = None
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", timeout=120.0)
        self.last_used = 0.0
        self.lock = asyncio.Lock()
        self.load_error: Optional[str] = None

    @property
    def available(self) -> bool:
        if not self.model_path or not self.model_path.exists():
            return False
        if self.role == "vision" and (not self.mmproj_path or not self.mmproj_path.exists()):
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
            bin_dir = "E:\\AI\\llama-vulkan"
            server_bin = find_llama_server(bin_dir)
            cmd = [
                str(server_bin),
                "-m", str(self.model_path),
                "-c", str(self.ctx),
                "-ngl", "999",
                "-dev", f"Vulkan{self.gpu}",
                "--port", str(self.port),
                "--host", "127.0.0.1",
                "-np", "1",
                "-fa", "on",
                "--jinja",
            ]
            if self.mmproj_path and self.mmproj_path.exists():
                cmd += ["--mmproj", str(self.mmproj_path)]
            if self.role == "embedder":
                cmd += ["--embedding"]

            print(f"[{self.role}] auto-loading on Vulkan{self.gpu} (port {self.port})...")
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.load_error = None
            asyncio.create_task(self._pump_logs())

            deadline = time.time() + 90
            while time.time() < deadline:
                if self.process.poll() is not None:
                    self.load_error = f"exited code {self.process.returncode}"
                    self.process = None
                    raise RuntimeError(f"[{self.role}] failed to start ({self.load_error})")
                try:
                    r = await self.client.get("/health", timeout=2.0)
                    if r.status_code == 200:
                        print(f"[{self.role}] ready on port {self.port}")
                        self.last_used = time.time()
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            self._stop()
            self.load_error = "timed out after 90s"
            raise RuntimeError(f"[{self.role}] health check timed out")

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
        idle_limit = int(APP_CONFIG["agent"].get("idle_unload_s", 120))
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
        while self.process and self.process.poll() is None:
            line = await loop.run_in_executor(None, self.process.stdout.readline)
            if not line:
                break
            print(f"[{self.role}] {line.rstrip()}")


class SmallModelManager:
    """Owns the small-model instances + the idle reaper task."""

    def __init__(self):
        sm = APP_CONFIG["small_models"]
        self.instances = {
            "executor": SmallModelInstance("executor", sm["executor"]),
            "vision": SmallModelInstance("vision", sm["vision"]),
            "embedder": SmallModelInstance("embedder", sm["embedder"]),
        }
        self.reaper_task: Optional[asyncio.Task] = None

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
                "model": inst.model_path.name if inst.model_path else None,
                "available": inst.available,
                "loaded": inst.is_up(),
                "port": inst.port,
                "error": inst.load_error,
            }
        return out


small_models = SmallModelManager()


# ---------------- vision (SmolVLM, on-demand) ----------------
async def describe_image_file(p: Path, question: str = "Describe this image in detail.") -> str:
    """Send one image through SmolVLM (auto-loads instance on demand)."""
    inst = small_models.instances["vision"]
    if not inst.available:
        return "error: vision model not configured in config.json (small_models.vision)"
    await inst.ensure_loaded()
    b64 = base64.b64encode(p.read_bytes()).decode()
    mime = "image/png" if p.suffix.lower() == ".png" else (
        "image/webp" if p.suffix.lower() == ".webp" else "image/jpeg")
    r = await inst.client.post("/v1/chat/completions", json={
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": 400,
        "temperature": 0.1,
    }, timeout=120.0)
    inst.last_used = time.time()
    data = r.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or "(vision model returned no text)"


# ---------------- Needle CPU router (fast lane, 0 VRAM) ----------------
_needle_agent = None
_needle_failed = False


def needle_available() -> bool:
    global _needle_failed
    if _needle_failed or not APP_CONFIG["router"].get("enabled", True):
        return False
    if _needle_agent is not None:
        return True
    try:
        import needle
    except ImportError:
        _needle_failed = True
        return False
    return True


def needle_route(query: str, tools: list) -> Optional[dict]:
    if not needle_available():
        return None
    global _needle_agent
    try:
        if _needle_agent is None:
            plain = [t["function"] for t in tools]
            import needle as _nd
            _needle_agent = _nd.Needle(tools=plain)
        resp = _needle_agent.complete(query)
        if resp.get("type") != "call" or not resp.get("function_calls"):
            return None
        conf = float(resp.get("confidence") or 0.0)
        threshold = float(APP_CONFIG["router"].get("confidence_threshold", 0.7))
        if conf < threshold:
            return None
        fc = resp["function_calls"][0]
        return {"name": fc["name"], "args": fc.get("arguments") or {},
                "confidence": conf, "reasoning": resp.get("reasoning") or ""}
    except Exception as e:
        print(f"[server_manager] needle route failed: {e}", file=sys.stderr)
        _needle_failed = True
        return None
