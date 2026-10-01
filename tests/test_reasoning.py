"""tests/test_reasoning.py - F3: reasoning-effort mapping (was 26% covered).

Pure request-field mapping: nothing client-supplied is forwarded raw. These
tests pin the level/mode/field tables, the budget fallback chain, and the
model-capability cache behaviour.

Run: python -m unittest tests.test_reasoning -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import reasoning
from core.small_model import APP_CONFIG


class BudgetTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(reasoning.budget("none"), 0)
        self.assertEqual(reasoning.budget("low"), 1024)
        self.assertEqual(reasoning.budget("medium"), 4096)
        self.assertEqual(reasoning.budget("high"), 12288)
        self.assertEqual(reasoning.budget("extra"), -1)
        self.assertEqual(reasoning.budget("whatever"), -1)

    def test_config_override_and_garbage(self):
        with mock.patch.dict(APP_CONFIG, {"reasoning_effort": {"budgets": {"medium": 100}}}):
            self.assertEqual(reasoning.budget("medium"), 100)
            self.assertEqual(reasoning.budget("low"), 1024)  # untouched levels keep defaults
        with mock.patch.dict(APP_CONFIG, {"reasoning_effort": {"budgets": {"medium": "lots"}}}):
            self.assertEqual(reasoning.budget("medium"), 4096)


class CapResolveTests(unittest.TestCase):
    def test_cap_orders_levels(self):
        self.assertEqual(reasoning.cap("high", "medium"), "medium")
        self.assertEqual(reasoning.cap("low", "high"), "low")
        self.assertEqual(reasoning.cap("medium", "medium"), "medium")

    def test_cap_unknown_passes_through(self):
        self.assertEqual(reasoning.cap("turbo", "medium"), "turbo")
        self.assertEqual(reasoning.cap("high", "turbo"), "high")

    def test_resolve_explicit_wins(self):
        self.assertEqual(reasoning.resolve("low", deep=True), "low")

    def test_resolve_none_keeps_old_behaviour(self):
        self.assertEqual(reasoning.resolve(None, deep=True), "medium")
        self.assertIsNone(reasoning.resolve(None, deep=False))
        self.assertEqual(reasoning.resolve("nonsense", deep=True), "medium")


class ModeTests(unittest.TestCase):
    def test_template_sniffing(self):
        self.assertEqual(reasoning.mode_from_template(""), "none")
        self.assertEqual(reasoning.mode_from_template("no markers here"), "none")
        self.assertEqual(reasoning.mode_from_template("x enable_thinking y"), "levels")
        self.assertEqual(reasoning.mode_from_template("reasoning_effort=high"), "levels")
        self.assertEqual(reasoning.mode_from_template("<think>hmm"), "toggle")
        self.assertEqual(reasoning.mode_from_template("reasoning_content here"), "toggle")

    def test_missing_model_is_none(self):
        self.assertEqual(reasoning.local_mode(None), "none")
        self.assertEqual(reasoning.local_mode("/no/such/model.gguf"), "none")

    def test_manual_override_wins(self):
        with mock.patch("core.profiles.load_model_configs",
                        return_value={"m.gguf": {"reasoning": "toggle"}}):
            self.assertEqual(reasoning.local_mode("m.gguf"), "toggle")

    def test_cloud_mode(self):
        self.assertEqual(reasoning.cloud_mode(SimpleNamespace(model_cfg={"reasoning": "none"})), "none")
        self.assertEqual(reasoning.cloud_mode(SimpleNamespace(model_cfg={"reasoning": "bogus"})), "levels")
        self.assertEqual(reasoning.cloud_mode(SimpleNamespace(model_cfg=None)), "levels")
        self.assertEqual(reasoning.cloud_mode(None), "levels")


class FieldsTests(unittest.TestCase):
    def test_local_fields(self):
        self.assertEqual(reasoning.local_fields("turbo"), {})
        none = reasoning.local_fields("none")
        self.assertFalse(none["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(none["thinking_budget_tokens"], 0)
        med = reasoning.local_fields("medium")
        self.assertTrue(med["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual((med["reasoning_effort"], med["thinking_budget_tokens"]), ("medium", 4096))
        extra = reasoning.local_fields("extra")
        self.assertEqual(extra["reasoning_effort"], "high")  # templates know high, not extra
        self.assertNotIn("thinking_budget_tokens", extra)  # -1 = unrestricted, key omitted

    def test_cloud_fields(self):
        cm = SimpleNamespace(model_cfg={}, base_url="https://openrouter.ai/api/v1")
        self.assertEqual(reasoning.cloud_fields("high", cm), {"reasoning": {"effort": "high"}})
        self.assertEqual(reasoning.cloud_fields("none", cm), {"reasoning": {"enabled": False}})
        other = SimpleNamespace(model_cfg={}, base_url="https://api.provider.com/v1")
        self.assertEqual(reasoning.cloud_fields("high", other), {"reasoning_effort": "high"})
        self.assertEqual(reasoning.cloud_fields("none", other), {"reasoning_effort": "none"})
        no_mode = SimpleNamespace(model_cfg={"reasoning": "none"}, base_url="https://x")
        self.assertEqual(reasoning.cloud_fields("high", no_mode), {})
        self.assertEqual(reasoning.cloud_fields("high", None), {})
        self.assertEqual(reasoning.cloud_fields("turbo", other), {})

    def test_cloud_keys_cover_both_shapes(self):
        self.assertEqual(set(reasoning.CLOUD_KEYS), {"reasoning", "reasoning_effort"})


if __name__ == "__main__":
    unittest.main()
