"""tests/test_vram_moe.py - mixture-of-experts models are split by experts, never by layer.

An explicit n_gpu_layers is launched as written. With full offload, the preflight picks
--n-cpu-moe N from the free VRAM when the profile does not set it.

Run: python -m unittest tests.test_vram_moe -v
"""

import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import process
from core.vram import gguf_parser, moe
from core.vram import preflight as pf
from core.vram.constants import GB, MIB

N_LAYER = 4
EXPERT_B = 3 * MIB       # per layer
OTHER_B = 1 * MIB        # attention etc. per layer


def _s(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def write_moe_gguf(path: Path, n_layer: int = N_LAYER) -> Path:
    kv = [("general.architecture", 8, _s("qwen")),
          ("qwen.block_count", 4, struct.pack("<I", n_layer)),
          ("qwen.expert_count", 4, struct.pack("<I", 8))]
    tensors, off = [], 0
    for i in range(n_layer):
        for name, size in ((f"blk.{i}.ffn_gate_exps.weight", EXPERT_B),
                           (f"blk.{i}.attn_q.weight", OTHER_B)):
            tensors.append((name, off))
            off += size
    head = b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", len(tensors), len(kv))
    for key, vtype, val in kv:
        head += _s(key) + struct.pack("<I", vtype) + val
    for name, o in tensors:
        head += _s(name) + struct.pack("<I", 1) + struct.pack("<Q", 8) + struct.pack("<I", 0) + struct.pack("<Q", o)
    head += b"\0" * (-len(head) % 32)
    path.write_bytes(head + b"\0" * off)
    return path


class ExpertBytes(unittest.TestCase):
    def test_reads_the_expert_bytes_of_every_block(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_moe_gguf(Path(d) / "m.gguf")
            self.assertEqual(gguf_parser.expert_layer_bytes(p), {i: EXPERT_B for i in range(N_LAYER)})

    def test_an_unreadable_file_gives_nothing_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.gguf"
            p.write_bytes(b"not a gguf")
            self.assertEqual(gguf_parser.expert_layer_bytes(p), {})


class HybridAttentionKv(unittest.TestCase):
    def test_only_the_attention_layers_are_charged_kv(self):
        from core.vram.estimator import estimate_footprint
        info = {"n_layer": 40, "n_head_kv": 2, "head_dim": 256, "file_bytes": GB, "n_vocab": 1000,
                "complete": True}
        full = estimate_footprint(info, 131072, "q8_0")
        hybrid = estimate_footprint(dict(info, n_kv_layers=10), 131072, "q8_0")
        self.assertEqual(hybrid["kv_total_b"] * 4, full["kv_total_b"])


class Resolve(unittest.TestCase):
    INFO = {"n_layer": 40, "n_expert": 128}

    def test_dense_models_are_untouched(self):
        self.assertEqual(moe.resolve_offload({}, {"n_layer": 40, "n_expert": 0}, 30, None, None), (30, 0))

    def test_an_explicit_layer_count_is_launched_as_written(self):
        self.assertEqual(moe.resolve_offload({}, self.INFO, 30, None, None), (30, 0))
        self.assertEqual(moe.resolve_offload({}, self.INFO, 30, None, 7), (30, 0))

    def test_full_offload_with_no_setting_stays_full(self):
        self.assertEqual(moe.resolve_offload({}, self.INFO, 999, None, None), (999, 0))

    def test_an_explicit_value_wins_over_everything(self):
        self.assertEqual(moe.resolve_offload({}, self.INFO, 38, 12, 5), (38, 12))

    def test_the_value_chosen_by_the_preflight_is_used(self):
        self.assertEqual(moe.resolve_offload({}, self.INFO, 999, None, 7), (999, 7))

    def test_smallest_fitting_value(self):
        self.assertEqual(moe.smallest_fitting(40, lambda n: n >= 13), 13)
        self.assertEqual(moe.smallest_fitting(40, lambda n: True), 0)
        self.assertIsNone(moe.smallest_fitting(40, lambda n: False))


class Preflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._cfg = pf._preflight_cfg
        pf._preflight_cfg = lambda: {"mode": "block", "headroom_gb": 1.0}
        self.model = write_moe_gguf(self.dir / "m.gguf")
        # a 20 GB, 40-layer MoE whose layers are 90% experts; expert bytes come from the table
        self.info = {"arch": "qwen", "n_layer": 40, "n_head": 16, "n_head_kv": 2, "n_embd": 2048,
                     "head_dim": 128, "ctx_train": 262144, "sliding_window": None, "n_expert": 128,
                     "n_expert_used": 8, "n_vocab": 150000, "file_bytes": 20 * GB,
                     "complete": True, "model_path": str(self.model), "error": None}
        self.per_layer = {i: int(0.45 * GB) for i in range(40)}

    def tearDown(self):
        pf._preflight_cfg = self._cfg
        self.tmp.cleanup()

    def _plan(self, free_gb, **profile):
        prof = {"model_path": str(self.model), "context_size": 4096, "n_gpu_layers": 999,
                "gpu_devices": [0], "tensor_split": "1", "kv_cache_type": "q8_0",
                "flash_attn": "on", "ubatch_size": 512, "llama_bin_dir": str(self.dir)}
        prof.update(profile)
        dev = {"index": 0, "name": "GPU", "total_b": 16 * GB, "free_b": int(free_gb * GB), "used_b": 0}
        with mock.patch.object(pf, "parse_gguf_info", return_value=dict(self.info)), \
             mock.patch.object(pf, "query_devices", return_value=[dev]), \
             mock.patch.object(moe, "expert_layer_bytes", return_value=self.per_layer):
            return prof, pf.plan_launch(prof)

    def test_a_model_that_does_not_fit_gets_the_smallest_expert_offload(self):
        _, plan = self._plan(free_gb=14)
        self.assertTrue(plan["allow"], plan["message"])
        self.assertIn(plan["status"], ("fit", "tight"))
        n = plan["auto_n_cpu_moe"]
        self.assertGreater(n, 0)
        self.assertEqual(plan["effective"]["n_cpu_moe"], n)
        self.assertEqual(plan["effective"]["n_gpu_layers"], 999)
        self.assertIn("--n-cpu-moe", plan["message"])

    def test_a_model_that_fits_offloads_nothing(self):
        _, plan = self._plan(free_gb=30)
        self.assertEqual(plan["status"], "fit")
        self.assertNotIn("auto_n_cpu_moe", plan)
        self.assertEqual(plan["effective"]["n_cpu_moe"], 0)

    def test_an_explicit_setting_is_respected_not_second_guessed(self):
        _, plan = self._plan(free_gb=30, tuned={"n_cpu_moe": 10})
        self.assertEqual(plan["effective"]["n_cpu_moe"], 10)
        self.assertNotIn("auto_n_cpu_moe", plan)

    def test_the_value_that_was_checked_is_the_value_that_is_launched(self):
        prof, _ = self._plan(free_gb=14)
        with mock.patch.object(pf, "parse_gguf_info", return_value=dict(self.info)), \
             mock.patch.object(pf, "query_devices", return_value=[
                 {"index": 0, "name": "GPU", "total_b": 16 * GB, "free_b": 14 * GB, "used_b": 0}]), \
             mock.patch.object(moe, "expert_layer_bytes", return_value=self.per_layer):
            plan = pf.check_or_raise(prof)
        self.assertEqual(prof["_auto_n_cpu_moe"], plan["auto_n_cpu_moe"])
        with mock.patch.object(process, "find_llama_server", return_value=Path("llama-server")), \
             mock.patch.object(process, "apply_runtime", side_effect=lambda p: p), \
             mock.patch("core.vram.gguf_parser.parse_gguf_info", return_value=dict(self.info)):
            cmd = process.build_launch_command(prof)
        self.assertEqual(cmd[cmd.index("-ngl") + 1], "999")
        self.assertEqual(cmd[cmd.index("-ncmoe") + 1], str(plan["auto_n_cpu_moe"]))

    def test_ngl_30_is_launched_as_ngl_30(self):
        prof = {"model_path": str(self.model), "n_gpu_layers": 30, "gpu_devices": [1, 2],
                "tensor_split": "9,11", "context_size": 4096}
        with mock.patch.object(process, "find_llama_server", return_value=Path("llama-server")),              mock.patch.object(process, "apply_runtime", side_effect=lambda p: p),              mock.patch("core.vram.gguf_parser.parse_gguf_info", return_value=dict(self.info)):
            cmd = process.build_launch_command(prof)
        self.assertEqual(cmd[cmd.index("-ngl") + 1], "30")
        self.assertNotIn("-ncmoe", cmd)
        self.assertIn("--no-op-offload", cmd, "experts in RAM must not be op-offloaded (Vulkan crash)")

    def test_a_fully_offloaded_model_keeps_op_offload_and_the_setting_can_force_it(self):
        def cmd_for(**over):
            prof = {"model_path": str(self.model), "n_gpu_layers": 999, "gpu_devices": [1, 2],
                    "tensor_split": "1,1", "context_size": 4096, **over}
            with mock.patch.object(process, "find_llama_server", return_value=Path("llama-server")),                  mock.patch.object(process, "apply_runtime", side_effect=lambda p: p),                  mock.patch("core.vram.gguf_parser.parse_gguf_info", return_value=dict(self.info)):
                return process.build_launch_command(prof)
        self.assertNotIn("--no-op-offload", cmd_for())
        self.assertIn("--no-op-offload", cmd_for(op_offload=False))
        self.assertNotIn("--no-op-offload", cmd_for(n_gpu_layers=30, op_offload=True))


if __name__ == "__main__":
    unittest.main()
