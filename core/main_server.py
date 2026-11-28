"""One extra llama-server of the main model (multi mode). Instance 0 is not here: it is ProxyState's own process.

Modelled on core/small_model/instance.py for the child process and on ProxyState._wait_healthy for start-up.
Supervision never sleeps: each instance keeps its own backoff timer, so a crash-looping extra cannot hold up the
watchdog tick that also serves the other instances.
"""
import asyncio
import collections
import re
import subprocess
import sys
import time
from typing import Optional

import httpx

from .config import (
    LOCAL_HTTP_CONNECT_TIMEOUT_S,
    LOCAL_HTTP_POOL_TIMEOUT_S,
    LOCAL_HTTP_READ_TIMEOUT_S,
    LOCAL_HTTP_WRITE_TIMEOUT_S,
)
from .events import log_event
from .process import build_launch_command, build_launch_environment, kill_process_tree
from .sycl_gate import launch_gate
from .supervision import health_timeout_s, max_restart_backoff_s, max_restart_count, restart_reset_after_s

LOG_TAIL_LINES = 2000


class MainInstance:
    """One llama-server for the main model on its own GPUs, port and slot count (spec from core/main_instances.py)."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.index = int(spec["index"])
        self.port = int(spec["port"])
        self.gpus = list(spec["gpus"])
        self.n_slots = int(spec["n_slots"])
        self.process: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None
        self.last_activity = time.time()
        self.healthy = False
        self.loading = False
        self.restart_count = 0
        self.degraded = False
        self.last_error: Optional[str] = None
        self._log_tail = collections.deque(maxlen=LOG_TAIL_LINES)
        self._log_tasks: set = set()
        # supervision state: the backoff is per instance and is applied as a deadline, not a sleep
        self._backoff = 1
        self._next_try_at: Optional[float] = None
        self._starting = False
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.port}",
            # same timeouts as the primary (core/state.py): a wedged generation must be endable
            timeout=httpx.Timeout(LOCAL_HTTP_CONNECT_TIMEOUT_S,
                                  read=LOCAL_HTTP_READ_TIMEOUT_S,
                                  write=LOCAL_HTTP_WRITE_TIMEOUT_S,
                                  pool=LOCAL_HTTP_POOL_TIMEOUT_S))

    @property
    def label(self) -> str:
        return f"main server #{self.index + 1}"

    async def start(self, profile: dict) -> None:
        """Launch and wait for /health 200. Raises RuntimeError (and leaves no process behind) on failure."""
        self._starting = True
        try:
            cmd = build_launch_command(profile, self.spec)
            env = build_launch_environment(profile, instance=self.spec)
            print(f"[server_manager] launching {self.label} on port {self.port}: {' '.join(cmd)}")
            self.loading = True
            self.healthy = False
            async with launch_gate(profile.get("backend")):
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",      # see core/state.py: no ANSI code-page decode on Windows
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
                self.started_at = time.time()
                self.last_error = None
                self._log_tail.clear()
                # keep the handle, same as the primary: an unreferenced pump task is never cancelled
                task = asyncio.create_task(self._pump_logs(self.process))
                self._log_tasks.add(task)
                task.add_done_callback(self._log_tasks.discard)

                deadline = time.time() + health_timeout_s()
                while time.time() < deadline:
                    if self.process.poll() is not None:
                        await asyncio.sleep(0.3)           # let the pump deliver the last lines
                        why = self._exit_reason()
                        msg = f"exited code {self.process.returncode}" + (f" - {why}" if why else "")
                        self._fail(msg)
                        raise RuntimeError(f"{self.label} failed to start ({msg})")
                    if await self._health_ok():
                        print(f"[server_manager] {self.label} healthy after {time.time() - self.started_at:.1f}s")
                        self.loading = False
                        self.healthy = True
                        self.last_activity = time.time()
                        self._backoff = 1
                        return
                    await asyncio.sleep(0.5)
                self._fail(f"didn't become healthy within {health_timeout_s()}s")
                raise RuntimeError(f"{self.label} didn't become healthy within {health_timeout_s()}s")
        finally:
            self._starting = False

    async def _health_ok(self) -> bool:
        try:
            r = await self.client.get("/health", timeout=2.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def _fail(self, msg: str) -> None:
        """Record a failed start and reclaim its process, so a half-started server never holds VRAM."""
        self.last_error = f"{self.label} {msg}"
        self._reclaim()

    def _reclaim(self) -> None:
        self._cancel_log_pump()
        if self.process is not None:
            if self.process.poll() is None:
                kill_process_tree(self.process, timeout=15)
        self.process = None
        self.healthy = False
        self.loading = False

    async def _pump_logs(self, proc: subprocess.Popen) -> None:
        if proc.stdout is None:
            return
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line:
                break
            self._record(line.rstrip())

    def _record(self, line: str) -> None:
        """Ring buffer, daily log file and console, with the instance number in front."""
        from .state import _write_log_file      # lazy: core.state imports this module
        self._log_tail.append(line)
        _write_log_file(f"[#{self.index + 1}] {line}")
        print(f"[llama-server#{self.index + 1}] {line}")

    def _cancel_log_pump(self) -> None:
        for task in list(self._log_tasks):
            if not task.done():
                task.cancel()
        self._log_tasks.clear()

    def _exit_reason(self) -> Optional[str]:
        errs = [l for l in self._log_tail
                if re.search(r"\berror\b|failed|not found|cannot allocate|out of memory|assert", l, re.I)]
        if not errs:
            return None
        from .pan import mask_pans              # PAN-masked: a log line is data
        flat = re.sub(r"\s+", " ", errs[-1]).strip()
        return mask_pans(flat[-300:])[0] or None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            print(f"[server_manager] stopping {self.label}...")
        self._reclaim()

    def log_tail(self, n: int = 60) -> list:
        return list(self._log_tail)[-max(1, int(n)):]

    async def supervise_once(self, profile: Optional[dict]) -> None:
        """One watchdog tick for this instance. Never raises and never sleeps.

        A death (or a failed start) is counted once, then a deadline is set; a later tick makes the attempt.
        The breaker and backoff mirror ProxyState.watchdog."""
        try:
            if self._starting or self.degraded or profile is None or self.is_running():
                return
            now = time.time()
            if self._next_try_at is None:
                if self.started_at and now - self.started_at > restart_reset_after_s():
                    self.restart_count = 0          # it was working for a while: not part of a crash loop
                if self.restart_count >= max_restart_count():
                    self.degraded = True
                    self.last_error = (f"{self.label} failed to stay up {self.restart_count}x in a row - automatic "
                                       f"restart is off. Fix the cause, then load the model again to re-arm it.")
                    print(f"[server_manager] giving up on {self.label} after {self.restart_count} restarts",
                          file=sys.stderr)
                    log_event("server.extra_degraded", index=self.index + 1, restarts=self.restart_count)
                    self._reclaim()
                    return
                self.restart_count += 1
                delay = self._backoff
                self._backoff = min(max(self._backoff * 2, 1), max_restart_backoff_s())
                code = self.process.returncode if self.process is not None else None
                self._reclaim()
                self._next_try_at = now + delay
                print(f"[server_manager] {self.label} died (code {code}), restarting in {delay}s "
                      f"(restart #{self.restart_count}/{max_restart_count()})")
                log_event("server.extra_restart", index=self.index + 1, restart=self.restart_count, code=code)
                return
            if now < self._next_try_at:
                return
            self._next_try_at = None
            await self.start(profile)
        except Exception as e:      # noqa: BLE001 - an extra must never break the watchdog
            self.last_error = f"{self.label}: {e}"
            self._reclaim()

    def status(self) -> dict:
        running = self.is_running()
        return {
            "index": self.index,
            "port": self.port,
            "gpus": list(self.gpus),
            "n_slots": self.n_slots,
            "healthy": self.healthy,
            "loading": self.loading,
            "pid": self.process.pid if running else None,
            "uptime_s": round(time.time() - self.started_at, 1) if running and self.started_at else None,
            "restart_count": self.restart_count,
            "degraded": self.degraded,
            "error": self.last_error,
        }

    async def aclose(self) -> None:
        self.stop()
        try:
            await self.client.aclose()
        except Exception:       # noqa: BLE001 - closing a pool that is already gone is fine
            pass
