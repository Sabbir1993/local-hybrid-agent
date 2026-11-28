"""tests/test_vram_hybrid.py - honest preflight for hybrid (recurrent + attention) models and MTP drafts.

A qwen35 model keeps KV only in every 4th layer; the other layers hold a fixed recurrent state per
sequence (slot), which the estimate used to leave out entirely. An MTP draft was charged only its file
size, not its own KV cache over the full context. Context checkpoints (copies of a slot's recurrent
state) live in host RAM and were not counted either.

Numbers are Qwen3.8-27B's: 65 blocks (64 + 1 nextn), full_attention_interval 4 -> 16 KV layers,
n_head_kv 4, head_dim 256; ssm state_size 128, group_count 16, time_step_rank 48, inner_size 6144,
conv_kernel 4 -> 48 Gated-DeltaNet layers.

Run: python -m unittest tests.test_vram_hybrid -v
"""

import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.vram import preflight as pf
from core.vram.constants import GB, MIB
from core.vram.estimator import estimate_draft, estimate_footprint, recurrent_state_bytes
from core.vram.gguf_parser import parse_gguf_info

RECURRENT = {"state_size": 128, "group_count": 16, "time_step_rank": 48,
             "inner_size": 6144, "conv_kernel": 4}
QWEN35 = {"arch": "qwen35", "n_layer": 65, "n_head": 24, "n_head_kv": 4, "n_embd": 5120,
          "head_dim": 256, "ctx_train": 262144, "sliding_window": None, "n_expert": 0,
          "n_expert_used": 0, "n_kv_layers": 16, "n_nextn_layers": 1, "recurrent": dict(RECURRENT),
          "n_vocab": 248320, "file_bytes": 16 * GB, "complete": True, "stat_ok": True,
          "model_path": "q.gguf", "error": None}
# the MTP draft file carries the same header; only its nextn layer runs
MTP = dict(QWEN35, file_bytes=int(1.3 * GB), model_path="mtp.gguf")
LLAMA = {"arch": "llama", "n_layer": 32, "n_head": 32, "n_head_kv": 8, "n_embd": 4096,
         "head_dim": 128, "ctx_train": 8192, "sliding_window": None, "n_expert": 0,
         "n_expert_used": 0, "n_kv_layers": 32, "n_vocab": 128256, "file_bytes": 8 * GB,
         "complete": True, "stat_ok": True, "model_path": "l.gguf", "error": None}

# per linear layer: f32 state 48 x 128 x 128, conv 3 x (6144 + 2 x 16 x 128)
PER_LAYER = (48 * 128 * 128 + 3 * (6144 + 2 * 16 * 128)) * 4
PER_SLOT = 48 * PER_LAYER


def _gguf(path: Path, arch: str, kv: dict) -> None:
    """Minimal GGUF v3 header: no tensors, general.architecture + uint32 {arch}.* keys."""
    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    body = s("general.architecture") + struct.pack("<I", 8) + s(arch)
    for k, v in kv.items():
        body += s(f"{arch}.{k}") + struct.pack("<I", 4) + struct.pack("<I", v)
    path.write_bytes(b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0)
                     + struct.pack("<Q", 1 + len(kv)) + body)


class ParserReadsTheRecurrentShape(unittest.TestCase):
    def test_qwen35_header(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.gguf"
            _gguf(p, "qwen35", {"block_count": 65, "attention.head_count": 24,
                                "attention.head_count_kv": 4, "embedding_length": 5120,
                                "attention.key_length": 256, "full_attention_interval": 4,
                                "nextn_predict_layers": 1, **{f"ssm.{k}": v for k, v in RECURRENT.items()}})
            info = parse_gguf_info(p)
        self.assertEqual(info["recurrent"], RECURRENT)
        self.assertEqual(info["n_nextn_layers"], 1)
        self.assertEqual(info["n_kv_layers"], 16)
        self.assertEqual(recurrent_state_bytes(info), PER_SLOT)

    def test_a_plain_model_has_no_recurrent_state(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "l.gguf"
            _gguf(p, "llama", {"block_count": 32, "attention.head_count": 32,
                               "attention.head_count_kv": 8, "embedding_length": 4096})
            info = parse_gguf_info(p)
        self.assertIsNone(info["recurrent"])
        self.assertEqual(info["n_nextn_layers"], 0)
        self.assertEqual(recurrent_state_bytes(info), 0)


class RecurrentStateIsCharged(unittest.TestCase):
    def test_about_150_mib_per_slot(self):
        b = recurrent_state_bytes(QWEN35)
        self.assertEqual(b, PER_SLOT)
        self.assertAlmostEqual(b / MIB, 149.6, delta=0.1)

    def test_times_the_number_of_slots_at_least_one(self):
        fp = estimate_footprint(QWEN35, 131072, "q8_0", 512, "on", n_seqs=15)
        self.assertEqual(fp["recurrent_b"], 15 * PER_SLOT)
        self.assertEqual(estimate_footprint(QWEN35, 131072, "q8_0", 512, "on", n_seqs=0)["recurrent_b"], PER_SLOT)

    def test_kv_still_only_on_the_16_attention_layers(self):
        fp = estimate_footprint(QWEN35, 1000, "f16", 512, "on")
        self.assertEqual(fp["kv_per_token_b"], 16 * 4 * 256 * 2 * 2)

    def test_non_hybrid_unchanged(self):
        before = estimate_footprint(LLAMA, 8192, "f16", 512, "on")
        after = estimate_footprint(LLAMA, 8192, "f16", 512, "on", n_seqs=15)
        self.assertEqual(after["recurrent_b"], 0)
        for k in ("weights_b", "kv_total_b", "compute_b", "headroom_b"):
            self.assertEqual(before[k], after[k], k)


class MtpDraft(unittest.TestCase):
    def test_draft_kv_is_its_attention_layers_over_the_context(self):
        d = estimate_draft(MTP, 131072, 512)
        self.assertEqual(d["layers"], 1)
        self.assertEqual(d["kv_b"], 1 * 4 * 256 * 2 * 2 * 131072)   # f16
        self.assertGreater(d["compute_b"], 0)

    def test_no_draft_costs_nothing(self):
        self.assertEqual(estimate_draft(None, 131072)["kv_b"], 0)


class _PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.model = self.dir / "q.gguf"
        self.draft = self.dir / "mtp.gguf"
        for f in (self.model, self.draft):
            f.write_bytes(b"GGUF" + b"\0" * 64)
        self.cfg = mock.patch.object(pf, "_preflight_cfg", return_value={
            "mode": "block", "headroom_gb": 1.0, "host_headroom_gb": 4.0})
        self.cfg.start()

    def tearDown(self):
        self.cfg.stop()
        self.tmp.cleanup()

    def plan(self, info, available_gb=200, **profile):
        prof = {"model_path": str(self.model), "context_size": 1_000_000, "n_slots": 15,
                "kv_unified": True, "n_gpu_layers": 999, "gpu_devices": [0, 1], "tensor_split": "1,1",
                "kv_cache_type": "q8_0", "flash_attn": "on", "ubatch_size": 512, "cache_ram": 4096,
                "llama_bin_dir": self.tmp.name, "backend": "sycl"}
        prof.update(profile)
        devs = [{"index": i, "id": f"SYCL{i}", "backend": "sycl", "name": "B70",
                 "total_b": 32 * GB, "free_b": 31 * GB, "used_b": GB} for i in (0, 1)]
        snap = {"total_b": 256 * GB, "available_b": int(available_gb * GB),
                "swap_total_b": 0, "swap_used_b": 0}
        by_path = {str(self.model): dict(info), str(self.draft): dict(MTP)}
        with mock.patch.object(pf, "parse_gguf_info", side_effect=lambda p: by_path[str(p)]), \
             mock.patch.object(pf, "host_memory_snapshot", return_value=snap):
            return pf.plan_launch(prof, devices=devs)


class PlanBreakdown(_PlanCase):
    def test_recurrent_line_and_per_card_split(self):
        p = self.plan(QWEN35)
        est = p["estimate"]
        self.assertAlmostEqual(est["recurrent_gb"], round(15 * PER_SLOT / GB, 2))
        self.assertEqual(est["n_slots"], 15)
        self.assertAlmostEqual(est["recurrent_per_slot_mib"], 149.6, delta=0.1)
        self.assertIn("recurrent state", p["message"])
        cards = [d for d in p["devices"] if d["target"]]
        self.assertEqual(len(cards), 2)
        for c in cards:   # split 1,1: each card holds half, as KV does
            self.assertAlmostEqual(c["breakdown"]["recurrent_gb"], 15 * PER_SLOT / GB / 2, delta=0.01)
            self.assertAlmostEqual(c["breakdown"]["kv_gb"], est["kv_gb"] / 2, delta=0.01)
        # the total now includes the recurrent state
        self.assertAlmostEqual(est["total_gb"], est["weights_gb"] + est["kv_gb"] + est["compute_gb"]
                               + est["recurrent_gb"], delta=0.02)

    def test_mtp_off_and_on(self):
        off = self.plan(QWEN35)
        self.assertEqual(off["estimate"]["mtp_kv_gb"], 0)
        self.assertEqual(off["estimate"]["mtp_compute_gb"], 0)
        on = self.plan(QWEN35, mtp_enabled=True, mtp_draft_path=str(self.draft))
        mtp_kv = 1 * 4 * 256 * 2 * 2 * 1_000_000
        self.assertAlmostEqual(on["estimate"]["mtp_kv_gb"], round(mtp_kv / GB, 2))
        self.assertGreater(on["estimate"]["mtp_compute_gb"], 0)
        self.assertIn("MTP draft KV", on["message"])
        # more than the draft file: its KV and compute are charged on top
        self.assertGreater(on["estimate"]["total_gb"] - off["estimate"]["total_gb"], mtp_kv / GB)
        for c in (d for d in on["devices"] if d["target"]):
            self.assertAlmostEqual(c["breakdown"]["mtp_kv_gb"], mtp_kv / GB / 2, delta=0.01)
        # a missing draft file is not launched, so not charged
        gone = self.plan(QWEN35, mtp_enabled=True, mtp_draft_path=str(self.dir / "nope.gguf"))
        self.assertEqual(gone["estimate"]["mtp_kv_gb"], 0)

    def test_non_hybrid_plan_unchanged(self):
        p = self.plan(LLAMA, context_size=8192)
        est = p["estimate"]
        self.assertEqual(est["recurrent_gb"], 0)
        self.assertEqual(est["mtp_kv_gb"], 0)
        self.assertAlmostEqual(est["total_gb"], est["weights_gb"] + est["kv_gb"] + est["compute_gb"], delta=0.02)
        self.assertNotIn("recurrent", p["message"])
        self.assertFalse(p["host"]["checked"])      # full offload, no checkpoints: no RAM gate
        self.assertEqual(p["host"]["checkpoints_gb"], 0)


class CheckpointsInHostRam(_PlanCase):
    def test_explicit_checkpoints(self):
        p = self.plan(QWEN35, ctx_checkpoints=8)
        h = p["host"]
        self.assertTrue(h["checked"])
        self.assertEqual(h["ctx_checkpoints"], 8)
        self.assertAlmostEqual(h["checkpoints_gb"], round(8 * 15 * PER_SLOT / GB, 2))
        self.assertAlmostEqual(h["required_gb"], h["checkpoints_gb"] + h["cache_gb"] + h["reserve_gb"], delta=0.02)
        self.assertIn("context checkpoints", p["message"])

    def test_llama_default_32_when_the_launcher_passes_none(self):
        with mock.patch("core.process.ctx_checkpoints_for", return_value=0):
            h = self.plan(QWEN35)["host"]
        self.assertEqual(h["ctx_checkpoints"], 32)
        self.assertAlmostEqual(h["checkpoints_gb"], round(32 * 15 * PER_SLOT / GB, 2))

    def test_checkpoints_that_do_not_fit_refuse_even_with_full_offload(self):
        p = self.plan(QWEN35, available_gb=40, ctx_checkpoints=32)   # ~70 GB of checkpoints
        self.assertEqual(p["status"], "nofit")
        self.assertFalse(p["allow"])
        self.assertFalse(p["host"]["fit"])
        self.assertFalse(p["host"]["partial_offload"])


if __name__ == "__main__":
    unittest.main()
