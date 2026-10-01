"""tests/test_request_limits.py - inbound body size and per-user request rate.

Run: python -m unittest tests.test_request_limits -v

There was no body-size limit anywhere in the app and no rate limit outside login, so anyone
holding `chat.use` could send one enormous `messages` array (the PAN regex then walks every
element) or an unbounded number of requests per second.
"""

import json
import sys
import unittest
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import limits
from core.limits import RequestLimitsMiddleware
from core.small_model import APP_CONFIG

MB = 1024 * 1024


async def _echo(request):
    body = await request.body()
    return JSONResponse({"len": len(body), "ok": True})


class LimitTests(unittest.TestCase):
    def setUp(self):
        self._serving = dict(APP_CONFIG.get("serving") or {})
        APP_CONFIG["serving"] = {
            "queue_enabled": True,
            "max_body_bytes": 64 * 1024,
            "body_limit_paths": {"/knowledge/upload": 512 * 1024},
            "rate_limit_per_min": 5,
            "rate_limit_paths": ["/agent/run"],
        }
        limits.buckets.reset()
        self.app = Starlette(routes=[
            Route("/agent/run", _echo, methods=["POST"]),
            Route("/knowledge/upload", _echo, methods=["POST"]),
            Route("/control/monitor", _echo, methods=["GET"]),
        ])
        self.app.add_middleware(RequestLimitsMiddleware)
        self.client = TestClient(self.app)

    def tearDown(self):
        APP_CONFIG["serving"] = self._serving
        limits.buckets.reset()

    # ---------- body size ----------

    def test_a_body_over_the_limit_is_refused(self):
        r = self.client.post("/agent/run", content=b"x" * (128 * 1024))
        self.assertEqual(r.status_code, 413)
        self.assertIn("too large", r.json()["error"])

    def test_a_body_under_the_limit_gets_through(self):
        r = self.client.post("/agent/run", content=b"x" * 1024)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["len"], 1024)

    def test_limits_are_per_path(self):
        # the knowledge-base upload endpoint legitimately carries tens of megabytes
        r = self.client.post("/knowledge/upload", content=b"x" * (200 * 1024))
        self.assertEqual(r.status_code, 200)

    def test_a_chunked_body_is_capped_too(self):
        """Content-Length alone would not do: a chunked request carries no such header."""

        def gen():
            for _ in range(8):
                yield b"y" * (32 * 1024)

        r = self.client.post("/agent/run", content=gen())
        self.assertEqual(r.status_code, 413)

    def test_zero_disables_the_limit(self):
        APP_CONFIG["serving"]["max_body_bytes"] = 0
        r = self.client.post("/agent/run", content=b"x" * (1024 * 1024))
        self.assertEqual(r.status_code, 200)

    def test_a_get_is_never_body_limited(self):
        # the monitor is polled; it must not be able to 413 itself
        r = self.client.get("/control/monitor")
        self.assertEqual(r.status_code, 200)

    # ---------- rate ----------

    def test_burst_is_allowed_then_throttled(self):
        codes = [self.client.post("/agent/run", content=b"{}").status_code for _ in range(8)]
        self.assertIn(200, codes)
        self.assertIn(429, codes)
        self.assertEqual(codes[:5], [200] * 5, "the allowance should be spent first")

    def test_the_throttle_is_per_credential_not_global(self):
        self.client.cookies.set("a770_session", "user-a")
        for _ in range(6):
            self.client.post("/agent/run", content=b"{}")
        self.client.cookies.set("a770_session", "user-b")
        r = self.client.post("/agent/run", content=b"{}")
        self.assertEqual(r.status_code, 200, "one user exhausting their bucket must not block another")

    def test_unthrottled_paths_are_untouched(self):
        for _ in range(30):
            r = self.client.get("/control/monitor")
            self.assertEqual(r.status_code, 200)

    def test_zero_disables_rate_limiting(self):
        APP_CONFIG["serving"]["rate_limit_per_min"] = 0
        codes = [self.client.post("/agent/run", content=b"{}").status_code for _ in range(20)]
        self.assertEqual(set(codes), {200})

    def test_a_credential_is_never_stored_in_the_clear(self):
        self.client.cookies.set("a770_session", "SUPERSECRETSESSIONTOKEN")
        self.client.post("/agent/run", content=b"{}")
        self.assertNotIn("SUPERSECRETSESSIONTOKEN", json.dumps(
            {k: str(v) for k, v in limits.buckets._m.items()}))


class ConfigDefaultsTests(unittest.TestCase):
    """Reads config/app.json off disk, not APP_CONFIG.

    APP_CONFIG is process-global and several tests overwrite blocks in it, so asserting against
    it here would depend on test ordering rather than on what actually ships."""

    @staticmethod
    def _shipped() -> dict:
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config" / "app.json"
                          ).read_text(encoding="utf-8"))
        return cfg["serving"]

    def test_the_shipped_config_declares_every_limit(self):
        serving = self._shipped()
        for key in ("max_body_bytes", "body_limit_paths", "rate_limit_per_min",
                    "rate_limit_paths", "max_inflight_cloud", "cloud_requests_per_day"):
            self.assertIn(key, serving, f"config/app.json serving.{key} must be set")

    def test_the_upload_paths_keep_real_headroom(self):
        over = self._shipped()["body_limit_paths"]
        # routes/knowledge/models.py allows a 50 MB file, and /agent/upload allows 20 of them
        self.assertGreaterEqual(over["/knowledge/upload"], 50 * MB)
        self.assertGreaterEqual(over["/agent/upload"], 20 * MB)

    def test_the_base_limit_fits_any_chat_payload(self):
        self.assertGreaterEqual(self._shipped()["max_body_bytes"], 4 * MB)

    def test_the_raw_api_is_rate_limited_too(self):
        paths = self._shipped()["rate_limit_paths"]
        self.assertIn("/v1/chat/completions", paths)


class BindDefaultsToLoopbackTests(unittest.TestCase):
    def test_the_server_does_not_bind_every_interface_by_default(self):
        """It bound 0.0.0.0: over plain HTTP the session cookie is sniffable on the LAN
        (PCI DSS 4.2.1), and this platform holds cloud keys and a shell tool."""
        from core.config import PROXY_HOST
        self.assertEqual(PROXY_HOST, "127.0.0.1")

    def test_the_off_loopback_path_still_warns(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "server_manager.py").read_text(encoding="utf-8")
        self.assertIn("PCI DSS 4.2.1", src)
        self.assertIn("--host", src)


class AdmissionCoversEveryPathTests(unittest.TestCase):
    """The gate was sized from the local llama-server's slot count and skipped every
    non-local client, and /v1/* forwarded raw bytes without meeting it at all."""

    def test_the_proxy_holds_the_gate(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "routes" / "proxy" / "endpoints.py"
               ).read_text(encoding="utf-8")
        self.assertIn("admission.hold()", src)

    def test_cloud_lanes_are_throttled_too(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "routes" / "common" / "llm_stream.py"
               ).read_text(encoding="utf-8")
        self.assertIn("cloud_gate", src)

    def test_the_proxy_checks_the_daily_cloud_quota(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "routes" / "proxy" / "endpoints.py"
               ).read_text(encoding="utf-8")
        self.assertIn("check_cloud_request_quota", src)


if __name__ == "__main__":
    unittest.main()