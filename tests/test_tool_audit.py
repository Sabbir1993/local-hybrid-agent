"""Per-tool-call audit rows: locators kept, content hashed, PANs and secrets masked."""

import json
import unittest
from unittest import mock

from core.agent_loop import tool_audit

# Luhn-valid, publicly documented test number (not a real card), built so no literal PAN sits in source.
TEST_PAN = "4" + "1" * 15


class SummarizeArgs(unittest.TestCase):
    def test_content_is_measured_not_stored(self):
        out = tool_audit.summarize_args({"path": "src/a.py", "content": "print('secret body')"})
        self.assertEqual(out["path"], "src/a.py")
        self.assertEqual(out["content"]["len"], len("print('secret body')"))
        self.assertNotIn("secret body", json.dumps(out))
        self.assertEqual(len(out["content"]["sha256"]), 12)

    def test_edit_strings_are_measured(self):
        out = tool_audit.summarize_args({"path": "a.py", "old_string": "foo", "new_string": "bar"})
        self.assertNotIn("foo", json.dumps(out["old_string"]))
        self.assertNotIn("bar", json.dumps(out["new_string"]))

    def test_pan_in_a_locator_or_command_is_masked(self):
        out = tool_audit.summarize_args({"command": f"echo {TEST_PAN}", "query": f"card {TEST_PAN}"})
        blob = json.dumps(out)
        self.assertNotIn(TEST_PAN, blob)
        self.assertIn("****1111", blob)

    def test_secret_shaped_values_are_redacted(self):
        out = tool_audit.summarize_args(
            {"command": "curl -H 'Authorization: Bearer abcdef1234567890' --data token=hunter2xyz"})
        blob = json.dumps(out)
        self.assertNotIn("abcdef1234567890", blob)
        self.assertNotIn("hunter2xyz", blob)

    def test_long_values_and_many_keys_are_bounded(self):
        out = tool_audit.summarize_args({f"k{i}": "x" * 1000 for i in range(30)})
        self.assertLessEqual(len(out), 13)
        self.assertLess(len(out["k0"]), 200)
        self.assertEqual(out["_more_keys"], 18)

    def test_nested_values_are_typed_and_sized(self):
        out = tool_audit.summarize_args({"items": [1, 2, 3], "opts": {"a": "SENSITIVE"}})
        self.assertEqual(out["items"], {"type": "list", "len": 3})
        self.assertNotIn("SENSITIVE", json.dumps(out))

    def test_non_dict_args_do_not_raise(self):
        self.assertEqual(tool_audit.summarize_args(None), {})
        self.assertEqual(tool_audit.summarize_args("oops"), {})


class AuditToolCall(unittest.TestCase):
    def _call(self, **kw):
        with mock.patch.object(tool_audit, "audit_log") as al:
            user = object()
            base = dict(user=user, run_id="r1", name="write_file", args={"path": "a.py"}, ok=True)
            base.update(kw)
            tool_audit.audit_tool_call(**base)
            return al

    def test_success_row(self):
        al = self._call(step=3, device_id="dev-1", session_id=9)
        al.assert_called_once()
        _, kw = al.call_args
        self.assertEqual(kw["action"], "agent.tool")
        self.assertEqual(kw["resource"], "write_file")
        self.assertEqual(kw["result"], "allow")
        d = kw["detail"]
        self.assertEqual((d["run_id"], d["step"], d["device"], d["session_id"]), ("r1", 3, "dev-1", 9))
        self.assertEqual(d["args"], {"path": "a.py"})

    def test_failure_row_carries_error_code(self):
        al = self._call(ok=False, err_code="not_found")
        _, kw = al.call_args
        self.assertEqual(kw["result"], "error")
        self.assertEqual(kw["detail"]["error"], "not_found")

    def test_audit_failure_never_breaks_the_run(self):
        with mock.patch.object(tool_audit, "audit_log", side_effect=RuntimeError("db locked")):
            tool_audit.audit_tool_call(None, "r1", "grep", {"pattern": "x"}, True)


if __name__ == "__main__":
    unittest.main()
