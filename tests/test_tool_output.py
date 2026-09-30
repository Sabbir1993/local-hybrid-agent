import unittest

from core.agent_loop.tool_output import DEFAULT_CAP, MIN_CAP, cap_from_config, cap_tool_result


class CapToolResultTests(unittest.TestCase):
    def test_short_and_non_text_results_are_untouched(self):
        self.assertEqual(cap_tool_result("ok", 100), "ok")
        self.assertEqual(cap_tool_result("x" * 100, 100), "x" * 100)
        self.assertEqual(cap_tool_result({"a": 1}, 100), {"a": 1})
        self.assertEqual(cap_tool_result(None, 100), None)

    def test_long_result_keeps_head_and_tail_and_says_what_was_cut(self):
        text = "HEAD-" + "m" * 50000 + "-TAIL"
        out = cap_tool_result(text, 2000)
        self.assertTrue(out.startswith("HEAD-"))
        self.assertTrue(out.endswith("-TAIL"))
        self.assertLess(len(out), 2300)
        self.assertIn("characters omitted", out)
        self.assertIn("read_file_chunk", out)

    def test_the_omitted_count_is_exact(self):
        text = "a" * 10000
        out = cap_tool_result(text, 2000)
        n = int(out.split("[")[1].split(" characters")[0])
        kept = len(out.split("\n... [")[0]) + len(out.split("] ...\n")[1])
        self.assertEqual(kept + n, 10000)

    def test_capping_is_idempotent(self):
        once = cap_tool_result("z" * 30000, 3000)
        self.assertEqual(cap_tool_result(once, 3000), once)   # a stored result is never rewritten

    def test_config(self):
        self.assertEqual(cap_from_config(None), DEFAULT_CAP)
        self.assertEqual(cap_from_config({"tool_result_chars": 0}), 0)
        self.assertEqual(cap_tool_result("x" * 999999, 0), "x" * 999999)     # 0 = off
        self.assertEqual(cap_from_config({"tool_result_chars": 50}), MIN_CAP)   # never cut ordinary results
        self.assertEqual(cap_from_config({"tool_result_chars": "abc"}), DEFAULT_CAP)


if __name__ == "__main__":
    unittest.main()
