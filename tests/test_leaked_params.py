"""A badly parsed XML tool call must not put a whole document into the file name (seen: Errno 22 on a doc_create path)."""
import unittest

from core.agent_loop.repair import split_leaked_params, validate_and_repair_tool_args


class LeakedParams(unittest.TestCase):
    LEAK = "loan.xlsx\n</paramparameter>\n<parameter=content>\n# Loan\n| a | b |\n"

    def test_content_is_given_back_to_its_own_argument(self):
        out = split_leaked_params({"path": self.LEAK})
        self.assertEqual(out["path"], "loan.xlsx")
        self.assertTrue(out["content"].startswith("# Loan"))

    def test_a_content_that_was_sent_properly_is_kept(self):
        out = split_leaked_params({"path": self.LEAK, "content": "real"})
        self.assertEqual(out["content"], "real")

    def test_clean_args_are_unchanged(self):
        args = {"path": "a/b.md", "content": "x < y and a > b"}
        self.assertEqual(split_leaked_params(args), args)

    def test_validate_repairs_then_accepts(self):
        args, err = validate_and_repair_tool_args("write_file", {"path": self.LEAK})
        self.assertIsNone(err)
        self.assertEqual(args["path"], "loan.xlsx")

    def test_a_path_with_line_breaks_gets_a_short_error(self):
        _, err = validate_and_repair_tool_args("doc_create", {"path": "a.xlsx\nnot a name", "content": "x"})
        self.assertIn("is not a file name", err)
        self.assertLess(len(err), 400)

    def test_windows_paths_still_pass(self):
        _, err = validate_and_repair_tool_args("read_file", {"path": "C:\\Users\\me\\notes.txt"})
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()
