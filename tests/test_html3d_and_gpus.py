"""The html3d lane (job, intent, lane pick with fallback to main) and the machine's GPU list (auto detection, 1 to 4 GPUs)."""
import asyncio
import unittest
from unittest import mock

from core import config, lanes, prompt_scope
from core.lanes import creative
from core.lanes.target import Target

GB = 2 ** 30


def dev(i, name="Intel(R) Arc(TM) A770 Graphics", total=16):
    return {"index": i, "name": name, "total_b": int(total * GB), "free_b": int(total * GB), "used_b": 0}


class ThreeDIntentTests(unittest.TestCase):
    def test_build_requests(self):
        for q in ("Make a 3D village with bangladeshi style", "make a three.js solar system page",
                  "create a webgl shader demo", "build a babylon.js racing game"):
            self.assertTrue(prompt_scope.three_d_wanted(q), q)

    def test_not_3d_web(self):
        for q in ("explain how webgl works", "3D printing guide, make a model", "make a dashboard", "write a blender script to make a cube"):
            self.assertFalse(prompt_scope.three_d_wanted(q), q)

    def test_error_followup_on_a_three_page(self):
        msgs = [{"role": "assistant", "content": "<script>const scene = new THREE.Scene()</script>"}]
        self.assertTrue(prompt_scope.three_d_wanted("Uncaught SyntaxError: missing ) after argument list index.html:694", [], msgs))
        self.assertFalse(prompt_scope.three_d_wanted("Uncaught SyntaxError: missing ) after argument list index.html:694", [], []))


class LanePickTests(unittest.TestCase):
    def run_pick(self, route, client=None, boom=False):
        class T(Target):
            async def client(self_inner):
                if boom:
                    raise RuntimeError("did not load")
                return client or object()
        routed = [T(n) for n in route]
        with mock.patch.object(creative, "targets", return_value=routed), \
                mock.patch.object(Target, "available", return_value=True):
            return asyncio.run(creative.pick("html3d", 1))

    def test_job_exists_local_only_default_main(self):
        j = lanes.JOBS["html3d"]
        self.assertEqual((j["kind"], j["default"], j.get("local_only")), ("chat", "main", True))

    def test_main_only_route_means_no_dedicated_lane(self):
        self.assertIsNone(self.run_pick(["main"]))

    def test_dedicated_lane_is_used(self):
        c = object()
        got = self.run_pick(["html3d", "main"], client=c)
        self.assertEqual((got.lane, got.client), ("html3d", c))
        self.assertGreaterEqual(got.max_tokens, 256)

    def test_lane_that_will_not_load_falls_back_to_main(self):
        self.assertIsNone(self.run_pick(["html3d", "main"], boom=True))


class GpuListTests(unittest.TestCase):
    def scan(self, devices, auto, configured):
        rt = {**config.ACTIVE_RUNTIME, "gpu_auto": auto, "gpu_devices": list(configured), "small_model_gpu": None}
        with mock.patch.dict(config.ACTIVE_RUNTIME, rt, clear=True), \
                mock.patch.dict(config.CONFIG_DEFAULTS, {"gpu_devices": list(configured)}), \
                mock.patch.dict(config.GPU_STATUS, {}), \
                mock.patch("core.vram.devices.query_devices", return_value=devices):
            status = config.resolve_gpu_devices(force=True)
            return status, list(config.ACTIVE_RUNTIME["gpu_devices"]), config.default_small_gpu()

    def test_auto_follows_the_machine(self):
        igpu = dev(0, "Intel(R) UHD Graphics 770", total=32)
        for n in (1, 2, 3, 4):
            gpus = [dev(i + 1) for i in range(n)]
            status, active, small = self.scan([igpu] + gpus, True, [1, 2])
            self.assertEqual(active, list(range(1, n + 1)), n)
            self.assertEqual(status["unused"], [])
            self.assertEqual(small, active[-1])

    def test_fixed_list_reports_missing_and_unused(self):
        status, active, _ = self.scan([dev(1), dev(2), dev(3)], False, [1, 4])
        self.assertEqual(active, [1, 4])               # a fixed list is never rewritten
        self.assertEqual((status["missing"], status["unused"]), ([4], [2, 3]))

    def test_unreadable_device_list_keeps_the_configured_list(self):
        from core.vram.devices import DeviceQueryError
        rt = {**config.ACTIVE_RUNTIME, "gpu_auto": True, "gpu_devices": [1, 2]}
        with mock.patch.dict(config.ACTIVE_RUNTIME, rt, clear=True), \
                mock.patch.dict(config.GPU_STATUS, {}), \
                mock.patch("core.vram.devices.query_devices", side_effect=DeviceQueryError("no llama-bench")):
            status = config.resolve_gpu_devices(force=True)
            self.assertFalse(status["checked"])
            self.assertEqual(config.ACTIVE_RUNTIME["gpu_devices"], [1, 2])


if __name__ == "__main__":
    unittest.main()
