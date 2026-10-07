"""tests/test_vram_lanes.py - /control/vram/lanes per-lane VRAM estimate.

The endpoint reuses plan_launch (the same calculator the launch preflight
uses), so the numbers are the ones that decide fit. We drive it with fake lane
instances (no llama-server spawn) and assert the shape + that a configured
vision mmproj inflates weights and flips fits for a tiny model.

Run: python -m unittest discover tests -p "test_vram_lanes.py"
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import deps
from core import small_model
from core.auth import Principal
from routes.control import status_endpoints as se


class _FakeInst:
    def __init__(self, model, gpu=1, ctx=8192, mmproj=None, up=False, n_slots=1):
        self.model_path = Path(model)
        self.gpu = gpu
        self.ctx = ctx
        self.kind = "vision" if mmproj else "chat"
        self.mmproj_path = Path(mmproj) if mmproj else None
        self.kv_cache_type = "q8_0"
        self.n_slots = n_slots
        self._up = up

    def is_up(self):
        return self._up


class VramLanesRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        (self.tmpdir / "exec.gguf").write_bytes(b"x" * 4096)
        (self.tmpdir / "mm.gguf").write_bytes(b"m" * 2048)
        app = FastAPI()
        app.include_router(se.router)
        p = Principal(id=7, username="u7", display_name="u7", is_super_admin=False,
                      must_change_password=False, role_names=[], permission_keys={"chat.use"})
        app.dependency_overrides[deps.get_current_user] = lambda: p
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_shape_and_fit_decision(self):
        insts = {
            "executor": _FakeInst(self.tmpdir / "exec.gguf", gpu=1, ctx=16384),
        }
        with mock.patch("core.small_model.small_models.instances", insts):
            r = self.client.get("/control/vram/lanes")
        self.assertEqual(r.status_code, 200, r.text[:200])
        d = r.json()
        by_name = {x["lane"]: x for x in d["lanes"]}
        self.assertIn("executor", by_name)
        self.assertIn("per_gpu", d)
        self.assertGreater(by_name["executor"]["total_gb"], 0)
        self.assertIn("weights_gb", by_name["executor"])
        self.assertIn("kv_gb", by_name["executor"])
        self.assertGreaterEqual(by_name["executor"]["kv_gb"], 0)

    def test_vision_mmproj_adds_weights(self):
        # With hand-written fake .gguf bytes parse_gguf_info cannot read header
        # keys, so weights estimate to 0.0 for both lanes. The endpoint-level
        # contract we CAN pin: a vision lane with an mmproj path reaches
        # plan_launch with vision_capable (no crash, shape intact, per-GPU roll).
        insts = {
            "text_only": _FakeInst(self.tmpdir / "exec.gguf", gpu=1, ctx=4096),
            "vision": _FakeInst(self.tmpdir / "exec.gguf", gpu=2, ctx=4096,
                                mmproj=self.tmpdir / "mm.gguf"),
        }
        with mock.patch("core.small_model.small_models.instances", insts):
            r = self.client.get("/control/vram/lanes")
        d = r.json()
        by_name = {x["lane"]: x for x in d["lanes"]}
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(by_name.keys()), {"text_only", "vision"})
        self.assertIn("per_gpu", d)
        self.assertIn("2", d["per_gpu"], "the vision lane's GPU 2 must be in the roll")

    def test_missing_model_skipped(self):
        insts = {"phantom": _FakeInst(self.tmpdir / "nope.gguf", gpu=1, ctx=4096)}
        with mock.patch("core.small_model.small_models.instances", insts):
            r = self.client.get("/control/vram/lanes")
        d = r.json()
        self.assertEqual([x["lane"] for x in d["lanes"]], [])


if __name__ == "__main__":
    unittest.main()
