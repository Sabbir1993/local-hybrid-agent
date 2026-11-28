"""tests/test_main_server.py - multi-mode main model: several llama-servers, supervised one by one.

Run: python -m unittest tests.test_main_server -v

No llama-server is started and no socket is opened: subprocess.Popen, the VRAM check, the health
probes, the log file and the kill path are all patched. What these pin:
  - the primary (port 8090) and the extras (8191...) are launched with their own GPUs and slots
  - every server is checked before any starts
  - a failed extra does not fail the load; a dead primary restarts alone; a dead extra restarts alone
  - the crash-loop breaker and the stop path cover the extras too
  - single mode launches exactly the command it always did
"""

import asyncio
import copy
import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import process
from core import state as state_mod
from core.main_server import MainInstance
from core.state import ProxyState
from core.supervision import MAX_RESTART_COUNT


class FakePopen:
    def __init__(self, cmd, alive=True, returncode=1):
        self.cmd = list(cmd)
        self.stdout = io.StringIO("")          # the log pump reads this and sees EOF at once
        self._alive = alive
        self.returncode = None if alive else returncode
        self.pid = 4000
        self.killed = False

    def poll(self):
        return None if self._alive else self.returncode

    def die(self, code=1):
        self._alive = False
        self.returncode = code

    def kill(self):
        self.killed = True
        self.die(-1)


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def _profile(instances=None):
    p = {"model_path": "C:/m.gguf", "llama_bin_dir": "C:/llama", "backend": "vulkan",
         "gpu_devices": [0, 1], "n_slots": 1}
    if instances is not None:
        p["main_mode"] = "multi"
        p["main_instances"] = instances
    return p


TWO_SERVERS = [{"gpus": [0], "n_slots": 2}, {"gpus": [1], "n_slots": 4}]


def run(coro):
    return asyncio.run(coro)


async def _tick_until(st, until, ticks=400):
    """Drive the real watchdog loop with its waits collapsed, until `until()` is true."""
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(0)

    with mock.patch.object(asyncio, "sleep", fast_sleep):
        task = asyncio.ensure_future(st.watchdog())
        try:
            for _ in range(ticks):
                await real_sleep(0)
                if until():
                    break
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class MainServerCase(unittest.TestCase):
    def setUp(self):
        self.popens: list = []
        self.checks: list = []
        self.wait_calls: list = []
        self.extra_health = True
        self.check_fail_gpu = None         # a GPU whose preflight should refuse
        self.extra_dead_port = None        # a port whose process exits at once
        patches = [
            mock.patch("core.process.find_llama_server", lambda d: Path("C:/llama/llama-server.exe")),
            mock.patch("core.process.resolve_device_ids",
                       side_effect=lambda devs, **kw: [f"Vulkan{d}" for d in devs]),
            mock.patch("core.process.apply_runtime", lambda p: None),
            mock.patch("core.state.apply_runtime", lambda p: None),
            mock.patch("core.vram.check_or_raise", side_effect=self._check),
            mock.patch("subprocess.Popen", side_effect=self._popen),
            mock.patch("core.state.kill_process_tree", side_effect=_kill),
            mock.patch("core.main_server.kill_process_tree", side_effect=_kill),
            mock.patch("core.state._write_log_file"),
            mock.patch("core.state.log_event"),
            mock.patch("core.main_server.log_event"),
            mock.patch("core.state.forget_engine_pid"),
            mock.patch.object(MainInstance, "_health_ok", new=_health),
            mock.patch.object(ProxyState, "_wait_healthy", new=self._wait_fn()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _check(self, profile, *a, **k):
        self.checks.append((list(profile["gpu_devices"]), profile["n_slots"]))
        if self.check_fail_gpu is not None and profile["gpu_devices"] == [self.check_fail_gpu]:
            raise RuntimeError("not enough free VRAM")
        return {}

    def _popen(self, cmd, **kw):
        port = _flag(cmd, "--port") if "--port" in cmd else None
        proc = FakePopen(cmd, alive=not (port is not None and port == str(self.extra_dead_port)))
        self.popens.append(proc)
        return proc

    def _wait_fn(self):
        """A plain function, so it binds as a method of ProxyState (self = the state)."""
        calls = self.wait_calls

        async def wait(st, watch_gpus=None):
            calls.append(watch_gpus)
            st.healthy = True
            st.loading = False
        return wait

    async def _load(self, profile):
        st = ProxyState()
        await st.load_profile(profile)
        return st


def _kill(proc, timeout=15):
    if proc is not None and proc.poll() is None:
        proc.kill()


async def _health(inst):
    """Every extra answers /health 200 unless a test says otherwise (see MainServerCase.extra_health)."""
    return True


class MultiLoad(MainServerCase):
    def test_primary_and_one_extra_launch_on_their_own_ports(self):
        async def go():
            st = await self._load(_profile(TWO_SERVERS))
            return st

        st = run(go())
        self.assertEqual(len(self.popens), 2)
        self.assertEqual(_flag(self.popens[0].cmd, "--port"), "8090")
        self.assertEqual(_flag(self.popens[1].cmd, "--port"), "8191")
        self.assertEqual(_flag(self.popens[1].cmd, "-np"), "4")
        self.assertEqual(_flag(self.popens[1].cmd, "-dev"), "Vulkan1")
        self.assertEqual(len(st.extras), 1)
        self.assertTrue(st.extras[0].healthy)
        self.assertEqual(self.wait_calls, [[0]], "the primary's VRAM watch uses its own GPUs")

    def test_each_server_is_checked_with_its_own_gpus_and_slots(self):
        run(self._load(_profile(TWO_SERVERS)))
        self.assertEqual(self.checks, [([0], 2), ([1], 4)])

    def test_launch_info_and_status_describe_every_server(self):
        st = run(self._load(_profile(TWO_SERVERS)))
        self.assertEqual(st.launch_info["mode"], "multi")
        self.assertEqual([i["port"] for i in st.launch_info["instances"]], [8090, 8191])
        self.assertEqual(st.launch_info["n_slots"], 2, "n_slots stays the primary's count")
        status = st.instances_status()
        self.assertEqual([s["port"] for s in status], [8090, 8191])
        self.assertEqual(status[0]["index"], 0)
        self.assertEqual(status[1]["n_slots"], 4)
        self.assertTrue(status[1]["healthy"])

    def test_log_lines_carry_the_instance_number(self):
        st = run(self._load(_profile(TWO_SERVERS)))
        with mock.patch("core.state._write_log_file") as write, mock.patch("builtins.print"):
            st.extras[0]._record("hello")
        write.assert_called_with("[#2] hello")
        self.assertEqual(st.extras[0].log_tail(1), ["hello"])


class PreflightFirst(MainServerCase):
    def test_refusal_on_the_second_server_starts_nothing(self):
        self.check_fail_gpu = 1

        async def go():
            st = ProxyState()
            with self.assertRaises(RuntimeError) as cm:
                await st.load_profile(_profile(TWO_SERVERS))
            return st, str(cm.exception)

        st, msg = run(go())
        self.assertIn("Server 2", msg)
        self.assertEqual(self.popens, [], "no server may start when any server fails its check")
        self.assertIsNone(st.process)


class ExtraFailure(MainServerCase):
    def test_a_failed_extra_does_not_fail_the_load(self):
        self.extra_dead_port = 8191

        st = run(self._load(_profile(TWO_SERVERS)))
        self.assertTrue(st.healthy, "the primary is serving, so the load succeeds")
        extra = st.extras[0]
        self.assertIsNone(extra.process)
        self.assertIn("exited code 1", extra.last_error)
        self.assertIn("exited code 1", st.instances_status()[1]["error"])


class PrimaryDeath(MainServerCase):
    def test_a_dead_primary_restarts_alone(self):
        async def go():
            st = await self._load(_profile(TWO_SERVERS))
            primary_proc = st.process
            extra_proc = self.popens[1]
            primary_proc.die(1)
            await _tick_until(st, lambda: len(self.popens) == 3)
            return st, primary_proc, extra_proc

        st, primary_proc, extra_proc = run(go())
        self.assertEqual(len(self.popens), 3, "exactly one new launch: the primary")
        self.assertEqual(_flag(self.popens[2].cmd, "--port"), "8090")
        self.assertFalse(extra_proc.killed, "the extra must keep serving while the primary restarts")
        self.assertTrue(st.extras[0].is_running())
        self.assertIsNot(st.process, primary_proc)
        self.assertEqual(self.wait_calls[-1], [0])


class ExtraDeath(MainServerCase):
    def test_a_dead_extra_restarts_alone(self):
        st = run(self._load(_profile(TWO_SERVERS)))
        primary_proc = st.process
        inst = st.extras[0]
        inst._backoff = 0
        inst.process.die(1)

        async def go():
            with mock.patch("core.main_server.max_restart_backoff_s", return_value=0):
                await inst.supervise_once(st.profile)     # counts the death, sets the deadline
                await inst.supervise_once(st.profile)     # the deadline has passed: relaunch

        run(go())
        self.assertIs(st.process, primary_proc, "the primary is not touched")
        self.assertFalse(primary_proc.killed)
        self.assertEqual(len(self.popens), 3)
        self.assertEqual(_flag(self.popens[2].cmd, "--port"), "8191")
        self.assertTrue(inst.is_running())
        self.assertEqual(inst.restart_count, 1)

    def test_the_crash_loop_breaker_degrades_the_extra(self):
        st = run(self._load(_profile(TWO_SERVERS)))
        inst = st.extras[0]

        async def go():
            with mock.patch("core.main_server.max_restart_backoff_s", return_value=0):
                for _ in range(4 * MAX_RESTART_COUNT + 10):
                    if inst.degraded:
                        break
                    inst._backoff = 0        # a healthy start resets the backoff to 1s; keep the test fast
                    if inst.is_running():
                        inst.process.die(1)      # it comes up and dies again, every time
                    await inst.supervise_once(st.profile)
                launched = len(self.popens)
                await inst.supervise_once(st.profile)
                await inst.supervise_once(st.profile)
                return launched

        launched = run(go())
        self.assertTrue(inst.degraded)
        self.assertEqual(inst.restart_count, MAX_RESTART_COUNT)
        self.assertEqual(len(self.popens), launched, "a degraded extra must not be relaunched")
        self.assertIn("automatic", inst.last_error)

    def test_a_supervision_error_never_reaches_the_watchdog(self):
        st = run(self._load(_profile(TWO_SERVERS)))
        with mock.patch.object(MainInstance, "supervise_once", side_effect=RuntimeError("boom")):
            run(st._supervise_extras())      # must not raise
        self.assertTrue(st.is_running(), "the primary is untouched by an extra's failure")


class StopAndSingle(MainServerCase):
    def test_stop_kills_every_server_and_forgets_the_extras(self):
        st = run(self._load(_profile(TWO_SERVERS)))
        procs = list(self.popens)
        run(st.stop())
        self.assertTrue(all(p.killed for p in procs))
        self.assertEqual(st.extras, [])
        self.assertIsNone(st.process)

    def test_single_mode_launches_the_same_command_as_before(self):
        profile = _profile()
        expected = process.build_launch_command(copy.deepcopy(profile))
        st = run(self._load(profile))
        self.assertEqual(len(self.popens), 1)
        self.assertEqual(self.popens[0].cmd, expected)
        self.assertEqual(st.extras, [])
        self.assertNotIn("mode", st.launch_info)
        self.assertEqual([s["port"] for s in st.instances_status()], [8090])


if __name__ == "__main__":
    unittest.main()
