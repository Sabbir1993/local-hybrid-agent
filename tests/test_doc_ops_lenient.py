"""doc_edit ops arrive as a JSON string written by a model: markdown inside it must not make the whole edit fail."""
import unittest

from core import doc_ops
from core.doc_ops.base import DocOpError

TABLE = "| Pillar | Focus |\n| --- | --- |\n| Scope \\_ Strategy | KPI \\* growth |"


def one(text):
    ops = doc_ops.normalize_ops(text)
    assert len(ops) == 1, ops
    return ops[0]


class LenientOps(unittest.TestCase):
    def test_a_clean_list_is_untouched(self):
        o = one(r'[{"op": "set_text", "addr": "a1", "text": "x\ny \"q\" \\ \u00e9"}]')
        self.assertEqual(o["text"], 'x\ny "q" \\ \u00e9')                # valid escapes are never altered

    def test_raw_newlines_inside_a_string_are_accepted(self):
        o = one('[{"op": "add_outline_sheet", "name": "Scope", "content": "' + TABLE.replace("\\", "\\\\") + '"}]')
        self.assertIn("| Pillar | Focus |\n", o["content"])
        raw = '[{"op": "add_outline_sheet", "name": "Scope", "content": "line one\nline two"}]'   # a real line break
        self.assertEqual(one(raw)["content"], "line one\nline two")

    def test_markdown_escapes_that_are_not_json_escapes_are_kept_as_text(self):
        text = '[{"op": "add_outline_sheet", "name": "S", "content": "' + TABLE.replace("\n", "\\n") + '"}]'
        o = one(text)
        self.assertIn("Scope \\_ Strategy", o["content"])
        self.assertIn("KPI \\* growth", o["content"])
        self.assertEqual(o["content"].count("\n"), 2)                     # the \n escapes still mean line breaks

    def test_a_valid_escaped_backslash_before_a_character_is_not_doubled(self):
        o = one(r'[{"op": "set_text", "addr": "a1", "text": "C:\\temp\\_x"}]')
        self.assertEqual(o["text"], "C:\\temp\\_x")

    def test_trailing_comma_code_fence_and_surrounding_prose(self):
        self.assertEqual(one('[{"op": "set_text", "addr": "a1", "text": "x",},]')["text"], "x")
        self.assertEqual(one('```json\n[{"op": "set_text", "addr": "a1", "text": "x"}]\n```')["text"], "x")
        self.assertEqual(one('Here are the ops: [{"op": "set_text", "addr": "a1", "text": "x"}] done.')["text"], "x")

    def test_a_list_encoded_twice_is_decoded(self):
        import json
        inner = json.dumps([{"op": "set_text", "addr": "a1", "text": "x"}])
        self.assertEqual(one(json.dumps(inner))["text"], "x")

    def test_a_single_object_still_works(self):
        self.assertEqual(one('{"op": "set_text", "addr": "a1", "text": "x"}')["op"], "set_text")

    def test_a_cut_off_list_is_refused_with_what_went_wrong_and_not_applied(self):
        with self.assertRaises(DocOpError) as cm:
            doc_ops.normalize_ops('[{"op": "add_outline_sheet", "name": "Scope", "content": "| Pillar | Fo')
        msg = str(cm.exception)
        self.assertIn("not valid JSON", msg)
        self.assertIn("line 1", msg)                                       # where the parser gave up
        self.assertIn("Do not recreate the document", msg)

    def test_a_list_without_op_fields_is_still_refused(self):
        with self.assertRaises(DocOpError):
            doc_ops.normalize_ops('[{"name": "x"}]')


if __name__ == "__main__":
    unittest.main()
