"""tests/test_webcheck.py - core/webcheck with the companion faked: one-call audit, flows that keep going
after a failed expect, saved reports and the run-to-run diff.

Run: python -m pytest tests/test_webcheck.py -q
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import browser_tools
from core.request_context import set_current_user
from core.webcheck import report as wc_report
from core.webcheck import tools as wc

TEST_PAN = "4" + "1" * 15      # standard test number, not a real card


def run(coro):
    return asyncio.run(coro)


def _issue(kind, msg, sev="error", sel=None):
    return {"kind": kind, "severity": sev, "message": msg, "selector": sel}


def _audit(issues, url="http://localhost:5173/"):
    return {"url": url, "title": "App", "device": "desktop", "issues": issues, "dropped": 0,
            "summary": {"errors": sum(i["severity"] == "error" for i in issues),
                        "warnings": sum(i["severity"] == "warning" for i in issues), "checked": {"images": 1}}}


class _Companion:
    """Replies per op (value, list consumed in order, or callable(params)); fs.* is an in-memory disk."""

    def __init__(self, replies):
        self.replies, self.calls, self.disk = replies, [], {}

    async def call(self, uid, op, params, timeout=60):
        self.calls.append((op, params))
        if op == "fs.write":
            self.disk[params["path"].replace("\\", "/")] = params["content"]
            return {}
        if op == "fs.write_b64":
            self.disk[params["path"].replace("\\", "/")] = params["data"]
            return {}
        if op == "fs.read":
            return {"content": self.disk.get(params["path"].replace("\\", "/"))}
        r = self.replies.get(op, {})
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if callable(r):
            r = r(params)
        if isinstance(r, Exception):
            raise r
        return r

    def ops(self, name):
        return [p for o, p in self.calls if o == name]


class WebCheckTests(unittest.TestCase):
    def setUp(self):
        set_current_user(7)
        self._dev = mock.patch("core.agent_tools.require_device_workspace", return_value=(7, Path("C:/proj")))
        self._dev.start()

    def tearDown(self):
        self._dev.stop()
        set_current_user(None)

    def _with(self, replies):
        comp = _Companion(replies)
        return comp, mock.patch.object(browser_tools.companion_bridge, "call", comp.call)

    NAV = {"url": "http://localhost:5173/", "title": "App", "status": 200, "snapshot": '- heading "Home"'}

    def test_audit_reports_issues_and_masks_card_numbers(self):
        issues = [_issue("console_error", f"boom {TEST_PAN}"), _issue("broken_image", "img#logo failed", "warning", "img#logo")]
        comp, p = self._with({"browser.navigate": self.NAV, "browser.audit": _audit(issues)})
        with p:
            out = run(wc.tool_browser_audit({"url": "http://localhost:5173/", "screenshot": False}))
        self.assertIn("1 errors, 1 warnings", out)
        self.assertIn("[broken_image] img#logo failed (img#logo)", out)
        self.assertNotIn(TEST_PAN, out)
        self.assertTrue(comp.ops("browser.navigate")[0]["headless"])          # audits are headless by default
        self.assertEqual(comp.ops("browser.audit")[0]["screenshot"], False)

    def test_audit_of_a_page_that_will_not_load(self):
        comp, p = self._with({"browser.navigate": {"url": "http://localhost:1/", "title": "", "error": "net::ERR_CONNECTION_REFUSED"}})
        with p:
            out = run(wc.tool_browser_audit({"url": "http://localhost:1/"}))
        self.assertIn("load_failed", out)
        self.assertEqual(comp.ops("browser.audit"), [])

    def test_old_companion_gets_upgrade_hint(self):
        comp, p = self._with({"browser.navigate": self.NAV, "browser.audit": RuntimeError("unknown op: browser.audit")})
        with p:
            out = run(wc.tool_browser_audit({"url": "http://localhost:5173/"}))
        self.assertIn("v0.2.0", out)

    def test_failed_expect_is_recorded_and_the_flow_continues(self):
        flow = {"name": "login", "steps": [
            {"do": "navigate", "url": "/login"},
            {"do": "expect", "contains": "Welcome"},          # not on the page
            {"do": "expect", "contains": "Home"},             # still evaluated
        ]}
        comp, p = self._with({"browser.navigate": self.NAV, "browser.audit": _audit([])})
        with p:
            out = run(wc.tool_browser_check({"base_url": "http://localhost:5173", "pages": [], "flows": [flow]}))
        self.assertIn('expected "Welcome"', out)
        self.assertNotIn('expected "Home"', out)
        self.assertIn("✗ login", out)
        self.assertEqual(comp.ops("browser.navigate")[-1]["url"], "http://localhost:5173/login")

    def test_throwing_step_stops_that_flow_but_not_the_next(self):
        def click(params):
            raise RuntimeError("element not found")
        flows = [{"name": "broken", "steps": [{"do": "navigate", "url": "/"}, {"do": "click", "text": "Nope"},
                                               {"do": "expect", "contains": "never reached"}]},
                 {"name": "fine", "steps": [{"do": "navigate", "url": "/"}, {"do": "expect", "contains": "Home"}]}]
        comp, p = self._with({"browser.navigate": self.NAV, "browser.click": click, "browser.audit": _audit([])})
        with p:
            out = run(wc.tool_browser_check({"base_url": "http://localhost:5173", "pages": ["/"], "flows": flows}))
        self.assertIn("✗ broken: step 2 (click): element not found", out)
        self.assertIn("✓ fine", out)
        self.assertNotIn("never reached", out)
        sessions = {c["session"] for c in comp.ops("browser.navigate")}
        self.assertEqual(len(sessions), 3)                      # pages + one session per flow
        self.assertEqual(len(comp.ops("browser.close")), 3)     # every session released

    def test_password_refusal_fails_the_flow_with_the_reason(self):
        def typ(params):
            raise RuntimeError("refused: the agent never types into password fields")
        flow = {"name": "login", "steps": [{"do": "navigate", "url": "/"}, {"do": "type", "selector": "#pw", "text": "x"}]}
        comp, p = self._with({"browser.navigate": self.NAV, "browser.type": typ, "browser.audit": _audit([])})
        with p:
            out = run(wc.tool_browser_check({"base_url": "http://localhost:5173", "pages": [], "flows": [flow]}))
        self.assertIn("never types into password", out)

    def test_second_run_shows_new_and_fixed(self):
        old = _issue("console_error", "x is not a function")
        new = _issue("http_error", "GET /api/user -> 500")
        comp, p = self._with({"browser.navigate": self.NAV,
                              "browser.audit": [_audit([old]), _audit([new])]})
        with p, mock.patch("core.webcheck.store.new_run_id", side_effect=["run-1", "run-2"]):
            first = run(wc.tool_browser_check({"base_url": "http://localhost:5173"}))
            second = run(wc.tool_browser_check({"base_url": "http://localhost:5173"}))
            again = run(wc.tool_browser_check_report({"run_id": "run-2", "compare_to": "run-1"}))
        self.assertNotIn("Since last run", first)
        self.assertIn("report saved: .agent/webcheck/run-1/report.md", first)
        self.assertIn("+ new: [http_error] GET /api/user -> 500", second)
        self.assertIn("- fixed: [console_error] x is not a function", second)
        self.assertIn("+ new: [http_error]", again)
        self.assertTrue(any(k.endswith(".agent/webcheck/run-2/report.json") for k in comp.disk))

    def test_report_on_device_is_masked_and_bad_run_ids_refused(self):
        comp, p = self._with({"browser.navigate": self.NAV, "browser.audit": _audit([_issue("console_error", f"card {TEST_PAN}")])})
        with p:
            run(wc.tool_browser_check({"base_url": "http://localhost:5173"}))
            out = run(wc.tool_browser_check_report({"run_id": "../../etc/passwd"}))
        self.assertIn("no report", out)
        md = [v for k, v in comp.disk.items() if k.endswith("report.md")][0]
        self.assertNotIn(TEST_PAN, md)

    def test_limits_from_config(self):
        comp, p = self._with({"browser.navigate": self.NAV, "browser.audit": _audit([])})
        with p, mock.patch.dict("core.webcheck.constants.APP_CONFIG", {"webcheck": {"max_pages": 2}}):
            run(wc.tool_browser_check({"base_url": "http://localhost:5173", "pages": ["/a", "/b", "/c", "/d"]}))
        self.assertEqual(len(comp.ops("browser.audit")), 2)


class ReportTests(unittest.TestCase):
    def test_diff_is_none_without_a_previous_run(self):
        self.assertIsNone(wc_report.diff_reports(None, {"pages": []}))

    def test_clean_run_summary(self):
        rep = {"run_id": "r", "base_url": "u", "pages": [{"path": "/", "issues": [], "summary": {"errors": 0, "warnings": 0}}], "flows": []}
        rep["summary"] = wc_report.summarize(rep)
        self.assertIn("0 errors, 0 warnings", wc_report.render_markdown(rep))


class RegistrationTests(unittest.TestCase):
    def test_tools_register_with_the_browser_family_and_executor(self):
        from core.registry import registry
        from routes.agent.constants import EXECUTOR_TEST_TOOLS, PARALLEL_READ_TOOLS
        with mock.patch.dict("core.small_model.APP_CONFIG", {"capabilities": {"browser": True}}):
            browser_tools.register_browser_tools()
        for name in ("browser_audit", "browser_check", "browser_check_report"):
            self.assertIsNotNone(registry.get(name))
            self.assertIn(name, EXECUTOR_TEST_TOOLS)
            self.assertNotIn(name, PARALLEL_READ_TOOLS)

    def test_audit_is_not_a_confirm_op(self):
        from core.companion_bridge.constants import CONFIRM_OPS
        self.assertNotIn("browser.audit", CONFIRM_OPS)


if __name__ == "__main__":
    unittest.main()
