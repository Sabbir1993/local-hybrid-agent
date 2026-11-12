"""Project builder recovery: a complete rewrite after a partial / failed first try replaces the file instead of being refused,
code is not flagged as a card number, and project_finish works without a plan.

Run: python -X utf8 -m unittest tests.test_project_files_recovery -v
"""
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.agent_tools  # noqa: F401
from core.agent_tools import project_files as pf

PARTIAL = "<!DOCTYPE html>\n<html>\n<head>\n<title>3D</title>\n</head>\n<body>\n<canvas id=\"c\"></canvas>\n<script>\nconst a = 1;\nconst b = 2;\nconst c = 3;\nconst d = 4;\n"
PAGE = "<!DOCTYPE html>\n<html>\n<head><title>3D</title></head>\n<body>\n<canvas id=\"c\"></canvas>\n<script>\nconst a = 1;\n</script>\n</body>\n</html>\n"
P = "bike-3d"


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        for patcher in (mock.patch.object(core.agent_tools, "COMMON_ROOT", Path(self.td.name)),
                        mock.patch.object(sys.modules["core.agent_tools.workspace"], "get_current_user_id", return_value=7)):
            patcher.start()
            self.addCleanup(patcher.stop)
        pf.set_scope(3)
        self.addCleanup(pf.reset_scope)
        self.root = Path(self.td.name) / "user_7"
        pf.tool_project_plan({"project": P, "files": [{"path": "index.html", "purpose": "page"}]})

    def write(self, content, path="index.html", **kw):
        return pf.tool_project_write({"project": P, "path": path, "content": content, **kw})

    def append(self, content, path="index.html"):
        return pf.tool_project_append({"project": P, "path": path, "content": content})

    @property
    def index(self):
        return self.root / "projects/s3/bike-3d/index.html"


class Replace(Base):
    def test_complete_page_replaces_a_partial_one(self):
        self.assertTrue(self.write(PARTIAL).startswith("Created"))
        r = self.write(PAGE)
        self.assertTrue(r.startswith("Replaced index.html"), r)
        self.assertIn("replaced the earlier version", r)
        self.assertNotIn("error", r)
        self.assertEqual(self.index.read_text(), PAGE)
        self.assertEqual(self.index.with_name("index.html.prev").read_text(), PARTIAL)
        out = pf.tool_project_finish({"project": P})
        self.assertIn("[DOWNLOAD:", out)
        self.assertEqual(pf._project_files(self.index.parent), [self.index])
        self.assertEqual(pf._size(self.index.parent)[0], 1)

    def test_prev_is_not_in_the_zip(self):
        self.write(PARTIAL)
        self.write(PAGE)
        self.write("body{margin:0}", path="css/styles.css")
        out = pf.tool_project_finish({"project": P})
        zip_name = next(n for n in re.findall(r"\[DOWNLOAD: ([^\]]+)\]", out) if n.endswith(".zip"))
        with zipfile.ZipFile(self.root / zip_name) as z:
            self.assertEqual(sorted(z.namelist()), ["bike-3d/css/styles.css", "bike-3d/index.html"])

    def test_identical_resend_is_a_note_not_an_error(self):
        self.write(PAGE)
        r = self.write(PAGE)
        self.assertTrue(r.startswith("note:"), r)
        self.assertIn("project_finish", r)
        self.assertFalse(self.index.with_name("index.html.prev").exists())

    def test_a_fragment_is_still_refused_with_a_next_step(self):
        self.write(PAGE)
        r = self.write("<p>just a bit</p>")
        self.assertTrue(r.startswith("error:"), r)
        self.assertIn("replace=true", r)
        self.assertEqual(self.index.read_text(), PAGE)
        self.assertTrue(self.write("<p>x</p>", replace=True).startswith("Replaced"))

    def test_other_types_need_to_be_nearly_as_long(self):
        long_js = "\n".join(f"const v{i} = {i};" for i in range(30)) + "\n"
        self.write(long_js, path="js/a.js")
        self.assertTrue(self.write("const v = 1;\n", path="js/a.js").startswith("error:"))
        self.assertTrue(self.write(long_js + "const z = 1;\n", path="js/a.js").startswith("Replaced"))


class Append(Base):
    def test_append_to_missing_file_creates_it(self):
        r = self.append("body{margin:0}\n", path="css/styles.css")
        self.assertTrue(r.startswith("Created css/styles.css"), r)
        self.assertEqual((self.index.parent / "css/styles.css").read_text(), "body{margin:0}\n")

    def test_fenced_and_prose_wrapped_page_replaces(self):
        self.write(PARTIAL)
        r = self.append("Here is the complete page:\n```html\n" + PAGE + "```\nHope it helps!")
        self.assertTrue(r.startswith("Replaced index.html"), r)
        self.assertEqual(self.index.read_text().strip(), PAGE.strip())
        self.assertFalse(self.index.read_text().startswith("Here"))

    def test_fenced_fragment_is_unfenced(self):
        self.write(PARTIAL)
        self.append("```js\nconst e = 5;\n```")
        self.assertNotIn("```", self.index.read_text())
        self.assertIn("const e = 5;", self.index.read_text())


class CodeIsNotACard(Base):
    CODE = ("// the cardinal direction, the card layout\n"
            "const MASK = 0x7fffffffffffffff;\n"
            "const seed = 1234567890123456; // card seed\n"
            "const card = [0xdeadbeefcafebabe, 4000000000000002001];\n"
            "const pi = 3.141592653589793238;\n"
            "const id = abc4111111111111111;\n")

    def test_code_with_card_words_and_long_numbers_is_accepted(self):
        for path in ("index.html", "js/main.js", "css/s.css"):
            body = "<script>\n" + self.CODE + "</script>" if path.endswith("html") else self.CODE
            r = self.write(body, path=path)
            self.assertFalse(r.startswith("error"), (path, r))

    def test_real_card_number_in_html_is_refused(self):
        for pan in ("4111 1111 1111 1111", "4111111111111111", "4111-1111-1111-1111"):
            r = self.write(f"<p>pay with {pan} now</p>")
            self.assertTrue(r.startswith("error: write refused"), (pan, r))
            r = self.write(f"const n = '{pan}';", path="js/a.js")
            self.assertTrue(r.startswith("error: write refused"), (pan, r))
        self.assertTrue(self.write("<p>card number 4111 1111 1111 1111</p>").startswith("error: write refused"))

    def test_text_files_keep_the_context_check(self):
        r = self.write("my card 4111 1111 1111 1112 expires soon", path="notes.md")
        self.assertTrue(r.startswith("error: write refused"), r)


class Finish(Base):
    def test_unbalanced_page_without_plan_finishes(self):
        pf._plan_path(P).unlink()
        raw = "<!DOCTYPE html>\n<html>\n<body>\n<div class=\"a\">\n<p>hello\n<script>\nlet x = 1;\n"
        self.index.parent.mkdir(parents=True, exist_ok=True)
        self.index.write_text(raw, encoding="utf-8")
        out = pf.tool_project_finish({"project": P})
        self.assertIn("[DOWNLOAD:", out)
        self.assertFalse(out.startswith("error"), out)
        txt = self.index.read_text()
        self.assertEqual(txt.count("<script"), txt.count("</script>"))
        self.assertIn("</div>", txt)
        self.assertTrue(txt.rstrip().endswith("</html>"))

    def test_finish_if_possible(self):
        self.assertIsNone(pf.finish_if_possible())
        self.write(PARTIAL)
        out = pf.finish_if_possible()
        self.assertIsNotNone(out)
        self.assertIn("[DOWNLOAD:", out)
        self.assertIsNone(pf.finish_if_possible())              # already finished: nothing left to do
        self.write(PAGE)
        self.assertIsNotNone(pf.finish_if_possible(P))          # changed after the finish: finish again

    def test_finish_if_possible_with_planned_files_missing_and_no_page(self):
        pf.tool_project_plan({"project": P, "files": [{"path": "index.html", "purpose": "page"}, {"path": "a.js", "purpose": "js"}]})
        self.write("let a = 1;\n", path="a.js")
        out = pf.finish_if_possible()
        self.assertIn("[DOWNLOAD:", out)
        self.assertIn("index.html", out)


if __name__ == "__main__":
    unittest.main()
