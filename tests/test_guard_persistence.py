"""tests/test_guard_persistence.py - Verify input & output sanitizer persistence across server restarts.

Run: python -m unittest tests.test_guard_persistence -v
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import small_model, input_guard, output_guard
from routes.input_guard import _public


class TestGuardPersistence(unittest.TestCase):
    def setUp(self):
        self._orig_app_config = copy.deepcopy(small_model.APP_CONFIG)

    def tearDown(self):
        small_model.APP_CONFIG.clear()
        small_model.APP_CONFIG.update(self._orig_app_config)
        input_guard._compile_cache.clear()
        input_guard._cache_ts = 0.0

    def test_load_app_config_loads_guards_from_file(self):
        sample_input = {
            "enabled": True,
            "rules": [
                {
                    "id": "in-1",
                    "name": "Block sensitive data",
                    "type": "regex",
                    "patterns": ["secret_token"],
                    "scope": "block_all",
                    "roles": [],
                    "users": [],
                    "message": "Blocked",
                    "enabled": True,
                }
            ],
        }
        sample_output = {
            "enabled": True,
            "rules": [
                {
                    "id": "out-1",
                    "name": "Redact passwords",
                    "type": "regex",
                    "patterns": ["password123"],
                    "scope": "block_all",
                    "roles": [],
                    "users": [],
                    "replacement": "[REDACTED]",
                    "enabled": True,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = Path(tmpdir) / "app.json"
            cfg_file.write_text(json.dumps({
                "models_dir": "E:/AI/Models",
                "input_guard": sample_input,
                "output_guard": sample_output,
                "custom_future_section": {"foo": "bar"},
            }), encoding="utf-8")

            with patch("core.small_model.CONFIG_FILE", cfg_file):
                loaded = small_model._load_app_config()

                self.assertIn("input_guard", loaded)
                self.assertTrue(loaded["input_guard"]["enabled"])
                self.assertEqual(len(loaded["input_guard"]["rules"]), 1)
                self.assertEqual(loaded["input_guard"]["rules"][0]["name"], "Block sensitive data")

                self.assertIn("output_guard", loaded)
                self.assertTrue(loaded["output_guard"]["enabled"])
                self.assertEqual(len(loaded["output_guard"]["rules"]), 1)
                self.assertEqual(loaded["output_guard"]["rules"][0]["name"], "Redact passwords")

                # Verify future/custom top-level sections are preserved
                self.assertIn("custom_future_section", loaded)
                self.assertEqual(loaded["custom_future_section"], {"foo": "bar"})

    def test_public_endpoint_reads_from_loaded_app_config(self):
        # When _load_app_config initializes APP_CONFIG, _public should return what was loaded
        loaded = small_model._load_app_config()
        small_model.APP_CONFIG.clear()
        small_model.APP_CONFIG.update(loaded)

        inp = _public("input_guard")
        out = _public("output_guard")

        # config/app.json already contains the user's stored rules
        self.assertTrue(inp["enabled"])
        self.assertTrue(len(inp["rules"]) >= 1)
        self.assertTrue(out["enabled"])
        self.assertTrue(len(out["rules"]) >= 1)

        # Assert the STRUCTURE of the shipped rule set, not the name of whichever rule
        # happens to be first. Both guards must carry a deterministic regex rule as well as
        # the semantic one: the semantic classifier is a fail-open 4-token local LLM call, so
        # a config with no regex rules leaves it as the only control (see core/secrets.py).
        for label, block in (("input_guard", inp), ("output_guard", out)):
            types = [str(r.get("type") or "regex").lower() for r in block["rules"]]
            with self.subTest(label):
                self.assertIn("regex", types,
                              f"{label} ships no regex rule - the semantic classifier is "
                              "fail-open and prompt-injectable, so it cannot be the only check")
                self.assertIn("semantic", types)

        # No rule may be scoped to a subset of roles that excludes administrators: an
        # admin-only account slipping past the data-sharing policy is not a useful default.
        for label, block in (("input_guard", inp), ("output_guard", out)):
            for r in block["rules"]:
                with self.subTest(f"{label}:{r.get('name')}"):
                    self.assertEqual(r.get("roles") or [], [])

    def test_guard_fallback_when_app_config_missing_keys(self):
        # Simulate edge case: APP_CONFIG has no input_guard or output_guard keys
        small_model.APP_CONFIG.pop("input_guard", None)
        small_model.APP_CONFIG.pop("output_guard", None)

        # Core guards and routes should fall back gracefully to CONFIG_FILE
        self.assertTrue(input_guard.enabled())
        self.assertTrue(len(input_guard.guard_cfg().get("rules", [])) >= 1)
        self.assertTrue(len(output_guard.guard_cfg().get("rules", [])) >= 1)

        pub_in = _public("input_guard")
        self.assertTrue(pub_in["enabled"])
        self.assertTrue(len(pub_in["rules"]) >= 1)


if __name__ == "__main__":
    unittest.main()
