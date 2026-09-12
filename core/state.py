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
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{LLAMA_SERVER_PORT}", timeout=600.0)

    async def load_profile(self, target: Union[Path, dict, str]):
        async with self.lock:
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
            await asyncio.sleep(1.0)
        raise RuntimeError(f"llama-server didn't become healthy within {HEALTH_TIMEOUT_S}s")

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
            await state.client.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "temperature": 0.1,
            }, timeout=60.0)
            state.last_activity = time.time()
            print("[server_manager] keepalive ping (1 tok) - VRAM kept resident")
        except Exception:
            pass
