"""tests/test_state_supervision.py - llama-server process supervision.

Run: python -m unittest tests.test_state_supervision -v

Two holes this covers, both of which left the platform unusable rather than merely wrong:

  - the health-check timeout path killed nothing. self.process stayed a live Popen holding
    the full weights in VRAM, is_running() kept returning True, the watchdog skipped it
    (poll() is None) and /control/status reported a live pid. One slow load bricked the
    platform until a manual restart.
  - restart_count was monotonic with no ceiling, so a model that could not launch retried
    once a minute for the life of the process.
"""

import asyncio
import collections
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import state as state_mod
from core import supervision as supervision_mod
from core.supervision import MAX_RESTART_COUNT, RESTART_RESET_AFTER_S
from core.state import ProxyState


class FakePopen:
    """Just enough of subprocess.Popen for the supervision paths: alive or exited, with a
    kill hook so a test can prove the process was actually reclaimed."""

    def __init__(self, alive: bool = True, returncode: int = 1):
        self._alive = alive
        self.returncode = None if alive else returncode
        self.pid = 4242
        self.killed = False

    def poll(self):
        return None if self._alive else (self.returncode if self.returncode is not None else 1)

    def kill(self):
        self.killed = True
        self._alive = False
        self.returncode = -1

    def cmdline(self):
        return ["llama-server", "--port", "8090"]


def _bare_state():
    '''A ProxyState built without __init__, so no httpx client or locks. Includes _log_tail:
    _load_failed reads it via _exit_reason to fold in what llama-server itself said (B4).'''
    st = ProxyState.__new__(ProxyState)
    st._log_tail = collections.deque(maxlen=64)
    return st


def run(coro):
    return asyncio.run(coro)


class HealthTimeoutReclaimsTheProcess(unittest.TestCase):
    """The one that bricked the platform."""

    def _state(self, proc):
        st = _bare_state()
        st._log_tasks = set()
        st.process = proc
        st.started_at = 0.0
        st.profile = None
        st.restart_count = 0
        st.degraded = False
        st.last_load_error = None
        st.lock = asyncio.Lock()
        return st

    def _timeout_now(self):
        with mock.patch.object(state_mod, "health_timeout_s", return_value=0):
            return run(self._state(FakePopen(alive=True))._wait_healthy())

    def test_the_process_is_killed_not_just_flagged(self):
        proc = FakePopen(alive=True)
        st = self._state(proc)
        with mock.patch.object(state_mod, "health_timeout_s", return_value=0):
            with self.assertRaises(RuntimeError):
                run(st._wait_healthy())
        self.assertTrue(proc.killed, "the hung llama-server must be killed, not left holding VRAM")

    def test_is_running_is_false_afterwards(self):
        st = self._state(FakePopen(alive=True))
        with mock.patch.object(state_mod, "health_timeout_s", return_value=0):
            with self.assertRaises(RuntimeError):
                run(st._wait_healthy())
        self.assertIsNone(st.process)
        self.assertFalse(st.is_running(),
                         "a leaked handle made is_running() report a server that never started")

    def test_the_reason_is_recorded_for_the_status_endpoint(self):
        st = self._state(FakePopen(alive=True))
        with mock.patch.object(state_mod, "health_timeout_s", return_value=0):
            with self.assertRaises(RuntimeError):
                run(st._wait_healthy())
        self.assertIsNotNone(st.last_load_error)
        self.assertIn("didn't become healthy", st.last_load_error)

    def test_it_does_not_claim_the_vram_wall_was_hit(self):
        """The same helper is used for the VRAM wall, so a generic failure must not report a
        VRAM index it never observed."""
        st = self._state(FakePopen(alive=True))
        with mock.patch.object(state_mod, "health_timeout_s", return_value=0):
            with self.assertRaises(RuntimeError):
                run(st._wait_healthy())
        self.assertNotIn("VulkanNone", st.last_load_error)

    def test_an_immediate_exit_is_also_reported_and_cleared(self):
        st = self._state(FakePopen(alive=False, returncode=1))
        with self.assertRaises(RuntimeError) as cm:
            run(st._wait_healthy())
        self.assertIn("exited immediately", str(cm.exception))
        self.assertIn("code 1", str(cm.exception))
        self.assertIsNone(st.process)
        self.assertFalse(st.is_running())


class HealthyLoadClearsTheFailureState(unittest.TestCase):
    def test_reaching_health_clears_the_error_and_degraded_flag(self):
        st = _bare_state()
        st.restart_count = 4
        st.degraded = True
        st.last_load_error = "something went wrong earlier"
        st._mark_healthy()
        self.assertFalse(st.degraded)
        self.assertIsNone(st.last_load_error)

    def test_reaching_health_does_NOT_clear_the_crash_loop_budget(self):
        """The subtle one. A model too big for the card answers /health and then OOM-crashes on
        the first real generation, so every crash-loop cycle passes through here. Resetting the
        count here would mean the breaker could never trip in exactly the case it exists for."""
        st = _bare_state()
        st.restart_count = 4
        st.degraded = False
        st.last_load_error = None
        st._mark_healthy()
        self.assertEqual(st.restart_count, 4,
                         "reaching /health is not proof of stability; uptime is")

    def test_long_uptime_does_reset_the_budget(self):
        """The other half of the contract: a process that demonstrably worked should not be
        counted against the breaker just because it eventually died."""
        async def go():
            st = _bare_state()
            st._log_tasks = set()
            st.profile = {"name": "demo"}
            st.profile_path = None
            st.started_at = time.time() - (RESTART_RESET_AFTER_S + 60)   # up for an hour
            st.restart_count = MAX_RESTART_COUNT - 1
            st.degraded = False
            st.last_load_error = None
            st.lock = asyncio.Lock()
            st.process = FakePopen(alive=False, returncode=1)
            st._kill_process_silently = lambda: setattr(st, "process", None)

            real_sleep = asyncio.sleep

            async def fast_sleep(delay, *a, **k):
                await real_sleep(0)

            async def fake_load(target):
                st.process = FakePopen(alive=False, returncode=1)
                st.started_at = time.time() - (RESTART_RESET_AFTER_S + 60)

            with mock.patch.object(state_mod, "watchdog_interval_s", return_value=0), \
                 mock.patch.object(asyncio, "sleep", fast_sleep), \
                 mock.patch.object(st, "_load_locked", side_effect=fake_load):
                task = asyncio.ensure_future(st.watchdog())
                for _ in range(20):
                    await real_sleep(0)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return st

        st = run(go())
        self.assertFalse(st.degraded, "a long-lived process is not a crash loop")


class CrashLoopBreaker(unittest.TestCase):
    """restart_count used to be monotonic with no ceiling."""

    def _watchdog_once(self, st, attempts):
        """Drive the watchdog body with the waits collapsed to zero. `attempts` is how many
        load attempts should fail before it stops trying."""
        async def go():
            calls = []
            # backoff starts at 1s, so a real sleep would make this test take seconds and the
            # drive loop below would never see more than one attempt. Collapse every sleep to a
            # zero-sleep (still yields, so other tasks run).
            real_sleep = asyncio.sleep

            async def fast_sleep(delay, *a, **k):
                await real_sleep(0)

            async def fake_load(target):
                # Model a real crash loop: the server starts, answers /health, then dies.
                # If _load_locked raised instead, process would end up None and the watchdog
                # would simply stop - which is a different (already-fixed) failure mode.
                calls.append(target)
                st.process = FakePopen(alive=False, returncode=1)
                st.started_at = time.time()        # just started, so short uptime
                st._mark_healthy()                 # it does report healthy before crashing

            with mock.patch.object(state_mod, "watchdog_interval_s", return_value=0), \
                 mock.patch.object(state_mod, "max_restart_backoff_s", return_value=0), \
                 mock.patch.object(asyncio, "sleep", fast_sleep), \
                 mock.patch.object(st, "_load_locked", side_effect=fake_load):
                task = asyncio.ensure_future(st.watchdog())
                for _ in range(500):
                    await real_sleep(0)
                    if st.degraded or len(calls) >= attempts:
                        break
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return calls

        return run(go())

    def _state(self):
        st = _bare_state()
        st._log_tasks = set()
        st.profile = {"name": "demo"}
        st.profile_path = None
        st.started_at = 0.0
        st.restart_count = 0
        st.degraded = False
        st.last_load_error = None
        st.lock = asyncio.Lock()
        st.process = FakePopen(alive=False, returncode=1)
        st._kill_process_silently = lambda: setattr(st, "process", None)
        return st

    def test_it_actually_trips_after_the_ceiling(self):
        st = self._state()
        self._watchdog_once(st, attempts=MAX_RESTART_COUNT + 4)
        self.assertTrue(st.degraded, "a model that cannot launch must stop being retried")

    def test_it_stops_retrying_once_degraded(self):
        st = self._state()
        self._watchdog_once(st, attempts=MAX_RESTART_COUNT + 4)
        self.assertTrue(st.degraded)
        # a dead handle left behind would otherwise keep the loop looking for work
        st.process = FakePopen(alive=False, returncode=1)
        before = st.restart_count
        self._watchdog_once(st, attempts=0)
        self.assertEqual(st.restart_count, before, "a degraded watchdog must not try again")

    def test_the_counter_is_not_reset_by_the_restart_itself(self):
        """The watchdog restarts via _load_locked, not load_profile. load_profile re-arms the
        breaker by design, so using it here would reset the count every attempt and the breaker
        could never trip."""
        import inspect
        src = inspect.getsource(ProxyState.watchdog)
        self.assertIn("self._load_locked(target)", src)
        self.assertNotIn("await self.load_profile(target)", src)

    def test_degraded_explains_itself(self):
        st = self._state()
        self._watchdog_once(st, attempts=MAX_RESTART_COUNT + 4)
        self.assertIn("automatic", (st.last_load_error or "").lower())
        self.assertIn("load a model", (st.last_load_error or "").lower())


class DegradedBlocksTheLazyAutostart(unittest.TestCase):
    """Otherwise every request quietly re-triggers the load that just failed five times."""

    def test_ensure_running_refuses_and_explains(self):
        async def go():
            st = _bare_state()
            st._log_tasks = set()
            st.process = None
            st.profile = {"name": "demo"}
            st.degraded = True
            st.restart_count = MAX_RESTART_COUNT
            st.last_load_error = "it would not stay up"
            st.lock = asyncio.Lock()

            loaded = []

            async def spy(target):
                loaded.append(target)

            st._load_locked = spy
            with self.assertRaises(RuntimeError) as cm:
                await st.ensure_running("demo")
            return str(cm.exception), loaded

        msg, loaded = run(go())
        self.assertEqual(loaded, [], "a degraded server must not be retried on request")
        self.assertIn("would not stay up", msg)

    def test_a_manual_load_re_arms_the_breaker(self):
        """The escape hatch has to exist, or recovering needs a full server restart."""

        async def go():
            st = _bare_state()
            st._log_tasks = set()
            st.process = None
            st.degraded = True
            st.restart_count = MAX_RESTART_COUNT
            st.last_load_error = "old failure"
            st.lock = asyncio.Lock()

            async def spy(target):
                return None

            st._load_locked = spy
            await st.load_profile("demo")
            return st.degraded, st.restart_count

        degraded, restarts = run(go())
        self.assertFalse(degraded, "an explicit load must re-arm automatic restart")
        self.assertEqual(restarts, 0)


class StatusEndpointReportsSupervision(unittest.TestCase):
    def test_status_exposes_the_breaker(self):
        from pathlib import Path as P
        src = (P(__file__).resolve().parent.parent / "routes" / "control" / "status_endpoints.py"
               ).read_text(encoding="utf-8")
        for key in ("degraded", "last_load_error", "max_restart_count"):
            self.assertIn(f'"{key}"', src, f"/control/status must report {key}")

    def test_the_ceiling_is_a_real_number(self):
        self.assertIsInstance(MAX_RESTART_COUNT, int)
        self.assertGreater(MAX_RESTART_COUNT, 0)


if __name__ == "__main__":
    unittest.main()