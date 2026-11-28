"""tests/test_main_instances.py - multi-instance main model: config model, validation, ports, config view.

Run: python -m unittest tests.test_main_instances -v

Single mode is the default and must not change, so most cases pin the multi-mode rules and the
single-mode shape that instance_specs() hands the launcher today.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import config
from core import main_instances as mi
from core.lanes import local_storage
from routes.control import config_helpers as ch

RUNTIME = {"gpu_devices": [0, 1, 2], "small_model_gpu": 2}


def _inst(gpus, n=4, ts=None):
    item = {"gpus": gpus, "n_slots": n}
    if ts is not None:
        item["tensor_split"] = ts
    return item


class InstancePort(unittest.TestCase):
    def test_index_zero_keeps_main_port_and_others_follow_block(self):
        self.assertEqual(mi.instance_port(0), config.LLAMA_SERVER_PORT)
        self.assertEqual(mi.instance_port(0), 8090)
        self.assertEqual([mi.instance_port(i) for i in range(1, 8)], list(range(8191, 8198)))


class MainMode(unittest.TestCase):
    def _p(self, **kw):
        return {"engine": "llama", "tuned": {}, **kw}

    def test_default_is_single(self):
        self.assertEqual(mi.main_mode(self._p()), "single")

    def test_multi_with_instances(self):
        p = self._p(tuned={"main_mode": "multi", "main_instances": [_inst([0])]})
        self.assertEqual(mi.main_mode(p), "multi")

    def test_top_level_fallback(self):
        p = self._p(main_mode="multi", main_instances=[_inst([0])])
        self.assertEqual(mi.main_mode(p), "multi")

    def test_vllm_forces_single(self):
        p = self._p(engine="vllm", tuned={"main_mode": "multi", "main_instances": [_inst([0])]})
        self.assertEqual(mi.main_mode(p), "single")

    def test_empty_list_is_single(self):
        p = self._p(tuned={"main_mode": "multi", "main_instances": []})
        self.assertEqual(mi.main_mode(p), "single")


class ValidateInstances(unittest.TestCase):
    def test_ok_case_sorts_and_keeps_split_paired(self):
        clean, warnings = mi.validate_instances(
            [_inst([0]), _inst([2, 1], n=8, ts="3,9")], [0, 1, 2], small_gpu=None)
        self.assertEqual(clean[0], {"gpus": [0], "n_slots": 4, "tensor_split": None})
        # GPU 1 had share 9 and GPU 2 had share 3; sorting must keep 1 -> 9, 2 -> 3
        self.assertEqual(clean[1], {"gpus": [1, 2], "n_slots": 8, "tensor_split": "9,3"})
        self.assertEqual(warnings, [])

    def test_shared_gpu_names_both_instances(self):
        with self.assertRaisesRegex(ValueError, "GPU 1 is used by servers 1 and 2"):
            mi.validate_instances([_inst([0, 1]), _inst([1])], [0, 1])

    def test_empty_gpus(self):
        with self.assertRaisesRegex(ValueError, "Server 1 needs at least one GPU"):
            mi.validate_instances([_inst([])], [0])

    def test_gpu_not_in_runtime(self):
        with self.assertRaisesRegex(ValueError, "GPU 5 is not in the active runtime"):
            mi.validate_instances([_inst([5])], [0, 1])

    def test_non_int_gpu_and_duplicate_gpu(self):
        with self.assertRaises(ValueError):
            mi.validate_instances([_inst(["1"])], [0, 1])
        with self.assertRaisesRegex(ValueError, "more than once"):
            mi.validate_instances([_inst([1, 1])], [0, 1])

    def test_n_slots_bounds(self):
        for bad in (0, 65, "4", True):
            with self.subTest(n_slots=bad), self.assertRaisesRegex(ValueError, "n_slots"):
                mi.validate_instances([{"gpus": [0], "n_slots": bad}], [0])

    def test_more_than_eight_instances(self):
        raw = [_inst([0]) for _ in range(9)]
        with self.assertRaisesRegex(ValueError, "At most 8"):
            mi.validate_instances(raw, [0])

    def test_not_a_list_and_zero_items(self):
        with self.assertRaises(ValueError):
            mi.validate_instances({"gpus": [0]}, [0])
        with self.assertRaises(ValueError):
            mi.validate_instances([], [0])

    def test_vllm_refused(self):
        with self.assertRaisesRegex(ValueError, "llama.cpp engine only"):
            mi.validate_instances([_inst([0])], [0], engine="vllm")

    def test_tensor_split_dropped_for_one_gpu(self):
        clean, _ = mi.validate_instances([_inst([0], ts="5,6")], [0])
        self.assertIsNone(clean[0]["tensor_split"])

    def test_bad_tensor_split_count_for_two_gpus(self):
        with self.assertRaisesRegex(ValueError, "tensor_split needs 2"):
            mi.validate_instances([_inst([0, 1], ts="9,11,3")], [0, 1])
        with self.assertRaisesRegex(ValueError, "tensor_split needs 2"):
            mi.validate_instances([_inst([0, 1], ts="9,x")], [0, 1])
        with self.assertRaisesRegex(ValueError, "non-negative"):
            mi.validate_instances([_inst([0, 1], ts="9,-1")], [0, 1])

    def test_helper_card_warning(self):
        clean, warnings = mi.validate_instances([_inst([0]), _inst([2])], [0, 1, 2], small_gpu=2)
        self.assertEqual(warnings, ["Instance 2 uses GPU 2, which the helper models also use."])
        self.assertEqual(len(clean), 2)


class InstanceSpecs(unittest.TestCase):
    def test_single_shape_matches_profile(self):
        p = {"engine": "llama", "gpu_devices": [0, 1], "n_slots": 3, "tuned": {}}
        self.assertEqual(mi.instance_specs(p), [{
            "index": 0, "port": 8090, "gpus": [0, 1], "n_slots": 3, "tensor_split": None, "single": True}])

    def test_single_default_slots(self):
        p = {"gpu_devices": [0]}
        self.assertEqual(mi.instance_specs(p)[0]["n_slots"], 1)

    def test_multi_drops_unknown_gpus_and_skips_empty_instances(self):
        p = {"engine": "llama", "gpu_devices": [0, 1], "tuned": {
            "main_mode": "multi",
            "main_instances": [_inst([0]), _inst([5]), _inst([1, 7], n=2, ts="3,4"), _inst([0, 1], n=6, ts="1,2")],
        }}
        specs = mi.instance_specs(p)
        self.assertEqual([s["index"] for s in specs], [0, 2, 3])
        self.assertEqual(specs[0], {"index": 0, "port": 8090, "gpus": [0], "n_slots": 4,
                                    "tensor_split": None, "single": False})
        # GPU 7 was dropped, so the split no longer lines up and is not guessed at
        self.assertEqual(specs[1]["gpus"], [1])
        self.assertIsNone(specs[1]["tensor_split"])
        self.assertEqual(specs[1]["port"], 8191 + 1)
        self.assertEqual(specs[2]["tensor_split"], "1,2")
        self.assertEqual(specs[2]["port"], 8192 + 1)


class ApplyConfigUpdate(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.dict(config.ACTIVE_RUNTIME, RUNTIME)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_main_mode_single_and_multi(self):
        p = {"engine": "llama"}
        self.assertIsNone(ch._apply_config_update(p, "main_mode", "multi"))
        self.assertEqual(p["tuned"]["main_mode"], "multi")
        self.assertIsNone(ch._apply_config_update(p, "main_mode", "single"))
        self.assertEqual(p["tuned"]["main_mode"], "single")

    def test_main_mode_rejects_bad_value_and_vllm(self):
        self.assertIn("main_mode must be one of", ch._apply_config_update({"engine": "llama"}, "main_mode", "quad"))
        err = ch._apply_config_update({"engine": "vllm"}, "main_mode", "multi")
        self.assertEqual(err, "Multiple servers work with the llama.cpp engine only")

    def test_main_instances_stores_clean_list(self):
        p = {"engine": "llama"}
        err = ch._apply_config_update(p, "main_instances", [_inst([0]), _inst([1, 2], n=2, ts="1,1")])
        self.assertIsNone(err)
        self.assertEqual(p["tuned"]["main_instances"][1], {"gpus": [1, 2], "n_slots": 2, "tensor_split": "1,1"})

    def test_main_instances_accepts_json_string(self):
        p = {"engine": "llama"}
        self.assertIsNone(ch._apply_config_update(p, "main_instances", '[{"gpus": [0], "n_slots": 2}]'))
        self.assertEqual(p["tuned"]["main_instances"][0]["n_slots"], 2)

    def test_main_instances_error_text_returned(self):
        err = ch._apply_config_update({"engine": "llama"}, "main_instances", [_inst([9])])
        self.assertEqual(err, "Server 1: GPU 9 is not in the active runtime's GPU list")

    def test_main_instances_empty_clears(self):
        p = {"engine": "llama", "tuned": {"main_mode": "multi", "main_instances": [_inst([0])]}}
        self.assertIsNone(ch._apply_config_update(p, "main_instances", []))
        self.assertEqual(mi.main_mode(p), "single")

    def test_config_view_fields(self):
        p = {"engine": "llama", "tuned": {"main_mode": "multi",
                                          "main_instances": [_inst([0]), _inst([2])]}}
        with mock.patch.object(ch, "_standalone_profile", return_value=None):
            view = ch._config_for_profile(p)
        self.assertEqual(view["main_mode"], "multi")
        self.assertEqual(view["main_instances"], [_inst([0]), _inst([2])])
        self.assertEqual(view["main_instance_ports"], [8090, 8191])
        self.assertEqual(view["main_instance_warnings"],
                         ["Instance 2 uses GPU 2, which the helper models also use."])
        self.assertTrue(view["main_multi_allowed"])


class PortReservation(unittest.TestCase):
    def test_suggest_port_skips_reserved_block(self):
        used = {"s%d" % i: {"port": 8094 + i} for i in range(96)}          # 8094..8189 taken
        with mock.patch.dict("core.small_model.APP_CONFIG", {"small_models": used}):
            self.assertEqual(local_storage.suggest_port(), 8200)

    def test_set_port_gpu_rejects_reserved_port(self):
        with self.assertRaisesRegex(ValueError, "reserved for the main model's extra servers"):
            local_storage._set_port_gpu("x", {}, {"port": 8195}, {}, cpu_ok=False)


if __name__ == "__main__":
    unittest.main()
