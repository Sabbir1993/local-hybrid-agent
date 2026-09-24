import asyncio
import base64
import json
import subprocess
import sys
import time
import re
from pathlib import Path
from typing import Optional, Union

import httpx

from .backend import device_prefix
from .config import BASE_DIR, LLAMA_SERVER_PORT, CONFIG_FILE, ROLES_FILE, CONFIG_DEFAULTS
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
        "llama_bin_dir": CONFIG_DEFAULTS["llama_bin_dir"],
        "backend": CONFIG_DEFAULTS["backend"],
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
            for k in ("models_dir", "workspace_dir", "common_dir", "llama_bin_dir", "backend"):
                if d.get(k):
                    base[k] = d[k]
            for k, sub in base["small_models"].items():
                if isinstance(d.get("small_models", {}).get(k), dict):
                    sub.update(d["small_models"][k])
            # "roles" is included here as a legacy override: an un-migrated
            # app.json that still has a "roles" block wins over config/roles.json.
            for k in ("router", "agent", "capabilities", "provider", "cloud", "roles",
                      "input_guard", "output_guard", "preflight"):
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
        # parallel slots share one unified KV pool of -c tokens (-kvu), so
        # several users' agent steps / embeddings run concurrently
        self.n_slots = max(1, int(cfg.get("np", 1)))
        self.kv_cache_type = str(cfg.get("kv_cache_type") or "")
        self.process: Optional[subprocess.Popen] = None
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", timeout=None)
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
            bin_dir = APP_CONFIG.get("llama_bin_dir") or CONFIG_DEFAULTS["llama_bin_dir"]
            backend = APP_CONFIG.get("backend") or CONFIG_DEFAULTS["backend"]
            prefix = device_prefix(backend)
            server_bin = find_llama_server(bin_dir)
            # Preflight: refuse to spawn if this small model wouldn't fit on
            # its target Vulkan device (prevents the WDDM OOM desktop hang).
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: vram.check_small_model_or_raise(
                    self.role, self.model_path, self.ctx, self.gpu,
                    mmproj_path=self.mmproj_path),
            )
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
            if self.role == "embedder":
                cmd += ["--embedding"]

            print(f"[{self.role}] auto-loading on {prefix}{self.gpu} (port {self.port})...")
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


# ---------------- vision (SmolVLM locally, or a cloud VLM) ----------------
async def describe_image_file(p: Path, question: str = "Describe this image in detail.") -> str:
    """Send one image through the vision lane (cloud when bound, else local)."""
    b64 = base64.b64encode(p.read_bytes()).decode()
    mime = "image/png" if p.suffix.lower() == ".png" else (
        "image/webp" if p.suffix.lower() == ".webp" else "image/jpeg")
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
    # 1. Main model if vision-capable
    try:
        from .state import state
        main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
        if main_ready and bool((state.profile or {}).get("vision_capable")):
            r = await state.client.post("/v1/chat/completions", json=payload, timeout=None)
            state.last_activity = time.time()
            data = r.json()
            return ((data.get("choices") or [{}])[0].get("message", {}).get("content")
                    or "(main vision model returned no text)")
    except Exception as e:
        print(f"[vision] main model vision failed: {e} - falling back to vision lane/model")

    # 2. Cloud vision lane if configured
    from . import cloud
    cm = cloud.cloud_lane("vision")
    if cm:
        try:
            r = await cloud.CloudClient(cm).post("/v1/chat/completions", json=payload, timeout=None)
            data = r.json()
            return ((data.get("choices") or [{}])[0].get("message", {}).get("content")
                    or "(cloud vision model returned no text)")
        except Exception as e:
            print(f"[vision] cloud lane failed ({cm.key}): {e}")

    inst = small_models.instances["vision"]
    if not inst.available:
        return "error: vision model not configured in config.json (small_models.vision)"
    await inst.ensure_loaded()
    r = await inst.client.post("/v1/chat/completions", json=payload, timeout=None)
    inst.last_used = time.time()
    data = r.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or "(vision model returned no text)"


# ---------------- Dual CPU Routers: Needle-2 & Laya (0 VRAM) ----------------
_needle_agent = None
_needle_tools_names = None
_needle_failed = False

_laya_router = None
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

