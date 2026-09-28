"""tests/test_browser_device_tools.py - core/browser_tools.py + core/device_tools.py with the
companion faked: ops are routed to the device, results are PAN-masked, screenshots are
saved on the device + described locally, and old companions get an upgrade hint.

Run: python -m pytest tests/test_browser_device_tools.py -q
"""

import asyncio
import base64
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import browser_tools, device_tools
from core.request_context import set_current_user

TEST_PAN = "4" + "1" * 15      # standard test number, not a real card


def _png() -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


class _Companion:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def call(self, uid, op, params, timeout=60):
        self.calls.append((op, params))
        r = self.replies.get(op)
        if isinstance(r, Exception):
            raise r
        return r if r is not None else {}


def run(coro):
    return asyncio.run(coro)


class BrowserToolTests(unittest.TestCase):
    def setUp(self):
        set_current_user(7)
        self.ws = Path("C:/proj")
        self._dev = mock.patch("core.agent_tools.require_device_workspace", return_value=(7, self.ws))
        self._dev.start()

    def tearDown(self):
        self._dev.stop()
        set_current_user(None)

    def _with(self, replies):
        comp = _Companion(replies)
        return comp, mock.patch.object(browser_tools.companion_bridge, "call", comp.call)

    def test_navigate_returns_masked_snapshot(self):
        comp, p = self._with({"browser.navigate": {"url": "http://localhost:3000/", "title": "App", "status": 200,
                                                   "snapshot": f'- textbox "card" [ref=e3]: {TEST_PAN}'}})
        with p:
            out = run(browser_tools.tool_browser_navigate({"url": "http://localhost:3000/", "device": "pixel-7"}))
        self.assertIn("title: App", out)
        self.assertIn("[ref=e3]", out)
        self.assertNotIn(TEST_PAN, out)
        op, params = comp.calls[0]
        self.assertEqual(op, "browser.navigate")
        self.assertEqual(params["device"], "pixel-7")
        self.assertTrue(params["headless"])

    def test_screenshot_saved_on_device_described_locally_and_thumbnailed(self):
        png = _png()
        comp, p = self._with({"browser.screenshot": {"url": "http://localhost/", "title": "t", "png_b64": png}})
        desc = mock.AsyncMock(return_value="a login form")
        with p, mock.patch("core.small_model.describe_image_bytes", desc):
            out = run(browser_tools.tool_browser_screenshot({"question": "is it ok?"}))
        self.assertIn("a login form", out)
        self.assertIn(".agent/screens/", out)
        write = [c for c in comp.calls if c[0] == "fs.write_b64"][0][1]
        self.assertTrue(write["path"].replace("\\", "/").startswith("C:/proj/.agent/screens/"))
        self.assertTrue(desc.call_args.kwargs["force_local"])          # never a cloud model by default
        thumb = browser_tools.pop_thumbnail("browser_screenshot", {})
        self.assertTrue(thumb.startswith("data:image/jpeg;base64,"))
        self.assertIsNone(browser_tools.pop_thumbnail("browser_screenshot", {}))

    def test_old_companion_gets_upgrade_hint(self):
        comp, p = self._with({"browser.snapshot": RuntimeError("unknown op: browser.snapshot")})
        with p:
            out = run(browser_tools.tool_browser_snapshot({}))
        self.assertIn("v0.2.0", out)

    def test_console_lists_errors_and_failed_requests(self):
        comp, p = self._with({"browser.console": {"url": "u", "console": [{"type": "error", "text": "x is undefined"}],
                                                  "network": [{"method": "GET", "url": "/api", "status": 500}]}})
        with p:
            out = run(browser_tools.tool_browser_console({}))
        self.assertIn("x is undefined", out)
        self.assertIn("/api -> 500", out)


class DeviceToolTests(BrowserToolTests):
    def test_devices_lists_android_and_notes_ios_needs_mac(self):
        comp, p = self._with({
            "android.devices": {"devices": [{"serial": "emulator-5554", "state": "device", "model": "Pixel_7",
                                             "emulator": True}], "avds": ["Pixel_7"], "emulator": "emu.exe"},
            "ios.devices": RuntimeError("iOS simulators need a Mac with Xcode - run the companion on macOS"),
        })
        with p:
            out = run(device_tools.tool_mobile_devices({}))
        self.assertIn("emulator-5554", out)
        self.assertIn("Pixel_7", out)
        self.assertIn("needs the companion running on a Mac", out)

    def test_install_resolves_path_inside_the_project(self):
        comp, p = self._with({"android.install": {"serial": "s", "output": "Success"}})
        with p, mock.patch("core.agent_tools._ws_resolve", lambda x: self.ws / x):
            out = run(device_tools.tool_mobile_install({"path": "app/build/app-debug.apk"}))
        self.assertIn("Success", out)
        self.assertEqual(Path(comp.calls[0][1]["apk"]), self.ws / "app/build/app-debug.apk")

    def test_logs_masked(self):
        comp, p = self._with({"android.logcat": {"log": f"E/Pay: card={TEST_PAN}"}})
        with p:
            out = run(device_tools.tool_mobile_logs({"app_id": "com.x.app", "crash": True}))
        self.assertNotIn(TEST_PAN, out)
        self.assertTrue(comp.calls[0][1]["crash"])

    def test_platform_routes_to_ios_ops(self):
        comp, p = self._with({"ios.ui_dump": {"udid": "booted", "elements": 1, "tree": '[n1] Button "OK"'}})
        with p:
            out = run(device_tools.tool_mobile_ui({"platform": "ios"}))
        self.assertEqual(comp.calls[0][0], "ios.ui_dump")
        self.assertIn('Button "OK"', out)


class RegistrationTests(unittest.TestCase):
    def test_off_unless_enabled(self):
        from core.registry import registry
        with mock.patch.dict("core.small_model.APP_CONFIG", {"capabilities": {}}):
            registry.unregister("browser_navigate")
            browser_tools.register_browser_tools()
            self.assertIsNone(registry.get("browser_navigate"))
        with mock.patch.dict("core.small_model.APP_CONFIG", {"capabilities": {"browser": True, "mobile": True}}):
            browser_tools.register_browser_tools()
            device_tools.register_device_tools()
            self.assertIsNotNone(registry.get("browser_navigate"))
            self.assertIsNotNone(registry.get("mobile_ui"))


if __name__ == "__main__":
    unittest.main()
