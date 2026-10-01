import json
import unittest

from core.agent_loop import safe_parse_and_repair_args, validate_and_repair_tool_args, was_cut_off
from core.agent_loop.repair import INVALID_JSON_KEY
from core.agent_loop.truncation import INVALID_ARGS_ERROR


class CutOffCallTests(unittest.TestCase):
    def test_cut_off_write_file_is_not_salvaged(self):
        raw = '{"path": "app.py", "content": "def main():\n    print(1)\n    x = ['
        args = safe_parse_and_repair_args(raw, "write_file")
        self.assertTrue(args.get(INVALID_JSON_KEY))
        self.assertNotIn("content", args)
        _, err = validate_and_repair_tool_args("write_file", args)
        self.assertEqual(err, INVALID_ARGS_ERROR)

    def test_cut_off_edit_file_is_not_salvaged(self):
        raw = '{"path": "a.py", "old_string": "foo", "new_string": "ba'
        args = safe_parse_and_repair_args(raw, "edit_file")
        self.assertTrue(args.get(INVALID_JSON_KEY))
        _, err = validate_and_repair_tool_args("edit_file", args)
        self.assertEqual(err, INVALID_ARGS_ERROR)

    def test_valid_write_file_still_parses(self):
        raw = json.dumps({"path": "a.py", "content": "x = 1\n"})
        args = safe_parse_and_repair_args(raw, "write_file")
        self.assertEqual(args, {"path": "a.py", "content": "x = 1\n"})

    def test_html_chunk_is_not_padded_with_closing_tags(self):
        chunk = "<div>part of a bigger page</div>\n<script>var a = 1;"
        args = safe_parse_and_repair_args({"path": "i.html", "content": chunk}, "write_file")
        self.assertEqual(args["content"], chunk)

    def test_other_tools_keep_the_repair(self):
        args = safe_parse_and_repair_args('{"path": "a.py"', "read_file")
        self.assertEqual(args.get("path"), "a.py")

    def test_was_cut_off(self):
        self.assertTrue(was_cut_off({"finish_reason": "length"}))
        self.assertFalse(was_cut_off({"finish_reason": "stop"}))
        self.assertFalse(was_cut_off(None))


if __name__ == "__main__":
    unittest.main()
