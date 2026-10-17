"""tools/runner/verify.py: the one-command isolation check. The logic (table, exit code, a crashing check = a failed check) is tested
here with a fake runner; the real hostile programs only run where bubblewrap works (Linux) - that run is the proof, not these tests."""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runner_verify", ROOT / "tools" / "runner" / "verify.py")
V = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V)


class LogicTests(unittest.TestCase):
    def test_a_crashing_check_is_a_failed_check(self):
        def boom(rs):
            raise RuntimeError("x")
        res = V.run_checks(object(), [("a", True, "w", lambda rs: (True, "fine")), ("b", True, "w", boom)])
        self.assertEqual([r["ok"] for r in res], [True, False])
        self.assertIn("RuntimeError", res[1]["detail"])
        self.assertEqual(V.exit_code(res), 1)

    def test_only_critical_failures_change_the_exit_code(self):
        res = V.run_checks(object(), [("a", True, "w", lambda rs: (True, "")), ("lib", False, "w", lambda rs: (False, "missing"))])
        self.assertEqual(V.exit_code(res), 0)
        text = V.render(res)
        self.assertIn("PASS  a", text)
        self.assertIn("WARN  lib", text)
        self.assertIn("every critical check passed", text)

    def test_a_failure_says_do_not_enable(self):
        text = V.render(V.run_checks(object(), [("net", True, "w", lambda rs: (False, "NET_OPEN"))]))
        self.assertIn("FAIL  net", text)
        self.assertIn("NET_OPEN", text)
        self.assertIn("do not enable", text)

    def test_the_check_list_covers_the_known_escape_routes(self):
        names = {c[0] for c in V.build_checks(mock.MagicMock())}
        for need in ("no_network", "no_secret_env", "host_files_hidden", "system_read_only", "symlink_not_returned",
                     "fork_bomb_contained", "endless_loop_killed", "memory_bomb_stopped", "oversize_file_stopped",
                     "output_file_count_capped", "job_folder_wiped", "concurrency_capped"):
            self.assertIn(need, names)

    def test_every_hostile_program_is_valid_python(self):
        for name in ("LOOP", "MEMORY", "BIG_FILE", "MANY_FILES", "SLEEP", "ENV_CANARY", "SYMLINK", "READ_ONLY", "FORK_BOMB"):
            compile(getattr(V, name), name, "exec")
        compile(V.HOST_FILES % "/var/lib/a770-runner", "HOST_FILES", "exec")

    def test_it_refuses_politely_off_linux_and_without_bubblewrap(self):
        with mock.patch.object(V.sys, "platform", "win32"):
            self.assertEqual(V.main(), 2)
        fake = mock.MagicMock()
        fake.detect_sandbox.return_value = "none"
        fake.STATE = {}
        with mock.patch.object(V.sys, "platform", "linux"), mock.patch.object(V, "load_runner", return_value=fake):
            self.assertEqual(V.main(), 2)

    def test_the_script_is_stdlib_only(self):
        import ast
        tree = ast.parse((ROOT / "tools" / "runner" / "verify.py").read_text(encoding="utf-8"))
        names = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertTrue(names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names))


if __name__ == "__main__":
    unittest.main()
