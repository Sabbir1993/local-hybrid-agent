"""tests/test_telemetry_ws.py - posture of /ws/telemetry.

The endpoint authenticates in-handler (cookie / Bearer - NEVER a query
parameter, URLs get logged), demands monitor.view, and closes with 4401
(no auth) / 4403 (wrong permission) before accepting. These are the unit
pins behind the PUBLIC_WS review entry in tests/test_auth_invariant.py."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def _principal(uid, perms, super_admin=False):
    from core.auth import Principal
    return Principal(id=uid, username=f"u{uid}", display_name=f"u{uid}",
                     is_super_admin=super_admin, must_change_password=False,
                     role_names=[], permission_keys=set(perms))


class TelemetryWsPosture(unittest.TestCase):
    def _app(self):
        from routes import telemetry_ws as t
        app = FastAPI()
        app.include_router(t.router)
        return t, TestClient(app)

    def test_missing_session_closes_4401(self):
        t, client = self._app()
        with mock.patch.object(t, "verify_session", return_value=None):
            with self.assertRaises(WebSocketDisconnect) as cm:
                with client.websocket_connect("/ws/telemetry") as ws:
                    ws.receive_text()
        self.assertEqual(cm.exception.code, 4401)

    def test_missing_monitor_view_closes_4403(self):
        t, client = self._app()
        principal = _principal(9, {"chat.use"})
        with mock.patch.object(t, "verify_session", return_value=principal):
            with self.assertRaises(WebSocketDisconnect) as cm:
                with client.websocket_connect(
                        "/ws/telemetry",
                        headers={"Authorization": "Bearer a770_pat_test"}) as ws:
                    ws.receive_text()
        self.assertEqual(cm.exception.code, 4403)

    def test_bearer_token_accepted_and_snapshot_streamed(self):
        t, client = self._app()
        principal = _principal(9, {"monitor.view"})
        snap = {"cards": {}, "model_state": {"state": "idle"}}
        with mock.patch.object(t, "verify_session", return_value=principal), \
             mock.patch.object(t, "get_telemetry_snapshot",
                               new=mock.AsyncMock(return_value=snap)):
            with client.websocket_connect(
                    "/ws/telemetry",
                    headers={"Authorization": "Bearer a770_pat_test"}) as ws:
                first = ws.receive_json()
        self.assertEqual(first, snap)

    def test_no_token_in_url_regression(self):
        src = Path(Path(__file__).resolve().parents[1] / "routes" / "telemetry_ws.py") \
            .read_text(encoding="utf-8")
        self.assertNotIn("query_params", src,
                         "session tokens must never travel in a URL (logs/referrers)")


if __name__ == "__main__":
    unittest.main()
