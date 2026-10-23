import asyncio
import collections
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Union

import httpx

from .config import (
    KEEPALIVE_INTERVAL_S,
    LLAMA_SERVER_PORT,
    LOCAL_HTTP_CONNECT_TIMEOUT_S,
    LOCAL_HTTP_POOL_TIMEOUT_S,
    LOCAL_HTTP_READ_TIMEOUT_S,
    LOCAL_HTTP_WRITE_TIMEOUT_S,
    LOG_DIR,
    LOG_KEEP_DAYS,
    LOG_TAIL_LINES,
    LOG_WRITE_FILE,
)
from .supervision import (
    health_timeout_s,
    max_restart_backoff_s,
    max_restart_count,
    restart_reset_after_s,
    watchdog_interval_s,
)
from .process import build_launch_command, kill_process_tree
from .profiles import build_dynamic_profile
from .events import log_event
from . import vram

# ---------------- llama-server log file ----------------
# One file per day, bounded by LOG_KEEP_DAYS, opened lazily. Never raises: a log that cannot
# be written must not take the model server down with it.
_log_fh = None
_log_day = ""

# ---------------- model-swap hooks ----------------
# Learned per-model state (context-budget anchor/TPS EMA, tool_choice verdicts)
# describes the OLD llama-server process: a swap to a different GGUF must not
# let it silently carry onto the new one. Owners of such state register a hook
# here instead of state importing them (dependency direction: hooks point
# INTO state). Hooks run after a HEALTHY load only, each guarded - hygiene must
# never fail the load it decorates. Firing on every healthy load (including the
# very first at boot) is harmless: a fresh process has no stale state anyway.
_SWAP_HOOKS: list = []


def register_swap_hook(fn) -> None:
    """Register fn() to run after each healthy model load. Idempotent."""
    if fn not in _SWAP_HOOKS:
        _SWAP_HOOKS.append(fn)


def notify_swap() -> None:
    for fn in list(_SWAP_HOOKS):
        try:
            fn()
        except Exception:        # noqa: BLE001 - a broken hook must not fail a load
            pass


def _write_log_file(line: str) -> None:
    global _log_fh, _log_day
    if not LOG_WRITE_FILE:
        return
    try:
        day = time.strftime("%Y%m%d")
        if _log_fh is None or day != _log_day:
            if _log_fh is not None:
                try:
                    _log_fh.close()
                except Exception:
                    pass
                _log_fh = None
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _log_fh = open(LOG_DIR / f"llama-server-{day}.log", "a", encoding="utf-8",
                            errors="replace")
            _log_day = day
            _prune_log_files()
        _log_fh.write(line + "\n")
        _log_fh.flush()
    except Exception:
        _log_fh = None          # stop trying every line after a failure


def _prune_log_files() -> None:
    try:
        files = sorted(LOG_DIR.glob("llama-server-*.log"))
        for old in files[:-LOG_KEEP_DAYS]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def close_log_file() -> None:
    global _log_fh, _log_day
    if _log_fh is not None:
        try:
            _log_fh.close()
        except Exception:
            pass
    _log_fh, _log_day = None, ""


class ProxyState:
    def __init__(self):
        self.profile_path: Optional[Path] = None
        self.profile: Optional[dict] = None
        self.process: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None
        self.restart_count = 0
        # set when the restart budget is exhausted: the watchdog stops trying, and the UI can
        # say why nothing is running
        self.degraded = False
        self.loading = False
        self.healthy = False
        # why the last load failed, for /control/status. Without it an operator sees only
        # "didn't become healthy" with nothing in the status endpoint to act on.
        self.last_load_error: Optional[str] = None
        self.lock = asyncio.Lock()
        self.watchdog_task: Optional[asyncio.Task] = None
        self.keepalive_task: Optional[asyncio.Task] = None
        self.keepalive_enabled = _keepalive_default()
        self.keepalive_interval_s = KEEPALIVE_INTERVAL_S
        self.last_activity = time.time()
        self._log_tasks: set = set()
        # the main model's stdout used to reach print() and nothing else, so the OOM trace
        # from a failed load lived in console scrollback and died with the terminal
        self._log_tail = collections.deque(maxlen=LOG_TAIL_LINES)
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{LLAMA_SERVER_PORT}",
            # was timeout=None: a wedged generation could never be ended, because the agent's
            # wall-clock budget is only checked between steps
            timeout=httpx.Timeout(LOCAL_HTTP_CONNECT_TIMEOUT_S,
                                  read=LOCAL_HTTP_READ_TIMEOUT_S,
                                  write=LOCAL_HTTP_WRITE_TIMEOUT_S,
                                  pool=LOCAL_HTTP_POOL_TIMEOUT_S))

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def ensure_running(self, target: Union[Path, dict, str]):
        """Auto-start for request paths: a no-op when llama-server is already
        up. Re-checked under the lock, so N concurrent requests that all find
        the model down trigger exactly one launch (the rest wait for it)
        instead of each killing and relaunching the previous one.

        Honours `degraded`: after the crash-loop breaker trips, a request must not quietly
        re-trigger the load that failed five times. The user loads a model by hand instead,
        which re-arms it."""
        if self.is_running():
            return
        if self.degraded:
            raise RuntimeError(
                self.last_load_error
                or "the model server is not running and automatic restart is off; "
                   "load a model in Settings → Models & Jobs to start it again.")
        async with self.lock:
            if self.is_running():
                return
            await self._load_locked(target)

    async def load_profile(self, target: Union[Path, dict, str]):
        """Explicit (re)load: always stops the current process first.

        Re-arms the crash-loop breaker. Without this, `degraded` would latch after five failed
        restarts and the only way back would be restarting the whole server, even though the
        admin has just asked for a load on purpose - which is exactly what they do to recover
        from whatever caused the loop."""
        async with self.lock:
            self.degraded = False
            self.restart_count = 0
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

        # Preflight: refuse to spawn llama-server if the VRAM math says it
        # won't fit (a WDDM OOM spill hangs the whole desktop on Arc). It runs first
        # because a MoE model's expert offload is chosen from the free VRAM it measures.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, vram.check_or_raise, self.profile)
        cmd = build_launch_command(self.profile)
        print(f"[server_manager] launching: {' '.join(cmd)}")
        self.loading = True
        self.healthy = False
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.started_at = time.time()
        # keep the handle: the pump read from the process pipe forever and nothing held a
        # reference, so every load added an untracked task that only died with the process
        self._log_task = self._spawn_log_pump(self.process)
        log_event("server.load_started", pid=self.process.pid,
                  model=Path(str(self.profile.get("model_path") or "")).name)
        await self._wait_healthy()
        notify_swap()

    async def stop(self, clear_profile: bool = False):
        async with self.lock:
            self._stop_process_locked()
            self.started_at = None
            if clear_profile:
                # under the same lock acquisition as the stop. The route used to write
                # state.profile itself straight after awaiting stop(), so a request landing in
                # between could see a cleared profile with a live process, or relaunch the model
                # the admin had just stopped.
                self.profile = None

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
            self._record_log(line.rstrip())

    def _record_log(self, line: str) -> None:
        """One line of llama-server output: ring buffer, daily log file, and console.

        The ring buffer is what makes a failed load diagnosable after the fact - it is what
        _exit_reason() greps. Previously the main model's stdout went to print() only, so the
        OOM trace existed in console scrollback and nowhere else. Small-model lanes already
        worked this way (core/small_model/instance.py::_pump_logs).
        """
        self._log_tail.append(line)
        _write_log_file(line)
        print(f"[llama-server] {line}")

    def log_tail(self, n: int = 60) -> list:
        """The most recent lines, oldest first. Served by /control/status and /control/log."""
        return list(self._log_tail)[-max(1, int(n)):]

    def _exit_reason(self) -> Optional[str]:
        """The last error-looking line of llama-server's own output, or None.

        Same intent as core/small_model/instance.py::_exit_reason, and PAN-masked for the same
        reason: a log line is data, and anything that can reach a prompt or another user's view
        has to go through the mask first."""
        errs = [l for l in self._log_tail
                if re.search(r"\berror\b|failed|not found|cannot allocate|out of memory|"
                             r"cannot allocate memory|assert", l, re.I)]
        if not errs:
            return None
        from .pan import mask_pans
        flat = re.sub(r"\s+", " ", errs[-1]).strip()
        return mask_pans(flat[-300:])[0] or None

    def _unknown_architecture(self):
        """The model family llama-server refused ("unknown model architecture: 'flint'"), or None. A model newer than the
        server program is the cause; the generic "bad path or VRAM" hint sent people looking in the wrong place."""
        for line in reversed(self._log_tail):
            m = re.search(r"unknown model architecture:\s*'([^'\n]{1,60})'", line)
            if m:
                return m.group(1)
        return None

    async def _wait_healthy(self):
        deadline = time.time() + health_timeout_s()
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
                # already dead, so there is nothing to reclaim; drop the handle anyway so
                # is_running() does not report on a process that exited
                code = self.process.returncode
                self._kill_loading_process()
                arch = self._unknown_architecture()
                if arch:
                    raise self._load_failed(
                        f"This model uses the '{arch}' architecture, which the installed llama.cpp does not know yet "
                        f"(the model is newer than the server program). It is not a path or VRAM problem. Update the "
                        f"llama.cpp build in the Vulkan runtime to one that supports '{arch}', or pick a model it already supports.")
                raise self._load_failed(
                    f"llama-server exited immediately (code {code}) - "
                    f"check the log lines above, this is almost always a bad model path "
                    f"or a VRAM allocation failure.")
            try:
                r = await self.client.get("/health", timeout=2.0)
                if r.status_code == 200:
                    print(f"[server_manager] llama-server healthy after "
                          f"{time.time() - self.started_at:.1f}s")
                    self._mark_healthy()
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
                    self._kill_loading_process()
                    raise self._load_failed(
                        f"VRAM exhausted on Vulkan{hit} while the model was loading - "
                        f"aborted llama-server to prevent a system hang. Reduce GPU "
                        f"layers (-ngl) / tensor-split / context size, or unload "
                        f"other models first, then load again.")
            await asyncio.sleep(1.0)
        # This path used to leak. The "exited immediately" case is dead already and the VRAM
        # wall case killed explicitly, but on timeout nothing was killed: self.process stayed a
        # live Popen holding the full weights in VRAM, is_running() kept returning True, the
        # watchdog skipped it (poll() is None), Target.available() reported ready and
        # /control/status showed a live pid. One slow load bricked the platform until a manual
        # restart - and nothing but the console explained why.
        self._kill_loading_process()
        raise self._load_failed(
            f"llama-server didn't become healthy within {health_timeout_s()}s - it has been "
            f"stopped so it does not hold VRAM. Load a smaller model, or raise the health "
            f"timeout, then try again.")

    def _mark_healthy(self) -> None:
        """A server that reached /health proves the load worked, so clear the failure state.

        Deliberately does NOT reset restart_count. Reaching /health is not the same as being
        stable: a model too big for the card comes up, answers /health, then OOM-crashes on
        the first real generation. Resetting here would clear the budget on every cycle of
        that loop and the crash-loop breaker could never fire. The count is reset instead when
        a process has demonstrably stayed up - see the watchdog."""
        self.degraded = False
        self.loading = False
        self.healthy = True
        self.last_load_error = None
        started = getattr(self, "started_at", None)
        log_event("server.healthy", load_seconds=round(time.time() - started, 1)
                  if started else None)

    def _load_failed(self, message: str) -> RuntimeError:
        """Record why the load failed so /control/status can say it. Operators otherwise only
        saw "didn't become healthy" with nothing to act on in the status endpoint.

        llama-server's own last error line is appended when there is one: it names the real
        cause (bad model path, failed mmap, allocator failure) where our message can only say
        the load timed out. This is the text that makes the ring buffer worth keeping."""
        self.loading = False
        self.healthy = False
        reason = self._exit_reason()
        full = f"{message} | llama-server: {reason}" if reason else message
        self.last_load_error = full
        proc = getattr(self, "process", None)
        log_event("server.load_failed",
                  reason=reason or message[:120],
                  code=proc.returncode if proc else None)
        return RuntimeError(full)

    def _kill_loading_process(self, vram_idx=None) -> None:
        self.loading = False
        self.healthy = False
        if self.process and self.process.poll() is None:
            if vram_idx is not None:
                print(f"[server_manager] VRAM wall hit on Vulkan{vram_idx} "
                      f"- killing the loading llama-server")
            else:
                print("[server_manager] llama-server failed to start - "
                      "killing it so it does not hold VRAM")
            kill_process_tree(self.process, timeout=15)
        self.process = None

    def _stop_process_locked(self):
        self.loading = False
        self.healthy = False
        self._cancel_log_pump()
        if self.process and self.process.poll() is None:
            print("[server_manager] stopping current llama-server...")
            kill_process_tree(self.process, timeout=15)
        if self.process is not None:
            log_event("server.stopped", pid=self.process.pid)
        self.process = None

    def _spawn_log_pump(self, proc) -> Optional[asyncio.Task]:
        try:
            task = asyncio.create_task(self._pump_logs(proc))
        except RuntimeError:
            return None          # no running loop (e.g. called from a sync context)
        self._log_tasks.add(task)
        task.add_done_callback(self._log_tasks.discard)
        return task

    def _cancel_log_pump(self) -> None:
        """Drop every log pump. Cancelling is a request: the executor thread blocked in
        readline() finishes on its own once the killed process closes the pipe."""
        for task in list(self._log_tasks):
            if not task.done():
                task.cancel()
        self._log_tasks.clear()

    async def watchdog(self):
        backoff = 1
        while True:
            await asyncio.sleep(watchdog_interval_s())
            if self.process is None:
                continue
            if self.process.poll() is None:
                continue
            # Crash-loop breaker. restart_count used to be monotonic with no ceiling, so a model
            # that cannot launch retried forever: every attempt spawned a process and burned up
            # to HEALTH_TIMEOUT_S in _wait_healthy, once a minute, for as long as the server ran.
            if self.degraded:
                continue
            # A process that stayed up for a while was working, whatever finally killed it, so
            # its death does not count toward the crash-loop budget. This is the only place
            # restart_count is reset, and it is what keeps a genuine crash loop (dies seconds
            # after starting, every time) distinguishable from an ordinary bad night.
            if self.started_at and (time.time() - self.started_at) > restart_reset_after_s():
                self.restart_count = 0
            if self.restart_count >= max_restart_count():
                self.degraded = True
                self.last_load_error = (
                    f"llama-server failed to stay up {self.restart_count}x in a row - automatic "
                    f"restart is off so it cannot spin forever. Fix the cause (see the log "
                    f"above), then load a model again to re-arm it.")
                print(f"[server_manager] giving up after {self.restart_count} restarts: "
                      f"automatic restart disabled", file=sys.stderr)
                log_event("server.degraded", restarts=self.restart_count,
                          code=self.process.returncode)
                self._kill_process_silently()
                continue
            self.restart_count += 1
            code = self.process.returncode
            reason = self._exit_reason()
            print(f"[server_manager] llama-server died (code {code}), "
                  f"restarting in {backoff}s (restart #{self.restart_count}"
                  f"/{max_restart_count()})")
            log_event("server.restart", restart=self.restart_count, code=code,
                      backoff_s=backoff, reason=reason)
            if reason:
                # what the server said, not merely that it died. This is the
                # difference between "it crashed" and a diagnosis, and before B4 it
                # existed only in console scrollback.
                self.last_load_error = f"llama-server exited (code {code}): {reason}"
                print(f"[server_manager] last error from the server: {reason}",
                      file=sys.stderr)
            dead = self.process
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_restart_backoff_s())
            # A switch/load that finished while we slept put ITS server up on this
            # slot: the stale restart's _load_locked would stop that healthy
            # process and relaunch the dead profile instead of the one the user
            # asked for. A live process that is not the one whose death began
            # this restart cancels the restart. The lock below is still acquired
            # under the (unchanged) contract that ensure_running holds the lock.
            if self.process and self.process is not dead and self.process.poll() is None:
                print("[server_manager] restart dropped: a new server finished "
                      "loading during the backoff", file=sys.stderr)
                log_event("server.restart_dropped")
                continue
            try:
                target = self.profile_path or self.profile
                if target:
                    # deliberately NOT load_profile(): that re-arms the breaker, so using it
                    # here would reset the count on every attempt and the breaker would never
                    # trip. This is the one restart path that must not clear the budget.
                    async with self.lock:
                        await self._load_locked(target)
                    backoff = 1     # _wait_healthy cleared the counters on success
            except Exception as e:
                print(f"[server_manager] restart failed: {e}", file=sys.stderr)
                # the failed attempt is not running either, so nothing is wedged, but the
                # process handle is: drop it or the watchdog sees a dead handle every tick
                self._kill_process_silently()

    def _kill_process_silently(self) -> None:
        """Reclaim a dead process handle without the 'stopping current llama-server' noise.
        Used by the watchdog, where the process has already exited on its own."""
        self._cancel_log_pump()
        if self.process and self.process.poll() is None:
            try:
                kill_process_tree(self.process, timeout=15)
            except Exception as e:
                print(f"[server_manager] reclaim error: {e}", file=sys.stderr)
        self.process = None


def _keepalive_default() -> bool:
    """On unless config runtime.keepalive is false: Windows demotes idle GPU allocations to
    system memory, and a model that has sat idle comes back slow."""
    try:
        from .small_model import APP_CONFIG
        return bool((APP_CONFIG.get("runtime") or {}).get("keepalive", True))
    except Exception:
        return True


# A one-token "ping" only touches what that token needs. A mixture-of-experts model routes each
# token to 8 of its 256 experts per layer, so a short ping leaves most expert weights untouched
# and they are the ones the OS pages out. This text is ~120 tokens of mixed prose, code, numbers
# and names so that the prefill routes through most experts; it is only used for MoE models.
_MOE_WARM_TEXT = (
    "Warm-up text, not a question. The quarterly report lists revenue, headcount and churn for "
    "Dhaka, Chattogram and Sylhet; def merge(a, b): return {**a, **b}; SELECT id, name FROM "
    "users WHERE active = 1; 3.14159 x 2718 = 8539; আমি বাংলায় কথা বলি; 日本語の文章です; "
    "Ceci est une phrase en français; the quick brown fox jumps over the lazy dog; "
    "import numpy as np; for i in range(10): print(i ** 2); JSON {\"k\": [1, 2, 3]}; "
    "photosynthesis, mitochondria, inflation, jurisprudence, thermodynamics, Shakespeare."
)


def keepalive_prompt(profile: Optional[dict]) -> str:
    try:
        from .vram.gguf_parser import parse_gguf_info
        from .vram.moe import is_moe
        path = (profile or {}).get("model_path")
        if path and is_moe(profile or {}, parse_gguf_info(path)):
            return _MOE_WARM_TEXT
    except Exception:
        pass
    return "ping"


SINGLE_SLOT_KEEPALIVE_IDLE_S = 300


def keepalive_idle_s(n_slots: int, interval_s: float) -> float:
    """Seconds without model activity before a keepalive ping. With one slot the ping runs on the SAME slot as the chat: it replaces
    that conversation's cached prompt, so the next request re-reads the whole prompt (35 s at 11k tokens) and starts from a slot
    holding the warm-up text. So with one slot it only pings after a long real idle, never between the rounds of a tool loop."""
    return float(interval_s) if int(n_slots or 1) > 1 else max(float(interval_s), SINGLE_SLOT_KEEPALIVE_IDLE_S)


state = ProxyState()


async def keepalive_loop():
    while True:
        await asyncio.sleep(5)
        if not state.keepalive_enabled:
            continue
        if state.process is None or state.process.poll() is not None:
            continue
        n_slots = int((state.profile or {}).get("n_slots") or 1)
        if time.time() - state.last_activity < keepalive_idle_s(n_slots, state.keepalive_interval_s):
            continue
        try:
            # Pin the ping to the last slot so, with -np > 1, it doesn't land on
            # (and overwrite) whichever slot holds the most recent user's cached
            # conversation prefix. With -np 1 there is no spare slot to use.
            await state.client.post("/completion", json={
                "prompt": keepalive_prompt(state.profile),
                "n_predict": 1,
                "cache_prompt": False,
                "id_slot": max(0, n_slots - 1),
            }, timeout=60.0)
            state.last_activity = time.time()
            print("[server_manager] keepalive ping (1 tok) - VRAM kept resident")
        except Exception:
            pass
