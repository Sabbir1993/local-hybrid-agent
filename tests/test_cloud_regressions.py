"""Behaviour the package split dropped and this file pins (audit vs 5887c73^)."""
import unittest
from unittest import mock

from core.cloud import lanes
from core.cloud.models import CloudModel

MERGED = {
    "provider": {
        "1": {"name": "Open Router",
              "options": {"baseURL": "https://openrouter.ai/api/v1", "apiKey": "sk-abcdef123456789xyz"},
              "models": {"stealth/space-bunny-alpha": {"name": "space-bunny-alpha", "ctx": 262144},
                         "plain": {"name": "plain"}}},
        "2": {"name": "NoBase", "options": {}, "models": {"m": {}}},
        "3": {"name": "Legacy", "options": {"base_url": "https://legacy.example/openai", "timeout_s": 0},
              "ctx": 65536, "models": {"x": {}}},
    },
    "cloud": {"main": " 1/stealth/space-bunny-alpha ", "executor": None, "fallback_local": False},
    "lanes": {"mine": {"kind": "chat", "cloud": "3/x"}},
}


def patched(merged=None):
    return mock.patch.object(lanes, "_merged", lambda uid=None: merged or MERGED)


class KeyResolutionTests(unittest.TestCase):
    def test_cloud_prefix_and_provider_ids_with_slashes_in_the_model(self):
        with patched():
            cm = lanes.get_cloud("cloud:1/stealth/space-bunny-alpha")
            self.assertEqual((cm.provider, cm.model_id), ("1", "stealth/space-bunny-alpha"))
            self.assertEqual(lanes.get_cloud(" 1/stealth/space-bunny-alpha ").key, cm.key)

    def test_only_configured_models_resolve(self):
        with patched():
            self.assertIsNone(lanes.get_cloud("1/typo-model"))      # not sent to the provider
            self.assertIsNone(lanes.get_cloud("2/m"))               # provider without an endpoint
            self.assertIsNone(lanes.get_cloud("nonsense"))
            self.assertEqual([m.key for m in lanes.cloud_models()],
                             ["1/stealth/space-bunny-alpha", "1/plain", "3/x"])


class ModelTests(unittest.TestCase):
    def test_endpoint_does_not_double_v1(self):
        with patched():
            self.assertEqual(lanes.get_cloud("1/plain").endpoint(), "https://openrouter.ai/api/v1/chat/completions")
            self.assertEqual(lanes.get_cloud("3/x").endpoint(), "https://legacy.example/openai/v1/chat/completions")

    def test_ctx_falls_back_model_then_provider_then_32768(self):
        with patched():
            self.assertEqual(lanes.get_cloud("1/stealth/space-bunny-alpha").ctx, 262144)
            self.assertEqual(lanes.get_cloud("3/x").ctx, 65536)
            self.assertEqual(lanes.get_cloud("1/plain").ctx, 32768)

    def test_base_url_alias_and_explicit_zero_timeout(self):
        with patched():
            cm = lanes.get_cloud("3/x")
            self.assertEqual(cm.base_url, "https://legacy.example/openai")
            self.assertGreater(cm.timeout_s, 0)                     # 0 means "default", not "no time"

    def test_names_the_ui_and_usage_records_rely_on(self):
        with patched():
            cm = lanes.get_cloud("1/stealth/space-bunny-alpha")
            self.assertEqual(cm.display, "space-bunny-alpha")
            self.assertEqual(cm.label(), "space-bunny-alpha (Open Router)")
            info = cm.info("main")
            for k in ("role", "source", "provider", "provider_name", "key", "display", "device"):
                self.assertIn(k, info)
            self.assertEqual((info["source"], info["provider_name"]), ("cloud", "Open Router"))


class BindingTests(unittest.TestCase):
    def test_bindings_are_normalised(self):
        with patched():
            b = lanes.cloud_bindings()
            self.assertEqual(b["main"], "1/stealth/space-bunny-alpha")
            self.assertIsNone(b["executor"])
            self.assertIs(b["fallback_local"], False)
            self.assertEqual(b["routing_mode"], "auto")             # absent -> auto
        with patched({**MERGED, "cloud": {"routing_mode": "CUSTOM"}}):
            self.assertEqual(lanes.cloud_bindings()["routing_mode"], "custom")
            self.assertTrue(lanes.cloud_bindings()["fallback_local"])

    def test_auto_routing_makes_executor_and_vision_follow_main(self):
        with patched():
            self.assertEqual(lanes.cloud_lane("main").key, "1/stealth/space-bunny-alpha")
            self.assertEqual(lanes.cloud_lane("executor").key, "1/stealth/space-bunny-alpha")
            self.assertEqual(lanes.cloud_lane("vision").key, "1/stealth/space-bunny-alpha")
        custom = {**MERGED, "cloud": {"main": "1/plain", "routing_mode": "custom"}}
        with patched(custom):
            self.assertIsNotNone(lanes.cloud_lane("main"))
            self.assertIsNone(lanes.cloud_lane("executor"))          # bound independently: local

    def test_user_owned_lane_uses_its_own_binding(self):
        with patched():
            self.assertEqual(lanes.cloud_lane("mine").key, "3/x")
            self.assertIsNone(lanes.cloud_lane("unknown-lane"))

    def test_a_stale_binding_falls_back_to_local(self):
        stale = {**MERGED, "cloud": {"main": "1/deleted-model", "routing_mode": "custom"}}
        with patched(stale):
            self.assertIsNone(lanes.cloud_lane("main"))


class PublicShapeTests(unittest.TestCase):
    def test_providers_public_has_the_shape_cloud_js_reads(self):
        with patched():
            p = lanes.providers_public()[0]
            for k in ("provider", "name", "base_url", "key_masked", "has_key", "extra_headers", "models"):
                self.assertIn(k, p)
            self.assertEqual(p["provider"], "1")
            self.assertTrue(p["has_key"])
            self.assertNotIn("sk-abcdef123456789xyz", str(p))         # never the key itself
            self.assertEqual(p["models"][0]["id"], "stealth/space-bunny-alpha")
            self.assertEqual(p["models"][0]["ctx"], 262144)


class ConfigAndReportTests(unittest.TestCase):
    def test_app_config_keeps_its_default_sections(self):
        from core.small_model import APP_CONFIG
        for k in ("capabilities", "input_guard", "output_guard", "media"):
            self.assertIn(k, APP_CONFIG)
        # the live config may override the limit (Settings), so only require that the section survives
        self.assertGreater(APP_CONFIG["media"]["limits"]["image_per_day"], 0)
        ex = APP_CONFIG["small_models"]["executor"]
        self.assertIn("port", ex)                                   # a partial user entry keeps the port

    def test_lane_kind_is_normalised(self):
        from core.small_model import lane_engine_of, lane_kind_of
        self.assertEqual(lane_kind_of("x", {"kind": "IMAGE_GEN"}), "image_gen")
        self.assertEqual(lane_kind_of("x", {"kind": "nonsense"}), "chat")
        self.assertEqual(lane_engine_of("x", {"kind": "stt"}), "whisper")

    def test_usage_report_carries_the_keys_the_js_reads(self):
        from core.db import db_report
        r = db_report(1)
        for k in ("requests", "total_requests", "cache_hit_rate", "orchestrator_requests",
                  "orchestrator_total_tokens", "by_model", "by_day"):
            self.assertIn(k, r)
        for m in r["by_model"]:
            self.assertIn("is_orchestrator", m)


if __name__ == "__main__":
    unittest.main()


class CloudWindowTests(unittest.TestCase):
    """Compaction budgets a cloud lane against the model's own window, not a 32K fallback."""
    def test_cloud_client_exposes_the_model_window(self):
        from core.cloud.client import CloudClient
        with patched():
            cm = lanes.get_cloud("1/stealth/space-bunny-alpha")
            self.assertEqual(CloudClient(cm).ctx, 262144)
            self.assertEqual(CloudClient(lanes.get_cloud("1/plain")).ctx, 32768)
