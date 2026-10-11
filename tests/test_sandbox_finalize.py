import unittest

from core.agent_loop.sandbox import validate_and_finalize_response


def run(result):
    return [{"name": "run_python", "ok": True, "args": {}, "result": result}]


class FinalizeFailedRunTests(unittest.TestCase):
    def test_failed_script_is_not_announced_as_success(self):
        text, _, note = validate_and_finalize_response(
            "fix it", "", "", run("exit code 1\n--- stderr ---\nSyntaxError: invalid syntax"))
        self.assertNotIn("executed successfully", text)
        self.assertIn("failed", text)
        self.assertIn("failed run", note)

    def test_clean_script_still_reports_success(self):
        text, _, _ = validate_and_finalize_response("run it", "", "", run("exit code 0\nhello"))
        self.assertIn("executed successfully", text)


if __name__ == "__main__":
    unittest.main()
