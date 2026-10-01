"""tests/test_supervision_limits.py - llama-server supervision limits are reachable and clamped.

Run: python -m unittest tests.test_supervision_limits -v

HEALTH_TIMEOUT_S and VRAM_WALL_FREE_B were module constants with no route to change them, and
they are the two knobs that decide whether a big model loads at all: a 70B off a slow NVMe can
legitimately need more than 120s, and there was no way to say so. Everything here writes to a
temp config, never the real config/app.json.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import supervision as sv
from core.small_model import APP_CONFIG


class LimitReading(unittest.TestCase):
    def setUp(self):
        self._saved = APP_CONFIG.get("supervision")
        APP_CONFIG["supervision"] = {}

    def tearDown(self):
        if self._saved is None:
            APP_CONFIG.pop("supervision", None)
        else:
            APP_CONFIG["supervision"] = self._saved

    def test_missing_keys_fall_back_to_the_defaults(self):
        self.assertEqual(sv.health_timeout_s(), sv.SUPERVISION_LIMITS["health_timeout_s"][0])
        self.assertEqual(sv.max_restart_count(), 5)

    def test_a_missing_block_does_not_raise(self):
        APP_CONFIG.pop("supervision", None)
        self.assertEqual(sv.health_timeout_s(), 120)

    def test_a_configured_value_is_used(self):
        APP_CONFIG["supervision"]["health_timeout_s"] = 900
        self.assertEqual(sv.health_timeout_s(), 900)

    def test_values_are_clamped_on_read(self):
        APP_CONFIG["supervision"]["health_timeout_s"] = 5
        self.assertEqual(sv.health_timeout_s(), sv.SUPERVISION_LIMITS["health_timeout_s"][1])
        APP_CONFIG["supervision"]["health_timeout_s"] = 10 ** 9
        self.assertEqual(sv.health_timeout_s(), sv.SUPERVISION_LIMITS["health_timeout_s"][2])

    def test_garbage_falls_back_rather_than_raising(self):
        for junk in ("soon", None, [], {}, float("nan")):
            APP_CONFIG["supervision"]["health_timeout_s"] = junk
            self.assertEqual(sv.health_timeout_s(), 120, f"{junk!r} should fall back to 120")

    def test_the_breaker_can_be_disabled(self):
        APP_CONFIG["supervision"]["max_restart_count"] = 0
        self.assertEqual(sv.max_restart_count(), 0)

    def test_the_vram_threshold_is_returned_in_bytes(self):
        """core/vram/devices.py compares free_b (bytes) against this, so the MiB config value
        has to be scaled or the threshold would be off by a factor of a million."""
        self.assertEqual(sv.vram_wall_free_b(), 256 * 1024 * 1024)
        APP_CONFIG["supervision"]["vram_wall_free_mb"] = 64
        self.assertEqual(sv.vram_wall_free_b(), 64 * 1024 * 1024)

    def test_view_reports_values_bounds_and_defaults(self):
        v = sv.view()
        for key in sv.SUPERVISION_LIMITS:
            self.assertIn(key, v)
            self.assertIn(key, v["limits"])
            self.assertIn(key, v["defaults"])
        self.assertEqual(v["limits"]["health_timeout_s"], [10, 3600])


class TheWallCheckUsesTheConfiguredThreshold(unittest.TestCase):
    def test_wall_check_honours_config(self):
        from core.vram.devices import wall_check
        devs = [{"index": 0, "free_b": 300 * 1024 * 1024}]
        APP_CONFIG["supervision"] = {"vram_wall_free_mb": 256}
        self.assertIsNone(wall_check({0}, devs), "300 MB free is above a 256 MB wall")
        APP_CONFIG["supervision"] = {"vram_wall_free_mb": 512}
        self.assertEqual(wall_check({0}, devs), 0, "a 512 MB wall trips on 300 MB free")

    def tearDown(self):
        APP_CONFIG.pop("supervision", None)


class SupervisionKeysArePersisted(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "app.json"
        self.tmp.write_text(json.dumps({"runtime": "vulkan",
                                        "supervision": {"health_timeout_s": 120}}),
                            encoding="utf-8")
        self._saved = APP_CONFIG.get("supervision")
        APP_CONFIG["supervision"] = {"health_timeout_s": 120}
        self.prof = {"name": "demo"}

    def tearDown(self):
        if self._saved is None:
            APP_CONFIG.pop("supervision", None)
        else:
            APP_CONFIG["supervision"] = self._saved

    def _apply(self, key, value):
        from routes.control import config_helpers
        with mock.patch("core.config.CONFIG_FILE", self.tmp):
            return config_helpers._apply_config_update(self.prof, key, value)

    def _on_disk(self):
        return json.loads(self.tmp.read_text(encoding="utf-8"))["supervision"]

    def test_a_value_is_written_and_takes_effect_without_a_restart(self):
        self.assertIsNone(self._apply("health_timeout_s", 900))
        self.assertEqual(self._on_disk()["health_timeout_s"], 900)
        self.assertEqual(sv.health_timeout_s(), 900)

    def test_out_of_range_is_clamped_not_rejected(self):
        self._apply("health_timeout_s", 5)
        self.assertEqual(self._on_disk()["health_timeout_s"], 10)
        self._apply("health_timeout_s", 99999)
        self.assertEqual(self._on_disk()["health_timeout_s"], 3600)

    def test_a_non_integer_is_rejected_with_a_reason(self):
        err = self._apply("health_timeout_s", "soon")
        self.assertIsNotNone(err)
        self.assertIn("integer", err)
        self.assertEqual(self._on_disk()["health_timeout_s"], 120, "must not have been written")

    def test_it_does_not_touch_the_model_profile(self):
        self._apply("health_timeout_s", 900)
        self.assertNotIn("health_timeout_s", self.prof,
                         "process supervision is not a per-model setting")

    def test_the_runtime_selector_is_not_clobbered(self):
        """app.json already has a top-level "runtime" key holding the llama.cpp preset NAME.
        Writing supervision settings into a second "runtime" object replaced it and broke
        preset selection - caught when core/config.py could not hash a dict."""
        self._apply("health_timeout_s", 900)
        cfg = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertEqual(cfg.get("runtime"), "vulkan")
        self.assertIn("health_timeout_s", cfg["supervision"])

    def test_an_unknown_key_is_still_rejected(self):
        err = self._apply("not_a_real_key_xyz", 1)
        self.assertIsNotNone(err)
        self.assertIn("unknown config field", err)


class ShippedConfigDeclaresThem(unittest.TestCase):
    def test_app_json_has_a_supervision_block(self):
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config" / "app.json"
                          ).read_text(encoding="utf-8"))
        self.assertIn("supervision", cfg)
        for key in sv.SUPERVISION_LIMITS:
            self.assertIn(key, cfg["supervision"], f"supervision.{key} must ship")

    def test_app_json_still_selects_a_named_runtime(self):
        """The bug this block was born from: a second top-level "runtime" key silently replaced
        the preset selector."""
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config" / "app.json"
                          ).read_text(encoding="utf-8"))
        self.assertIsInstance(cfg.get("runtime"), str)
        self.assertIn(cfg["runtime"], cfg.get("runtimes", {}))
        self.assertIsInstance(cfg.get("supervision"), dict)

    def test_no_duplicate_top_level_keys(self):
        """json.loads silently keeps the last of a duplicated key, so a collision like the one
        above is invisible without counting them."""
        import collections
        import re
        raw = (Path(__file__).resolve().parent.parent / "config" / "app.json"
               ).read_text(encoding="utf-8")
        # top-level keys sit at exactly two spaces of indent
        keys = re.findall(r'^ {2}"([a-z_]+)":', raw, re.M)
        dups = [k for k, c in collections.Counter(keys).items() if c > 1]
        self.assertEqual(dups, [], f"duplicate top-level keys in app.json: {dups}")

    def test_every_shipped_value_is_within_its_bounds(self):
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config" / "app.json"
                          ).read_text(encoding="utf-8"))["supervision"]
        for key, (_default, lo, hi) in sv.SUPERVISION_LIMITS.items():
            self.assertTrue(lo <= cfg[key] <= hi, f"{key}={cfg[key]} outside [{lo}, {hi}]")


class NoStaleConstantImports(unittest.TestCase):
    """core/state.py and core/vram/devices.py must read the config, not a module constant."""

    def test_state_calls_the_getters(self):
        import inspect

        from core.state import ProxyState
        src = inspect.getsource(ProxyState)
        for call in ("health_timeout_s()", "watchdog_interval_s()", "max_restart_count()",
                     "restart_reset_after_s()", "max_restart_backoff_s()"):
            self.assertIn(call, src, f"watchdog/health paths must use {call}")

    def test_state_does_not_import_the_raw_constants(self):
        """Check the imports, not the text: a comment legitimately mentions the old constant
        names to explain what changed."""
        import ast
        from pathlib import Path as P

        tree = ast.parse((P(__file__).resolve().parent.parent / "core" / "state.py"
                          ).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.name)
        stale = {"HEALTH_TIMEOUT_S", "WATCHDOG_INTERVAL_S", "MAX_RESTART_BACKOFF_S",
                 "MAX_RESTART_COUNT", "RESTART_RESET_AFTER_S"} & imported
        self.assertEqual(stale, set(), f"core/state.py still imports hardcoded constants: {stale}")
        # and the getters it needs are imported instead
        for getter in ("health_timeout_s", "watchdog_interval_s", "max_restart_count",
                       "restart_reset_after_s", "max_restart_backoff_s"):
            self.assertIn(getter, imported, f"core/state.py must import {getter}")

    def test_devices_does_not_use_the_module_constant(self):
        from pathlib import Path as P
        src = (P(__file__).resolve().parent.parent / "core" / "vram" / "devices.py"
               ).read_text(encoding="utf-8")
        self.assertNotIn("VRAM_WALL_FREE_B", src.split('"""', 2)[-1],
                         "wall_check must use the configured threshold")


class SettingsDrawerExposesThem(unittest.TestCase):
    def test_config_view_includes_supervision(self):
        from routes.control.config_helpers import _config_for_profile
        cfg = _config_for_profile({"name": "demo"})
        for key in sv.SUPERVISION_LIMITS:
            self.assertIn(key, cfg, f"the Settings drawer must be able to show/edit {key}")
        self.assertIn("limits", cfg)

    def test_status_reports_the_effective_values(self):
        from pathlib import Path as P
        src = (P(__file__).resolve().parent.parent / "routes" / "control" / "status_endpoints.py"
               ).read_text(encoding="utf-8")
        self.assertIn("max_restart_count()", src)
        self.assertIn("health_timeout_s()", src)


if __name__ == "__main__":
    unittest.main()