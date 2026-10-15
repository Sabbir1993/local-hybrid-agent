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
