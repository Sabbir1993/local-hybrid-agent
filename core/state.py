import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Union

import httpx

from .config import HEALTH_TIMEOUT_S, KEEPALIVE_INTERVAL_S, LLAMA_SERVER_PORT, MAX_RESTART_BACKOFF_S, WATCHDOG_INTERVAL_S
from .process import build_launch_command
from .profiles import build_dynamic_profile
from . import vram


class ProxyState:
    def __init__(self):
        self.profile_path: Optional[Path] = None
        self.profile: Optional[dict] = None
        self.process: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None
        self.restart_count = 0
        self.lock = asyncio.Lock()
        self.watchdog_task: Optional[asyncio.Task] = None
        self.keepalive_task: Optional[asyncio.Task] = None
        self.keepalive_enabled = False
        self.keepalive_interval_s = KEEPALIVE_INTERVAL_S
        self.last_activity = time.time()
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{LLAMA_SERVER_PORT}", timeout=None)

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def ensure_running(self, target: Union[Path, dict, str]):
        """Auto-start for request paths: a no-op when llama-server is already
        up. Re-checked under the lock, so N concurrent requests that all find
        the model down trigger exactly one launch (the rest wait for it)
        instead of each killing and relaunching the previous one."""
        if self.is_running():
            return
        async with self.lock:
            if self.is_running():
                return
            await self._load_locked(target)

    async def load_profile(self, target: Union[Path, dict, str]):
        """Explicit (re)load: always stops the current process first."""
        async with self.lock:
            await self._load_locked(target)

    async def _load_locked(self, target: Union[Path, dict, str]):
        self._stop_process_locked()
        if isinstance(target, (Path, str)):
            p = Path(target)
            if p.suffix == ".json" and p.exists():
                self.profile_path = p
                self.profile = json.loads(p.read_text())
            elif p.suffix == ".gguf" or p.exists():
                self.profile_path = None
                self.profile = build_dynamic_profile(p)
            else:
                raise FileNotFoundError(f"Target file not found: {target}")
        elif isinstance(target, dict):
            self.profile_path = None
            self.profile = target
        else:
            raise ValueError("Invalid profile target")

        cmd = build_launch_command(self.profile)
        # Preflight: refuse to spawn llama-server if the VRAM math says it
        # won't fit (a WDDM OOM spill hangs the whole desktop on Arc).
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, vram.check_or_raise, self.profile)
        print(f"[server_manager] launching: {' '.join(cmd)}")
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.started_at = time.time()
        asyncio.create_task(self._pump_logs(self.process))
        await self._wait_healthy()

    async def stop(self):
        async with self.lock:
            self._stop_process_locked()
            self.started_at = None

    async def start(self):
        if self.profile is None and self.profile_path is None:
            raise RuntimeError("No model or profile loaded - select a model first")
        target = self.profile_path or self.profile
        await self.load_profile(target)

    async def _pump_logs(self, proc: subprocess.Popen):
        loop = asyncio.get_event_loop()
        while proc.poll() is None:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line:
                break
            print(f"[llama-server] {line.rstrip()}")

    async def _wait_healthy(self):
        deadline = time.time() + HEALTH_TIMEOUT_S
        # VRAM wall watch: if free VRAM on a target card collapses while the
        # model is still loading, kill the process *before* WDDM starts
        # spilling into shared RAM and hanging the desktop.
        target_idx: set = set()
        try:
            if self.profile:
                target_idx = set(vram.effective_params(self.profile)["gpu_devices"])
        except Exception:
            pass
        monitor_on = bool(target_idx) and vram.preflight_mode() != "off"
        wall_hits = 0
        last_vram_poll = 0.0
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"llama-server exited immediately (code {self.process.returncode}) - "
                    f"check the log lines above, this is almost always a bad model path "
                    f"or a VRAM allocation failure."
                )
            try:
                r = await self.client.get("/health", timeout=2.0)
                if r.status_code == 200:
                    print(f"[server_manager] llama-server healthy after "
                          f"{time.time() - self.started_at:.1f}s")
                    return
            except httpx.HTTPError:
                pass
            if monitor_on and time.time() - last_vram_poll >= 2.0:
                last_vram_poll = time.time()
                loop = asyncio.get_event_loop()
                devs = await loop.run_in_executor(
                    None, lambda: vram.query_devices(force=True))
                hit = vram.wall_check(target_idx, devs)
                wall_hits = wall_hits + 1 if hit else 0
                if wall_hits >= 2:
                    self._kill_loading_process(hit)
                    raise RuntimeError(
                        f"VRAM exhausted on Vulkan{hit} while the model was loading - "
                        f"aborted llama-server to prevent a system hang. Reduce GPU "
                        f"layers (-ngl) / tensor-split / context size, or unload "
                        f"other models first, then load again.")
            await asyncio.sleep(1.0)
        raise RuntimeError(f"llama-server didn't become healthy within {HEALTH_TIMEOUT_S}s")

    def _kill_loading_process(self, vram_idx=None) -> None:
        if self.process and self.process.poll() is None:
            print(f"[server_manager] VRAM wall hit on Vulkan{vram_idx} "
                  f"- killing the loading llama-server")
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                               capture_output=True, timeout=15)
            except Exception:
                pass
        self.process = None

    def _stop_process_locked(self):
        if self.process and self.process.poll() is None:
            print("[server_manager] stopping current llama-server...")
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                               capture_output=True, timeout=15)
            except Exception:
                self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    async def watchdog(self):
        backoff = 1
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            if self.process is None:
                continue
            if self.process.poll() is not None:
                self.restart_count += 1
                print(f"[server_manager] llama-server died (code {self.process.returncode}), "
                      f"restarting in {backoff}s (restart #{self.restart_count})")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_RESTART_BACKOFF_S)
                try:
                    target = self.profile_path or self.profile
                    if target:
                        await self.load_profile(target)
                        backoff = 1
                except Exception as e:
                    print(f"[server_manager] restart failed: {e}", file=sys.stderr)


state = ProxyState()


async def keepalive_loop():
    while True:
        await asyncio.sleep(5)
        if not state.keepalive_enabled:
            continue
        if state.process is None or state.process.poll() is not None:
            continue
        if time.time() - state.last_activity < state.keepalive_interval_s:
            continue
        try:
            # Pin the ping to the last slot so, with -np > 1, it doesn't land on
            # (and overwrite) whichever slot holds the most recent user's cached
            # conversation prefix. With -np 1 there is no spare slot to use.
            n_slots = int((state.profile or {}).get("n_slots") or 1)
            await state.client.post("/completion", json={
                "prompt": "ping",
                "n_predict": 1,
                "cache_prompt": False,
                "id_slot": max(0, n_slots - 1),
            }, timeout=60.0)
            state.last_activity = time.time()
            print("[server_manager] keepalive ping (1 tok) - VRAM kept resident")
        except Exception:
            pass
