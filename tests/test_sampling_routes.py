import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import Principal
from routes import control


def _principal(uid=1):
    return Principal(id=uid, username="admin", display_name="Admin", is_super_admin=True,
                     must_change_password=False, role_names=[], permission_keys={"settings.runtime.view"})


class SamplingRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_file = Path(self.tmp.name) / "sampling_config.json"
        self.app = FastAPI()
        self.app.include_router(control.router)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sampling_defaults_and_save(self):
        from core.deps import get_current_user
        self.app.dependency_overrides[get_current_user] = lambda: _principal()
        with mock.patch.object(control, "SAMPLING_CONFIG_PATH", self.cfg_file):
            # 1. Fetch defaults
            r = self.client.get("/control/sampling")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["temp"], 0.6)
            self.assertEqual(data["topp"], 0.95)
            self.assertEqual(data["topk"], 20)

            # 2. Save custom sampling config
            payload = {
                "sysprompt": "You are a senior engineer.",
                "temp": 0.85,
                "topp": 0.9,
                "minp": 0.05,
                "rep": 1.1,
                "presence": 0.2,
                "topk": 50,
                "maxtok": 4096,
            }
            r_post = self.client.post("/control/sampling", json=payload)
            self.assertEqual(r_post.status_code, 200)
            self.assertTrue(r_post.json().get("ok"))
            self.assertEqual(r_post.json()["config"]["temp"], 0.85)
            self.assertEqual(r_post.json()["config"]["sysprompt"], "You are a senior engineer.")

            # 3. Fetch again: should be saved
            r_get = self.client.get("/control/sampling")
            self.assertEqual(r_get.status_code, 200)
            self.assertEqual(r_get.json()["temp"], 0.85)
            self.assertEqual(r_get.json()["topp"], 0.9)
            self.assertEqual(r_get.json()["maxtok"], 4096)
            self.assertEqual(r_get.json()["sysprompt"], "You are a senior engineer.")

            # 4. Out-of-bounds values should be clamped safely
            bad_payload = {
                "temp": 99.0,
                "topp": -5.0,
                "topk": 999999,
            }
            r_clamp = self.client.post("/control/sampling", json=bad_payload)
            self.assertEqual(r_clamp.status_code, 200)
            self.assertEqual(r_clamp.json()["config"]["temp"], 2.0)
            self.assertEqual(r_clamp.json()["config"]["topp"], 0.0)
            self.assertEqual(r_clamp.json()["config"]["topk"], 1000)
