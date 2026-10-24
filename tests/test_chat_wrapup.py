"""The tool-free wrap-up turn must never show tool markup or a provider's 'tool not available' message as the answer."""
import unittest

from routes.chat.wrapup import ToolTextFilter, finish_text, strip_markup


def stream(chunks):
    f = ToolTextFilter()
    shown = "".join(f.feed(c) for c in chunks) + f.flush()
    return shown, f.open


class Filter(unittest.TestCase):
    def test_plain_text_passes_unchanged(self):
        self.assertEqual(stream(["Hello ", "world, 2 < 3 and a<b."]), ("Hello world, 2 < 3 and a<b.", False))

    def test_a_call_split_across_chunks_is_never_shown(self):
        shown, opened = stream(["Done so far. ", "<", "tool", "_call>\n<function=doc_edit>", "<parameter=file>x"])
        self.assertEqual(shown, "Done so far. ")
        self.assertTrue(opened)

    def test_function_tag_without_tool_call_wrapper(self):
        shown, opened = stream(["ok <func", "tion=write_file>..."])
        self.assertEqual(shown, "ok ")
        self.assertTrue(opened)

    def test_a_held_back_less_than_sign_is_released_at_the_end(self):
        self.assertEqual(stream(["x <"])[0], "x <")


class Finish(unittest.TestCase):
    def test_provider_placeholder_becomes_the_status(self):
        msg = 'Tool "write_file" is not available in this request. Available tools: (none). Use one of them, or answer as plain text.'
        text, replaced = finish_text(msg, 30, ["loan.xlsx"])
        self.assertTrue(replaced)
        self.assertIn("30 steps", text)
        self.assertIn("loan.xlsx", text)
        self.assertIn("Continue", text)
        self.assertNotIn("not available", text)

    def test_raw_tool_call_becomes_the_status(self):
        raw = '<tool_call>\n<function=doc_edit>\n<parameter=ops>\n[{"op": "set_formula", "cell": "G'
        text, replaced = finish_text(raw, 15, [])
        self.assertTrue(replaced)
        self.assertNotIn("<function", text)

    def test_a_real_summary_is_kept_without_the_markup(self):
        raw = "The schedule sheet is built and the totals are in place. The ratio sheet still needs its formulas.\n<tool_call><function=doc_edit>"
        text, replaced = finish_text(raw, 15, [])
        self.assertTrue(replaced)
        self.assertTrue(text.startswith("The schedule sheet is built"))
        self.assertNotIn("<tool_call>", text)

    def test_a_clean_summary_is_untouched(self):
        raw = "Everything is done: the workbook has three sheets with live formulas."
        self.assertEqual(finish_text(raw, 15, []), (raw, False))

    def test_strip_handles_unfinished_calls(self):
        self.assertEqual(strip_markup("a <function=x><parameter=p>v"), "a")


if __name__ == "__main__":
    unittest.main()
