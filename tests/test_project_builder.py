"""Chat project builder: a website written file by file (project_write / project_append), finished as a ZIP + one-file preview.

Run: python -X utf8 -m unittest tests.test_project_builder -v
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

INDEX = ('<!doctype html><html><head><link rel="stylesheet" href="css/styles.css"></head>'
         '<body><h1>Dhaka</h1><script src="js/main.js"></script></body></html>')


class ProjectBuilder(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        for patcher in (mock.patch.object(core.agent_tools, "COMMON_ROOT", Path(self.td.name)),
                        mock.patch.object(sys.modules["core.agent_tools.workspace"], "get_current_user_id", return_value=7)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = Path(self.td.name) / "user_7"

    def w(self, path, content, project="Dhaka Site"):
        """project_write with the file added to the plan first (as the model must do), so older tests stay about one thing."""
        try:
            rel = pf._file(project, path)[2]
            cur = pf._load_plan(project)
            if rel not in {f["path"] for f in cur}:
                pp = pf._plan_path(project)
                pp.parent.mkdir(parents=True, exist_ok=True)
                import json
                pp.write_text(json.dumps({"files": cur + [{"path": rel, "purpose": "", "min_lines": 1}]}), encoding="utf-8")
        except ValueError:
            pass
        return pf.tool_project_write({"project": project, "path": path, "content": content})

    def test_intent(self):
        self.assertTrue(pf.project_wanted("BUILD AN INTERACTIVE DHAKA WEBSITE ... index.html ... css/styles.css"))
        self.assertTrue(pf.project_wanted("make me a landing page"))
        self.assertFalse(pf.project_wanted("write a quarterly report as pdf"))
        self.assertFalse(pf.project_wanted("fill this excel sheet"))

    def test_write_append_finish_makes_zip_and_inlined_preview(self):
        self.assertTrue(self.w("index.html", INDEX).startswith("Created index.html"))
        self.w("css/styles.css", "body{margin:0}")
        self.w("js/main.js", "console.log(1)")
        r = pf.tool_project_append({"project": "Dhaka Site", "path": "css/styles.css", "content": "h1{color:red}"})
        self.assertTrue(r.startswith("Appended to css/styles.css"), r)
        out = pf.tool_project_finish({"project": "Dhaka Site"})
        names = re.findall(r"\[DOWNLOAD: ([^\]]+)\]", out)
        self.assertEqual(len(names), 2, out)
        zip_name = next(n for n in names if n.endswith(".zip"))
        page_name = next(n for n in names if n.endswith(".html"))
        with zipfile.ZipFile(self.root / zip_name) as z:
            self.assertEqual(sorted(z.namelist()), ["dhaka-site/css/styles.css", "dhaka-site/index.html", "dhaka-site/js/main.js"])
            self.assertEqual(z.read("dhaka-site/css/styles.css").decode(), "body{margin:0}\nh1{color:red}")
        page = (self.root / page_name).read_text(encoding="utf-8")
        self.assertIn("<style>", page)
        self.assertIn("h1{color:red}", page)
        self.assertIn("console.log(1)", page)
        self.assertNotIn('href="css/styles.css"', page)
        self.assertNotIn("WARNING", out)

    def test_missing_linked_file_is_reported(self):
        self.w("index.html", INDEX)
        out = pf.tool_project_finish({"project": "Dhaka Site"})
        self.assertIn("WARNING", out)
        self.assertIn("css/styles.css", out)

    def test_paths_cannot_escape_or_be_programs(self):
        for bad in ("../x.html", "C:/x.html", "a/../../x.html"):
            self.assertTrue(self.w(bad, "<p>x</p>").startswith("error:"), bad)
        self.assertIn("text files only", self.w("run.sh", "echo hi"))
        self.assertIn("text files only", self.w("logo.png", "x"))
        self.assertEqual([f for f in self.root.rglob("*") if f.is_file()], [])
        self.assertTrue(self.w("/etc/passwd.html", "<p>x</p>").startswith("Created etc/passwd.html"))   # absolute = relative to the project

    def test_empty_body_and_missing_target_are_errors(self):
        self.assertTrue(self.w("index.html", "  \n").startswith("error:"))
        r = pf.tool_project_append({"project": "p", "path": "index.html", "content": "<p>x</p>"})
        self.assertTrue(r.startswith("error:"), r)
        self.assertTrue(pf.tool_project_finish({"project": "nothing"}).startswith("error:"))

    def test_card_numbers_are_refused(self):
        r = self.w("index.html", "<p>pay with 4111 1111 1111 1111</p>")
        self.assertTrue(r.startswith("error: write refused"), r)

    def test_projects_are_separate_per_name(self):
        self.w("index.html", "<p>a</p>", project="one")
        self.w("index.html", "<p>b</p>", project="two")
        self.assertEqual((self.root / "projects/one/index.html").read_text(), "<p>a</p>")
        self.assertEqual((self.root / "projects/two/index.html").read_text(), "<p>b</p>")


class ChatWiring(unittest.TestCase):
    def test_chat_offers_and_dispatches_project_tools(self):
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn("chat_tools += PROJECT_SCHEMAS", src)
        self.assertIn("elif t_name in PROJECT_TOOL_NAMES:", src)
        self.assertIn("sys_parts.append(PROJECT_MANUAL)", src)


if __name__ == "__main__":
    unittest.main()


class PrefixedPaths(ProjectBuilder):
    def test_path_starting_with_the_project_name_is_not_nested(self):
        r = self.w("dhaka-site/css/styles.css", "body{}", project="Dhaka Site")
        self.assertIn("Created css/styles.css", r)
        self.assertTrue((self.root / "projects/dhaka-site/css/styles.css").is_file())
        self.assertFalse((self.root / "projects/dhaka-site/dhaka-site").exists())


class UnfinishedBuildNudge(unittest.TestCase):
    def test_text_only_reply_before_project_finish_is_nudged(self):
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn("if (project_build and not tool_calls and not project_done and project_nudges < 3", src)
        self.assertIn("project_done = True", src)


class ResumeAfterStop(ProjectBuilder):
    def test_unfinished_project_gets_a_resume_note_and_finished_one_does_not(self):
        self.assertEqual(pf.open_project_note(), "")
        self.w("index.html", INDEX)
        self.w("css/styles.css", "body{}")
        note = pf.open_project_note()
        self.assertIn("dhaka-site", note)
        self.assertIn("index.html (1 lines)", note)
        self.assertIn("project_finish", note)
        pf.tool_project_finish({"project": "Dhaka Site"})
        self.assertEqual(pf.open_project_note(), "")
        self.w("js/main.js", "var a=1")                       # new work after the zip: open again
        import os, time
        for z in self.root.glob("dhaka-site-*.zip"):
            os.utime(z, (time.time() - 60, time.time() - 60))
        self.assertIn("main.js", pf.open_project_note())


class Planning(ProjectBuilder):
    P = "Plan Site"

    def plan(self, files):
        return pf.tool_project_plan({"project": self.P, "files": files})

    def test_write_without_a_plan_is_refused_with_the_instruction(self):
        r = pf.tool_project_write({"project": self.P, "path": "index.html", "content": "<p>x</p>"})
        self.assertTrue(r.startswith("error: plan the site first"), r)
        self.assertEqual([f for f in self.root.rglob("*") if f.is_file()], [])

    def test_plan_is_validated(self):
        self.assertIn("no HTML page", self.plan([{"path": "js/a.js", "purpose": "x"}]))
        self.assertIn("at most", self.plan([{"path": f"js/f{i}.js", "purpose": "x"} for i in range(15)] + [{"path": "index.html", "purpose": "x"}]))
        self.assertTrue(self.plan([{"path": "../x.html", "purpose": "x"}]).startswith("error:"))
        self.assertTrue(self.plan([]).startswith("error:"))

    def test_progress_is_reported_and_finish_waits_for_the_plan(self):
        r = self.plan([{"path": "index.html", "purpose": "page", "min_lines": 1}, {"path": "css/styles.css", "purpose": "style", "min_lines": 2},
                       {"path": "js/main.js", "purpose": "logic", "min_lines": 1}])
        self.assertIn("0 of 3 files done", r)
        r = pf.tool_project_write({"project": self.P, "path": "index.html", "content": INDEX})
        self.assertIn("1 of 3 files done. Next: css/styles.css", r)
        r = pf.tool_project_write({"project": self.P, "path": "css/styles.css", "content": "body{}"})
        self.assertIn("1 of 3 files done", r)                                   # one line is below its min_lines of 2
        r = pf.tool_project_append({"project": self.P, "path": "css/styles.css", "content": "h1{}"})
        self.assertIn("2 of 3 files done. Next: js/main.js", r)
        fin = pf.tool_project_finish({"project": self.P})
        self.assertTrue(fin.startswith("error: the plan is not finished"), fin)
        self.assertIn("js/main.js", fin)
        pf.tool_project_write({"project": self.P, "path": "js/main.js", "content": "var a=1"})
        out = pf.tool_project_finish({"project": self.P})
        self.assertIn("[DOWNLOAD:", out)
        self.assertNotIn("error", out)

    def test_dropping_a_file_from_the_plan_lets_finish_pass(self):
        self.plan([{"path": "index.html", "purpose": "page", "min_lines": 1}, {"path": "js/map.js", "purpose": "map"}])
        pf.tool_project_write({"project": self.P, "path": "index.html", "content": "<p>x</p>"})
        self.assertTrue(pf.tool_project_finish({"project": self.P}).startswith("error: the plan is not finished"))
        self.plan([{"path": "index.html", "purpose": "page", "min_lines": 1}])
        self.assertIn("[DOWNLOAD:", pf.tool_project_finish({"project": self.P}))

    def test_resume_note_lists_what_is_still_to_write(self):
        self.plan([{"path": "index.html", "purpose": "page", "min_lines": 1}, {"path": "js/map.js", "purpose": "leaflet map"}])
        pf.tool_project_write({"project": self.P, "path": "index.html", "content": "<p>x</p>"})
        note = pf.open_project_note()
        self.assertIn("1 of 2 files done", note)
        self.assertIn("js/map.js (leaflet map)", note)
