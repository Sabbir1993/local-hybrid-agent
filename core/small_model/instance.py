import asyncio
import collections
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

from ..backend import device_prefix
from ..config import (
    ACTIVE_RUNTIME,
    LOCAL_HTTP_CONNECT_TIMEOUT_S,
    LOCAL_HTTP_POOL_TIMEOUT_S,
    LOCAL_HTTP_READ_TIMEOUT_S,
    LOCAL_HTTP_WRITE_TIMEOUT_S,
)
from ..process import find_llama_server, kill_process_tree
from .. import vram
from .config import APP_CONFIG, lane_kind_of


SMALL_CACHE_RAM_MB = 1024
SMALL_CTX_CHECKPOINTS = 8


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
        fallback = ACTIVE_RUNTIME.get("small_model_gpu")
        if fallback is not None and self.gpu not in ACTIVE_RUNTIME["gpu_devices"]:
            self.gpu = int(fallback)
        self.ctx = int(cfg.get("ctx", 4096))
        self.n_slots = max(1, int(cfg.get("np", 1)))
        # optional per-conversation ceiling inside the shared pool (0 = unset, the
        # pool stays fully shareable). Passed as --kv-unified-per-slot; without it a
        # single run can grow to the whole -c, which is how one long agent step can
        # starve the other slots.
        self.kv_unified_per_slot = max(0, int(cfg.get("kv_unified_per_slot") or 0))
        self.kv_cache_type = str(cfg.get("kv_cache_type") or "")
        # llama-server keeps saved prompt states in host RAM: -cram defaults to 8192 MiB and -ctxcp to 32 per slot,
        # which is how an executor on a 3 GB model reached 7 GB resident. Bounded here; 0 disables, -1 = no limit.
        default_cram = 0 if self.kind == "embed" else SMALL_CACHE_RAM_MB
        self.cache_ram = int(cfg.get("cache_ram", default_cram))
        self.ctx_checkpoints = max(0, int(cfg.get("ctx_checkpoints", SMALL_CTX_CHECKPOINTS)))
        self.process: Optional[subprocess.Popen] = None
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.port}",
            # was timeout=None, same reason as core/state.py: nothing upstream could end a
            # hung lane generation, and idle_unload_s=0 means it holds its VRAM for good
            timeout=httpx.Timeout(LOCAL_HTTP_CONNECT_TIMEOUT_S,
                                  read=LOCAL_HTTP_READ_TIMEOUT_S,
                                  write=LOCAL_HTTP_WRITE_TIMEOUT_S,
                                  pool=LOCAL_HTTP_POOL_TIMEOUT_S))
        self.last_used = 0.0
        self.lock = asyncio.Lock()
        self.load_error: Optional[str] = None
        self._log_tail = collections.deque(maxlen=60)
        self._log_tasks: set = set()

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
            # keep the handle: the pump read the process pipe forever and nothing held a
            # reference, so every load cycle added an untracked task
            task = asyncio.create_task(self._pump_logs())
            self._log_tasks.add(task)
            task.add_done_callback(self._log_tasks.discard)

            deadline = time.time() + self._start_timeout
            while time.time() < deadline:
                if self.process.poll() is not None:
                    await asyncio.sleep(0.3)
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
        # Pass the SAME values _launch_cmd() will use. The preflight used to invent its own
        # (kv_cache_type "f16" while the executor runs q8_0, ubatch 512 regardless of config,
        # and mmproj accepted but never counted), so the estimate and the launch disagreed.
        await loop.run_in_executor(
            None,
            lambda: vram.check_small_model_or_raise(
                self.role, self.model_path, self.ctx, self.gpu,
                mmproj_path=self.mmproj_path,
                kv_cache_type=self.kv_cache_type or "f16",
                n_slots=self.n_slots,
                ubatch_size=int(self.cfg.get("ubatch_size", 512) or 512)),
        )

    def _launch_cmd(self) -> tuple:
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
            # same rule as the main profile, one implementation: only meaningful with
            # several slots, and only when the cap is really below the pool
            from ..process import per_slot_cap
            cap = per_slot_cap({"kv_unified": True, "kv_unified_per_slot": self.kv_unified_per_slot,
                                "context_size": self.ctx, "n_slots": self.n_slots})
            if cap > 0:
                cmd += ["--kv-unified-per-slot", str(cap)]
        if self.kv_cache_type:
            cmd += ["-ctk", self.kv_cache_type, "-ctv", self.kv_cache_type]
        cmd += ["-cram", str(self.cache_ram), "-ctxcp", str(self.ctx_checkpoints)]   # 0 = none, not llama's default 32
        if self.mmproj_path and self.mmproj_path.exists():
            cmd += ["--mmproj", str(self.mmproj_path)]
        if self.kind == "embed":
            cmd += ["--embedding"]
        return cmd, None

    def _stop(self) -> None:
        self._cancel_log_pump()
        if self.process and self.process.poll() is None:
            kill_process_tree(self.process, timeout=10)
        self.process = None

    def _cancel_log_pump(self) -> None:
        for task in list(self._log_tasks):
            if not task.done():
                task.cancel()
        self._log_tasks.clear()

    async def aclose(self) -> None:
        """Release the HTTP connection pool. httpx clients keep sockets open until closed, so
        dropping an instance without this orphaned a pool (and its fds) per reconfigure."""
        self._cancel_log_pump()
        try:
            await self.client.aclose()
        except Exception:
            pass

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
        while proc is not None and proc.stdout is not None:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line:
                break
            line = line.rstrip()
            if self._on_log_line(line):
                continue
            self._log_tail.append(line)
            print(f"[{self.role}] {line}")

    def _on_log_line(self, line: str) -> bool:
        return False

    def _exit_reason(self) -> Optional[str]:
        errs = [l for l in self._log_tail if re.search(r"\berror\b|failed|not found", l, re.I)]
        if not errs:
            return None
        from .. import pan
        return pan.mask_pans(re.sub(r"\s+", " ", errs[-1]).strip()[-200:])[0]
