"""tests/test_config_endpoints.py - regressions for POST /control/config.

The handler's `state.profile is None` branch referenced a `target` that the
routes/agent.py -> routes/control/ split left behind, so it raised NameError instead of
returning the 400 it clearly intended. ruff's F821 (one of the six gated rules) caught it;
this pins the behaviour so a later rename cannot reintroduce it silently.
Run: python -m unittest tests.test_config_endpoints -v
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.responses import JSONResponse

from core.auth import Principal
from routes.control import config_endpoints as ce
from routes.control.models import ConfigRequest


def _principal(uid=1):
    return Principal(id=uid, username=f"u{uid}", display_name=f"u{uid}", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set())


def _body(resp):
    """JSONResponse -> decoded body, so assertions read as data not bytes."""
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body).decode("utf-8"))
    return resp


class SetConfigWithoutLoadedProfile(unittest.IsolatedAsyncioTestCase):
    """Cold start: nothing in VRAM, so state.profile is None. This is the branch that
    raised NameError. get_config resolves the same fallback via get_model_hint, and so must
    this handler - otherwise a fresh install cannot be configured at all."""

    async def test_no_profile_and_no_model_hint_returns_400_not_nameerror(self):
        with mock.patch.object(ce.state, "profile", None), \
             mock.patch.object(ce.common, "get_model_hint", return_value=None):
            resp = await ce.set_config(ConfigRequest(updates={}), model=None, user=_principal())
        self.assertIsInstance(resp, JSONResponse)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(_body(resp), {"error": "no profile loaded"})

    async def test_model_hint_supplies_the_target(self):
        prof = {"model_path": "C:/m/hint.gguf", "ctx": 4096}
        with mock.patch.object(ce.state, "profile", None), \
             mock.patch.object(ce.common, "get_model_hint", return_value="C:/m/hint.gguf"), \
             mock.patch.object(ce, "_standalone_profile", return_value=prof), \
             mock.patch.object(ce, "_apply_config_update", return_value=None) as apply_upd:
            resp = await ce.set_config(ConfigRequest(updates={"ctx": 8192}), model=None,
                                       user=_principal())
        self.assertNotIsInstance(resp, JSONResponse, f"expected success, got {_body(resp)}")
        self.assertEqual(apply_upd.call_args[0][0], prof, "must validate against the hint's profile")

    async def test_explicit_model_beats_the_hint(self):
        prof = {"model_path": "C:/m/explicit.gguf"}
        with mock.patch.object(ce.state, "profile", None), \
             mock.patch.object(ce.common, "get_model_hint", return_value="C:/m/hint.gguf"), \
             mock.patch.object(ce, "_standalone_profile", return_value=prof) as sa, \
             mock.patch.object(ce, "_apply_config_update", return_value=None):
            await ce.set_config(ConfigRequest(updates={}), model="C:/m/explicit.gguf",
                                user=_principal())
        sa.assert_called_once_with("C:/m/explicit.gguf")

    async def test_unresolvable_hint_reports_the_path_not_a_crash(self):
        with mock.patch.object(ce.state, "profile", None), \
             mock.patch.object(ce.common, "get_model_hint", return_value="C:/m/gone.gguf"), \
             mock.patch.object(ce, "_standalone_profile", return_value=None):
            resp = await ce.set_config(ConfigRequest(updates={}), model=None, user=_principal())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(_body(resp), {"error": "model file not found: C:/m/gone.gguf"})

    async def test_loaded_profile_takes_the_live_path(self):
        """Regression guard for the other side of the branch: when a profile IS loaded and
        no ?model= is given, it must be used directly and the hint must not be consulted."""
        live = {"model_path": "C:/m/live.gguf"}
        with mock.patch.object(ce.state, "profile", live), \
             mock.patch.object(ce.common, "get_model_hint") as hint, \
             mock.patch.object(ce, "_apply_config_update", return_value=None):
            resp = await ce.set_config(ConfigRequest(updates={"ctx": 4096}), model=None,
                                       user=_principal())
        self.assertNotIsInstance(resp, JSONResponse, f"expected success, got {_body(resp)}")
        hint.assert_not_called()


if __name__ == "__main__":
    unittest.main()