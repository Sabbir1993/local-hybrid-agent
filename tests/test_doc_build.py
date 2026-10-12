"""Documents built in pieces: doc_create(total=N) then doc_append rebuilds ONE file in place (chat / common space)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import agent_tools, doc_store, doc_tools  # noqa: E402
from core.request_context import set_current_user  # noqa: E402
from tests.test_doc_ops import doc_files_db  # noqa: E402

FIRST = ("Theme: forest\n# Cyber Basics\nPlain guide\n---\n## What is it\n- Protecting data\n- Protecting devices\n"
         "---\n## Why it matters\n- Breaches cost money\n- Trust is lost\n")


def _name(out: str) -> str:
    return out.split("[DOWNLOAD: ")[1].split("]")[0]


class BuildInPiecesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._patches = [mock.patch.object(agent_tools, "COMMON_ROOT", self.root),
                         mock.patch.object(doc_store, "_projects_db", doc_files_db())]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        set_current_user(None)
        self.tmp.cleanup()

    def test_deck_grows_in_one_file(self):
        set_current_user(6)
        out = doc_tools.tool_doc_create_common({"file": "cyber.pptx", "content": FIRST, "total": 6})
        name = _name(out)
        self.assertIn("doc_append", out)
        res = doc_tools.tool_doc_append_common({"file": name, "content": "## Threats\n- Phishing\n- Malware\n"
                                                "---\n## Passwords\n- Long\n- Unique\n"})
        self.assertIn("now 5 slides of 6", res, res)
        self.assertIn("Continue", res)
        res = doc_tools.tool_doc_append_common({"file": name, "content": "## Thank you\n- Questions?\n", "done": True})
        self.assertIn("Finished", res, res)
        from pptx import Presentation
        self.assertEqual(len(Presentation(str(self.root / "user_6" / name)).slides), 6)
        self.assertEqual([f.name for f in (self.root / "user_6").iterdir()], [name])      # one file, no -vN copies
        self.assertEqual(doc_store.find(6, name)["source_spec"].count("---"), 5)

    def test_early_done_and_non_documents_are_refused(self):
        set_current_user(7)
        out = doc_tools.tool_doc_create_common({"file": "d.pptx", "total": 5, "content": "# T\nsub\n---\n## A\n- x\n- y\n"})
        res = doc_tools.tool_doc_append_common({"file": _name(out), "content": "## B\n- z\n- w\n", "done": True})
        self.assertTrue(res.startswith("doc error: not finished"), res)
        agent_tools.tool_write_file_common({"path": "t.csv", "content": "a,b\n1,2"})
        csv = next(f.name for f in (self.root / "user_7").iterdir() if f.suffix == ".csv")
        self.assertIn("doc error", doc_tools.tool_doc_append_common({"file": csv, "content": "x"}))

    def test_pdf_is_printed_once_at_the_end(self):
        set_current_user(8)
        out = doc_tools.tool_doc_create_common({"file": "memo.pdf", "content": "# Memo\n\n## One\nfirst\n", "total": 2})
        name = _name(out)
        path = self.root / "user_8" / name
        before = path.read_bytes()
        res = doc_tools.tool_doc_append_common({"file": name, "content": "## Two\nsecond\n"})
        self.assertIn("sections", res)
        self.assertEqual(path.read_bytes(), before)                                      # draft only: not re-printed
        self.assertIn("second", doc_store.find(8, name)["source_spec"])
        res = doc_tools.tool_doc_append_common({"file": name, "content": "", "done": True})
        self.assertIn("Finished", res, res)
        self.assertNotEqual(path.read_bytes(), before)                                   # printed on done


class BriefIntentTests(unittest.TestCase):
    def test_pdf_brief_intent(self):
        from core import prompt_scope as ps
        for q in ("make a PDF guide about cyber security", "generate a pdf report on sales"):
            self.assertTrue(ps.pdf_brief_wanted(q, []), q)
        for q in ("what is a pdf", "summarize this pdf", "make a PPT about cats", "create a pdf presentation on x", "hi"):
            self.assertFalse(ps.pdf_brief_wanted(q, []), q)
        card = {"role": "assistant", "content": '```questionnaire\n{"title":"A few quick choices for your PDF",'
                                                '"questions":[{"q":"How many sections?"}]}\n```'}
        self.assertTrue(ps.pdf_brief_wanted("Short, executives", [card]))
        self.assertFalse(ps.style_wanted("Short, executives, friendly tone please", [card]),
                         "our card has its own brief, not the advice-style one")

    def test_deck_follow_up_is_recognised(self):
        from core import prompt_scope as ps
        self.assertIsNotNone(ps._PPTX_RE.search("your deck"))      # was a pattern with backspace characters, never matched


if __name__ == "__main__":
    unittest.main()
