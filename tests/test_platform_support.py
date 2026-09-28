"""Windows + Linux support: process kill, orphan cleanup, the GPU panel query
and the keyring-backend check. Both OS branches are exercised on whichever
OS runs the suite by patching IS_WINDOWS; no real processes are killed
except a throwaway `python -c sleep` child."""

import subprocess
import sys
import unittest
from unittest import mock

from core import credentials, gpu, process, state as state_mod, vram


class _FakeProc:
    def __init__(self, pid=4242, wait_raises=False):
        self.pid = pid
        self.calls = []
        self._alive = True
        self._wait_raises = wait_raises

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")
        self._alive = False

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self._wait_raises and "kill" not in self.calls:
            raise subprocess.TimeoutExpired("x", timeout)
        self._alive = False
        return 0


class KillProcessTreeTests(unittest.TestCase):
    def test_windows_uses_taskkill_tree(self):
        p = _FakeProc()
        with mock.patch.object(process, "IS_WINDOWS", True), \
                mock.patch.object(process.subprocess, "run") as run:
            process.kill_process_tree(p, timeout=3)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd, ["taskkill", "/F", "/T", "/PID", "4242"])
        self.assertNotIn("terminate", p.calls)

    def test_linux_terminates_without_taskkill(self):
        p = _FakeProc()
        with mock.patch.object(process, "IS_WINDOWS", False), \
                mock.patch.object(process.subprocess, "run") as run:
            process.kill_process_tree(p, timeout=3)
        run.assert_not_called()
        self.assertEqual(p.calls[:2], ["terminate", "wait"])
        self.assertNotIn("kill", p.calls)

    def test_linux_escalates_to_kill(self):
        p = _FakeProc(wait_raises=True)
        with mock.patch.object(process, "IS_WINDOWS", False):
            process.kill_process_tree(p, timeout=1)
        self.assertIn("kill", p.calls)
        self.assertIsNotNone(p.poll())

    def test_taskkill_missing_still_kills(self):
        # taskkill not on PATH / fails: the wait -> kill fallback still runs
        p = _FakeProc(wait_raises=True)
        with mock.patch.object(process, "IS_WINDOWS", True), \
                mock.patch.object(process.subprocess, "run", side_effect=FileNotFoundError):
            process.kill_process_tree(p, timeout=1)
        self.assertIn("kill", p.calls)

    def test_none_and_exited_are_noops(self):
        process.kill_process_tree(None)
        p = _FakeProc()
        p._alive = False
        with mock.patch.object(process.subprocess, "run") as run:
            process.kill_process_tree(p)
        run.assert_not_called()
        self.assertEqual(p.calls, [])

    def test_real_child_is_killed(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        process.kill_process_tree(child, timeout=10)
        self.assertIsNotNone(child.poll())


class OrphanCleanupTests(unittest.TestCase):
    def _run(self, is_windows, rc):
        with mock.patch.object(process, "IS_WINDOWS", is_windows), \
                mock.patch.object(process.subprocess, "run",
                                  return_value=mock.Mock(returncode=rc)) as run, \
                mock.patch.object(process.time, "sleep"):
            killed = process.kill_orphan_llama_servers()
        return killed, run.call_args[0][0]

    def test_windows_taskkill(self):
        killed, cmd = self._run(True, 0)
        self.assertTrue(killed)
        self.assertEqual(cmd[:4], ["taskkill", "/F", "/IM", "llama-server.exe"])

    def test_linux_pkill(self):
        killed, cmd = self._run(False, 0)
        self.assertTrue(killed)
        self.assertEqual(cmd, ["pkill", "-x", "llama-server"])

    def test_linux_nothing_to_kill(self):
        killed, _ = self._run(False, 1)
        self.assertFalse(killed)


class VramWallKillTests(unittest.TestCase):
    """Regression: on Linux the old taskkill-with-`pass` never stopped the
    loading llama-server when the VRAM wall was hit."""

    def test_loading_process_killed_on_linux(self):
        st = state_mod.ProxyState.__new__(state_mod.ProxyState)
        st.process = _FakeProc()
        proc = st.process
        with mock.patch.object(process, "IS_WINDOWS", False):
            st._kill_loading_process(0)
        self.assertIn("terminate", proc.calls)
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(st.process)


class GpuQueryTests(unittest.TestCase):
    DEVS = [
        {"index": 0, "name": "Intel Arc A770", "total_b": 16 * vram.GB,
         "free_b": 10 * vram.GB, "used_b": 6 * vram.GB},
        {"index": 1, "name": "Intel Arc A770", "total_b": 16 * vram.GB,
         "free_b": 16 * vram.GB, "used_b": 0},
        {"index": 2, "name": "llvmpipe", "total_b": vram.GB // 2,
         "free_b": vram.GB // 2, "used_b": 0},
    ]

    def test_linux_maps_list_devices_and_skips_powershell(self):
        with mock.patch.object(gpu, "IS_WINDOWS", False), \
                mock.patch.object(gpu.vram, "query_devices", return_value=self.DEVS), \
                mock.patch.object(gpu.subprocess, "run") as run:
            data = gpu._query_gpu_sync()
        run.assert_not_called()
        self.assertEqual(data["compute"], [])
        self.assertEqual(data["adapters"], [{"luid": "vulkan0", "gb": 6.0},
                                            {"luid": "vulkan1", "gb": 0.0}])

    def test_linux_query_failure_is_empty(self):
        with mock.patch.object(gpu, "IS_WINDOWS", False), \
                mock.patch.object(gpu.vram, "query_devices", side_effect=RuntimeError("no bench")):
            self.assertEqual(gpu._query_gpu_sync(), {"adapters": [], "compute": []})

    def test_windows_still_uses_powershell(self):
        out = mock.Mock(stdout='{"adapters": [{"luid": "0xabc", "gb": 5.5}], "compute": []}')
        with mock.patch.object(gpu, "IS_WINDOWS", True), \
                mock.patch.object(gpu.subprocess, "run", return_value=out) as run:
            data = gpu._query_gpu_sync()
        self.assertEqual(run.call_args[0][0][0], "powershell")
        self.assertEqual(data["adapters"], [{"luid": "0xabc", "gb": 5.5}])


class KeyringBackendTests(unittest.TestCase):
    def _backend(self, module, name="B", backends=None):
        cls = type(name, (), {"__module__": module})
        b = cls()
        if backends is not None:
            b.backends = backends
        return b

    def _check(self, backend):
        fake = mock.Mock(get_keyring=mock.Mock(return_value=backend))
        with mock.patch.object(credentials, "_keyring", return_value=fake):
            return credentials.keyring_backend_warning()

    def test_os_keychains_ok(self):
        for mod in ("keyring.backends.Windows", "keyring.backends.SecretService",
                    "keyring.backends.kwallet", "keyring.backends.macOS"):
            self.assertIsNone(self._check(self._backend(mod)), mod)

    def test_plaintext_and_fail_backends_warn(self):
        for mod in ("keyrings.alt.file", "keyring.backends.fail"):
            self.assertIn("insecure", self._check(self._backend(mod)), mod)

    def test_chainer_ok_only_with_secure_members(self):
        good = self._backend("keyring.backends.chainer",
                             backends=[self._backend("keyring.backends.SecretService")])
        bad = self._backend("keyring.backends.chainer",
                            backends=[self._backend("keyrings.alt.file")])
        empty = self._backend("keyring.backends.chainer", backends=[])
        self.assertIsNone(self._check(good))
        self.assertIsNotNone(self._check(bad))
        self.assertIsNotNone(self._check(empty))

    def test_keyring_import_error_warns(self):
        with mock.patch.object(credentials, "_keyring", side_effect=ImportError("no keyring")):
            self.assertIn("unavailable", credentials.keyring_backend_warning())


if __name__ == "__main__":
    unittest.main()
