"""tests/test_ram_caps.py - host-RAM bounds for llama-server lanes, tracked file baselines, /control/memory.

llama-server keeps saved prompt states in system RAM (-cram defaults to 8192 MiB, -ctxcp to 32 per slot); a 3 GB
executor model was seen at 7 GB resident after a long agent session.

Run: python -m unittest tests.test_ram_caps -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import process
from core.agent_tools import diffs
from core.small_model.instance import SmallModelInstance


def _cmd(cfg):
    inst = SmallModelInstance("executor", {"model": "x.gguf", "port": 8091, "gpu": 1, "ctx": 8192, **cfg})
    with mock.patch("core.small_model.instance.find_llama_server", lambda d: Path("C:/llama/llama-server.exe")):
        return inst._launch_cmd()[0]


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


class SmallLaneCaps(unittest.TestCase):
    def test_executor_default_is_bounded(self):
        cmd = _cmd({})
        self.assertEqual(_flag(cmd, "-cram"), "1024")
        self.assertEqual(_flag(cmd, "-ctxcp"), "8")

    def test_lane_override(self):
        cmd = _cmd({"cache_ram": 256, "ctx_checkpoints": 2})
        self.assertEqual(_flag(cmd, "-cram"), "256")
        self.assertEqual(_flag(cmd, "-ctxcp"), "2")

    def test_zero_disables_the_cache_instead_of_meaning_llamas_default(self):
        self.assertEqual(_flag(_cmd({"cache_ram": 0}), "-cram"), "0")

    def test_zero_checkpoints_is_passed_not_left_to_llamas_default(self):
        self.assertEqual(_flag(_cmd({"ctx_checkpoints": 0}), "-ctxcp"), "0")

    def test_embedder_has_no_prompt_cache(self):
        cmd = _cmd({"kind": "embed"})
        self.assertEqual(_flag(cmd, "-cram"), "0")
        self.assertIn("--embedding", cmd)


class MainProfileCap(unittest.TestCase):
    def _main(self, **profile):
        base = {"model_path": "C:/m.gguf", "llama_bin_dir": "C:/llama", "port": 8090}
        with mock.patch("core.process.find_llama_server", lambda d: Path("C:/llama/llama-server.exe")), \
                mock.patch("core.process.apply_runtime", lambda p: None):
            return process.build_launch_command({**base, **profile})

    def test_unset_and_zero_no_longer_mean_8gib(self):
        for prof in ({}, {"cache_ram": 0}):
            self.assertEqual(_flag(self._main(**prof), "-cram"), str(process.MAIN_CACHE_RAM_MB), prof)

    def test_explicit_value_wins(self):
        self.assertEqual(_flag(self._main(cache_ram=1024), "-cram"), "1024")


class TrackedFiles(unittest.TestCase):
    def test_baselines_are_bounded_oldest_first(self):
        changes = {f"f{i}": {"before": "x"} for i in range(diffs.MAX_TRACKED_FILES + 5)}
        diffs._trim_changes(changes)
        self.assertEqual(len(changes), diffs.MAX_TRACKED_FILES)
        self.assertNotIn("f0", changes)
        self.assertIn(f"f{diffs.MAX_TRACKED_FILES + 4}", changes)


class MemoryReport(unittest.TestCase):
    def test_reports_this_process_with_numbers(self):
        from routes.control.status_endpoints import _memory_report
        rep = _memory_report()
        if "error" in rep:
            self.skipTest(rep["error"])
        self.assertGreater(rep["total_mb"], 0)
        me = [p for p in rep["processes"] if p["self"]]
        self.assertEqual(len(me), 1)
        self.assertGreater(me[0]["rss_mb"], 0)


if __name__ == "__main__":
    unittest.main()
