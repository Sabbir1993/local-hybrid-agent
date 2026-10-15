"""Chat saves documents, not programs: executable file types are refused, tool markup never becomes the answer."""
import unittest
from unittest import mock

import core.agent_tools  # noqa: F401
from routes.chat import delivery as d


class Executables(unittest.TestCase):
    def test_scripts_and_programs_are_refused(self):
        for name in ("run.sh", "build.PY", "setup.bat", "x.cmd", "a.ps1", "m.vbs", "t.exe", "i.msi", "lib.dll", "a.jar",
                     "dir/inner/run_create-1.sh", " spaced.py "):
            self.assertTrue(d.is_executable_path(name), name)
            with mock.patch("core.agent_tools.tool_write_file_common") as w:
                self.assertEqual(d.chat_write_file({"path": name, "content": "echo hi"}), d._SCRIPT_REFUSAL)
                w.assert_not_called()

    def test_the_other_path_keys_are_covered_too(self):
        for key in ("file", "filename"):
            self.assertTrue(d.chat_write_file({key: "a.sh", "content": "x"}).startswith("error:"))

    def test_documents_still_save(self):
        for name in ("notes.md", "data.csv", "deck.pptx", "guide.pdf", "page.html", "app.js", "style.css", "report.docx"):
            self.assertFalse(d.is_executable_path(name), name)
        with mock.patch("core.agent_tools.tool_write_file_common", return_value="Saved ok") as w:
            self.assertEqual(d.chat_write_file({"path": "notes.md", "content": "hi"}), "Saved ok")
            w.assert_called_once()

    def test_the_route_saves_only_through_the_guarded_writer(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertNotIn("tool_write_file_common({", src)           # every chat write goes through chat_write_file
        self.assertNotIn("run_in_executor_ctx(tool_write_file_common", src)
        self.assertGreaterEqual(src.count("chat_write_file"), 4)
        self.assertIn("_NO_TOOL_ANSWER", src)

    def test_deck_tools_are_offered_on_resume(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn("if pdf_build or resume_note or brief_phase or", src)

    def test_the_agent_writer_is_not_restricted(self):
        from core.agent_tools import tool_write_file_common
        self.assertTrue(callable(tool_write_file_common))


if __name__ == "__main__":
    unittest.main()


class RepeatWrites(unittest.TestCase):
    def test_same_document_again_is_detected_even_with_a_new_suffix(self):
        saved = ["Hercules_TAP_Bangladesh_Market_Growth-897d8c24.md"]
        self.assertTrue(d._already_written(saved, {"path": "Hercules_TAP_Bangladesh_Market_Growth.md"}))
        self.assertTrue(d._already_written(saved, {"file": "hercules_tap_bangladesh_market_growth-4ab05d2f.md"}))

    def test_other_documents_and_types_are_fine(self):
        saved = ["Plan-897d8c24.md"]
        self.assertFalse(d._already_written(saved, {"path": "Budget.md"}))
        self.assertFalse(d._already_written(saved, {"path": "Plan.pdf"}))
        self.assertFalse(d._already_written([], {"path": "Plan.md"}))
        self.assertFalse(d._already_written(saved, {}))


class ImageGate(unittest.TestCase):
    def test_pictures_only_when_asked(self):
        from core import prompt_scope as ps
        self.assertTrue(ps.image_intent("draw a logo for my shop"))
        self.assertTrue(ps.image_intent("make it darker", [{"role": "user", "content": "generate an image of a cat"}]))
        self.assertFalse(ps.image_intent("write a TAP document for Hercules sales force automation", []))
        self.assertFalse(ps.image_intent("summarise the market growth in Bangladesh"))


class MissingType(unittest.TestCase):
    def test_where_is_the_spreadsheet_when_only_a_md_exists(self):
        from routes.chat import file_intent as fi
        files = ["Hercules_TAP-3d384188.md"]
        self.assertEqual(fi.missing_type("where is spread sheet ?", files), "spreadsheet")
        self.assertEqual(fi.missing_type("where is the excel file", files), "spreadsheet")
        self.assertEqual(fi.missing_type("send me the pdf", files), "pdf")

    def test_when_one_exists_or_no_type_is_named_nothing_is_missing(self):
        from routes.chat import file_intent as fi
        self.assertEqual(fi.missing_type("where is the spreadsheet", ["a.md", "b-1234abcd.xlsx"]), "")
        self.assertEqual(fi.missing_type("where is the file", ["a.md"]), "")
        self.assertEqual(fi.missing_type("where is my csv", ["data.csv"]), "")

    def test_the_route_builds_instead_of_resending(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn("absent_type = missing_type(own_q, prior_files)", src)
        self.assertIn('absent_type == "spreadsheet"', src)


class ServerPaths(unittest.TestCase):
    def s(self, text):
        return d.scrub_server_paths(text)

    def test_the_reported_case(self):
        out = self.s("Here it is: [Hercules_TAP_Bangladesh_Market_Grep-a9978806.xlsx](/common_space/Hercules_TAP_Bangladesh_Market_Grep-a9978806.xlsx)")
        self.assertEqual(out, "Here it is: [DOWNLOAD: Hercules_TAP_Bangladesh_Market_Grep-a9978806.xlsx]")
        self.assertNotIn("common_space", out)

    def test_other_shapes(self):
        self.assertEqual(self.s("saved to (/common_space/user_5/a.pdf)"), "saved to (a.pdf)")
        self.assertEqual(self.s("`/common_space/x.csv`"), "x.csv")
        self.assertEqual(self.s(r"[file](C:\Users\sabbir\a.xlsx)"), "[DOWNLOAD: a.xlsx]")
        self.assertEqual(self.s("[open](/home/app/uploads/report.docx)"), "[DOWNLOAD: report.docx]")
        self.assertNotIn("/", self.s("see knowledge_uploads/abc/def.xlsx here").replace("see knowledge_uploads", ""))
        self.assertEqual(self.s("[DOWNLOAD: a.xlsx] ([DOWNLOAD: a.xlsx])"), "[DOWNLOAD: a.xlsx]")

    def test_ordinary_text_and_code_are_untouched(self):
        for t in ("Edit /etc/hosts and restart", "the path C:/Windows/System32 is protected", "[docs](https://example.com/a.pdf)",
                  "[DOWNLOAD: report-1234abcd.xlsx]", "```bash\ncat /common_space/notes.txt\n```", "[dl](/agent/download?path=a.xlsx)"):
            self.assertEqual(self.s(t), t)

    def test_the_route_scrubs_both_final_paths_and_the_tool_result_stops_saying_common_space(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        src = (root / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("scrub_server_paths(content)"), 2)
        w = (root / "core" / "agent_tools" / "common_writer.py").read_text(encoding="utf-8")
        self.assertNotIn("to common space", w)


class ToolMessages(unittest.TestCase):
    def test_a_write_result_carries_exactly_one_download_tag(self):
        from pathlib import Path
        w = (Path(__file__).resolve().parents[1] / "core" / "agent_tools" / "common_writer.py").read_text(encoding="utf-8")
        for line in w.splitlines():
            if 'msg = f"Wrote' in line:
                self.assertEqual(line.count("[DOWNLOAD:"), 1, line)
