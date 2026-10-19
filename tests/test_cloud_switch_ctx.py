import unittest
import asyncio
from unittest import mock
from core.auth import Principal
from routes.control.status_endpoints import get_system_status
from routes.control.model_endpoints import available_models
from core.cloud.models import CloudModel


class TestCloudSwitchAndCtx(unittest.TestCase):
    def setUp(self):
        self.user = Principal(
            id=1, username="admin", display_name="Admin",
            is_super_admin=True, must_change_password=False,
            role_names=["admin"], permission_keys={"*"}
        )

    def test_status_returns_cloud_ctx_when_cloud_main_active(self):
        # Even if a local process is currently running with a 96k slot,
        # status must report the active cloud main lane's context window (262144).
        fake_cloud = CloudModel(
            provider="1",
            arg2={"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "https://openrouter.ai/api/v1"}},
            arg3="minimax/minimax-m3.1-flash:free",
            model_cfg={"name": "minimax-m3.1-flash", "ctx": 262144}
        )

        with mock.patch("core.cloud.cloud_lane", side_effect=lambda lane, uid: fake_cloud if lane == "main" else None):
            res = asyncio.run(get_system_status(self.user))
            self.assertEqual(res["cloud_main"]["ctx"], 262144)
            self.assertEqual(res["context"]["n_ctx"], 262144)

    def test_available_models_includes_ctx_and_loaded(self):
        fake_cloud = CloudModel(
            provider="1",
            arg2={"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "https://openrouter.ai/api/v1"}},
            arg3="minimax/minimax-m3.1-flash:free",
            model_cfg={"name": "minimax-m3.1-flash", "ctx": 262144}
        )

        with mock.patch("core.cloud.cloud_models", return_value=[fake_cloud]), \
             mock.patch("core.cloud.cloud_lane", return_value=fake_cloud):
            res = asyncio.run(available_models(self.user))
            cms = [m for m in res["models"] if m.get("kind") == "cloud"]
            self.assertTrue(len(cms) > 0)
            self.assertEqual(cms[0]["ctx"], 262144)
            self.assertTrue(cms[0]["currently_loaded"])


if __name__ == "__main__":
    unittest.main()
