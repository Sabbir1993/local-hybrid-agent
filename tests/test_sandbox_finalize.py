import pathlib
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


class MainPathAlwaysEmitsTextTests(unittest.TestCase):
    """Live eval caught completed runs with zero `delta` events: the model ended
    right after tool results and the main finalization path only emitted text when
    the validator said `synthesized`. Guard the invariant at the source level."""

    SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "agent" / "stream.py"

    def test_both_finalization_paths_key_off_actually_emitted_text(self):
        src = self.SRC.read_text(encoding="utf-8")
        # final_content comes from res_dict; delta comes from streamed_content. Keying
        # off the former left completed runs with zero visible text.
        self.assertEqual(src.count('emitted_answer = "".join(str(c or "") for c in (streamed_content or []))'), 2)
        self.assertEqual(src.count('if not emitted_answer.strip():'), 2)
        self.assertIn("Task completed. All requested operations have been processed in the workspace.", src)

    def test_answer_check_sees_the_emitted_text(self):
        src = self.SRC.read_text(encoding="utf-8")
        self.assertIn("last_query, out_text,", src)
        self.assertIn("last_query, answer_text,", src)


if __name__ == "__main__":
    unittest.main()
