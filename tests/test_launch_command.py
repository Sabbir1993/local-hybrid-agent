"""tests/test_launch_command.py - exact llama-server argv for known profiles.

Run: python -m unittest tests.test_launch_command -v

core/process.py:build_launch_command() assembles every flag the server launches with, and
the only existing coverage (test_ram_caps.py) touches it for one flag. A wrong argv here
fails at launch on real hardware - or worse, launches with a silently wrong split - so the
known-good shapes are pinned exactly.

Two deliberate scopings, both documented where they bite:
- apply_runtime() is neutralized: it stamps machine-specific ACTIVE_RUNTIME values
  (E:\\AI\\... paths, this box's device list) onto the profile, which cannot be asserted
  hermetically. The argv assembly below it is what these tests own.
- CONFIG_DEFAULTS["tensor_split"] is the literal "9,11" from the author's 2x-A770 box and
  flows into every profile that does not set one. That is pinned, not changed: making the
  default dynamic is a product decision with launch-time blast radius, not a test fix.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import process
from core.config import CONFIG_DEFAULTS


def _cmd(**profile):
    base = {"model_path": "C:/m.gguf", "llama_bin_dir": "C:/llama", "port": 8090}
    with mock.patch("core.process.find_llama_server", lambda d: Path("C:/llama/llama-server.exe")), \
            mock.patch("core.process.apply_runtime", lambda p: None):
        return process.build_launch_command({**base, **profile})


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


class DualGpuVulkanDefaults(unittest.TestCase):
    """Minimal profile: every default in CONFIG_DEFAULTS must appear, exactly once."""

    def test_full_argv_shape(self):
        cmd = _cmd()
        # Path normalizes separators per-OS, so compare the name, not the string
        self.assertEqual(Path(cmd[0]).name, "llama-server.exe")
        self.assertEqual(_flag(cmd, "-m"), "C:/m.gguf")
        self.assertEqual(_flag(cmd, "-c"), str(CONFIG_DEFAULTS["context_size"]))
        self.assertEqual(_flag(cmd, "-ngl"), str(CONFIG_DEFAULTS["n_gpu_layers"]))
        self.assertEqual(_flag(cmd, "-dev"), "Vulkan1,Vulkan2")
        self.assertEqual(_flag(cmd, "-fa"), CONFIG_DEFAULTS["flash_attn"])
        self.assertEqual(_flag(cmd, "--port"), "8090")
        self.assertEqual(_flag(cmd, "--host"), "127.0.0.1")
        self.assertEqual(_flag(cmd, "-sm"), CONFIG_DEFAULTS["split_mode"])
        self.assertEqual(_flag(cmd, "--tensor-split"),
                         CONFIG_DEFAULTS["tensor_split"])
        self.assertEqual(_flag(cmd, "-cram"), str(process.MAIN_CACHE_RAM_MB))
        self.assertIn("--jinja", cmd)

    def test_split_flags_come_as_a_pair(self):
        cmd = _cmd()
        self.assertIn("-sm", cmd)
        self.assertIn("--tensor-split", cmd)
        # values sit in fixed positions relative to each other
        self.assertEqual(cmd[cmd.index("-sm") + 1], CONFIG_DEFAULTS["split_mode"])
        self.assertEqual(cmd[cmd.index("--tensor-split") + 1],
                         CONFIG_DEFAULTS["tensor_split"])


class BackendNaming(unittest.TestCase):
    def test_cuda_dev_names(self):
        cmd = _cmd(backend="cuda", gpu_devices=[0], tensor_split="1")
        self.assertEqual(_flag(cmd, "-dev"), "CUDA0")

    def test_cuda_multi(self):
        cmd = _cmd(backend="cuda", gpu_devices=[0, 1], tensor_split="10,10")
        self.assertEqual(_flag(cmd, "-dev"), "CUDA0,CUDA1")
        self.assertEqual(_flag(cmd, "--tensor-split"), "10,10")

    def test_unknown_backend_falls_back_to_vulkan_names(self):
        cmd = _cmd(backend="sycl", gpu_devices=[0])
        self.assertEqual(_flag(cmd, "-dev"), "Vulkan0")


class SingleGpu(unittest.TestCase):
    def test_split_flags_omitted(self):
        cmd = _cmd(gpu_devices=[0], tensor_split="1")
        self.assertNotIn("-sm", cmd)
        self.assertNotIn("--tensor-split", cmd)

    def test_dev_is_still_pinned_explicitly(self):
        # README:188 says no -dev is emitted for one GPU; the code emits it and the code is
        # right (an explicit device beats an implicit one). Pinned so the two cannot drift
        # apart silently again - if this is ever changed intentionally, fix the README line too.
        cmd = _cmd(gpu_devices=[0], tensor_split="1")
        self.assertEqual(_flag(cmd, "-dev"), "Vulkan0")


class ZeroShareFiltering(unittest.TestCase):
    def test_zero_share_disables_that_gpu(self):
        cmd = _cmd(gpu_devices=[0, 1], tensor_split="1,0")
        self.assertEqual(_flag(cmd, "-dev"), "Vulkan0")
        self.assertNotIn("-sm", cmd)
        self.assertNotIn("--tensor-split", cmd)

    def test_reversed(self):
        cmd = _cmd(gpu_devices=[0, 1], tensor_split="0,1")
        self.assertEqual(_flag(cmd, "-dev"), "Vulkan1")

    def test_trailing_comma_does_not_crash(self):
        # "9," with 2 devices: the device filter one line up already dropped the empty
        # segment, but the split rebuild called int("") and raised ValueError. Now behaves
        # as the "9" the device list already computed.
        cmd = _cmd(gpu_devices=[0, 1], tensor_split="9,")
        self.assertEqual(_flag(cmd, "-dev"), "Vulkan0")
        self.assertNotIn("--tensor-split", cmd)

    def test_all_zero_shares_leave_no_devices(self):
        # degenerate but must not crash: every device filtered, single-device path taken
        cmd = _cmd(gpu_devices=[0, 1], tensor_split="0,0")
        self.assertNotIn("--tensor-split", cmd)


class OptionalFlags(unittest.TestCase):
    def test_kv_cache_type_pair(self):
        cmd = _cmd(kv_cache_type="q8_0")
        self.assertEqual(_flag(cmd, "-ctk"), "q8_0")
        self.assertEqual(_flag(cmd, "-ctv"), "q8_0")

    def test_kv_cache_type_absent_when_unset(self):
        self.assertNotIn("-ctk", _cmd())
        self.assertNotIn("-ctv", _cmd())

    def test_thread_and_batch_flags(self):
        cmd = _cmd(threads=8, threads_batch=8, batch_size=1024, ubatch_size=256, n_slots=2)
        for flag, val in (("-t", "8"), ("-tb", "8"), ("-b", "1024"),
                          ("-ub", "256"), ("-np", "2")):
            self.assertEqual(_flag(cmd, flag), val)

    def test_unset_counts_are_omitted(self):
        cmd = _cmd()
        for flag in ("-t", "-tb", "-b", "-ub", "-np"):
            self.assertNotIn(flag, cmd)

    def test_unified_kv_pool(self):
        cmd = _cmd(kv_unified=True, n_slots=2, context_size=32768)
        self.assertIn("-kvu", cmd)

    def test_no_unified_pool_by_default(self):
        self.assertNotIn("-kvu", _cmd())

    def test_per_slot_cap_only_with_unified_pool(self):
        # per_slot_cap returns 0 without kv_unified (mirrors the inert
        # config/model_configs.json combination the audit found)
        with_cap = _cmd(kv_unified=True, n_slots=2, context_size=32768,
                        kv_unified_per_slot=8192)
        self.assertEqual(_flag(with_cap, "--kv-unified-per-slot"), "8192")
        without_pool = _cmd(kv_unified_per_slot=8192)
        self.assertNotIn("--kv-unified-per-slot", without_pool)

    def test_cache_reuse(self):
        self.assertEqual(_flag(_cmd(cache_reuse=256), "--cache-reuse"), "256")
        self.assertNotIn("--cache-reuse", _cmd())

    def test_moe_offload_only_for_moe_with_tuning(self):
        cmd = _cmd(model_type="moe", tuned={"n_cpu_moe": 8})
        self.assertEqual(_flag(cmd, "-ncmoe"), "8")
        self.assertNotIn("-ncmoe", _cmd(model_type="dense", tuned={"n_cpu_moe": 8}))
        self.assertNotIn("-ncmoe", _cmd(model_type="moe"))
        self.assertNotIn("-ncmoe", _cmd())

    def test_jinja_always(self):
        self.assertIn("--jinja", _cmd())
        self.assertIn("--jinja", _cmd(backend="cuda", gpu_devices=[0]))

    def test_server_extra_args_appended(self):
        cmd = _cmd(server_extra_args=["--temp", "0.7"])
        self.assertEqual(cmd[-2:], ["--temp", "0.7"])


class TunedOverridesProfile(unittest.TestCase):
    def test_tuned_wins_for_split_and_layers(self):
        cmd = _cmd(tensor_split="5,15", n_gpu_layers=10,
                   tuned={"tensor_split": "10,10", "n_gpu_layers": 99})
        self.assertEqual(_flag(cmd, "--tensor-split"), "10,10")
        self.assertEqual(_flag(cmd, "-ngl"), "99")


if __name__ == "__main__":
    unittest.main()
