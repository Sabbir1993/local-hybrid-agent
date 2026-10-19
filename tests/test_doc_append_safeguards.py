import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import agent_tools, doc_store, doc_tools  # noqa: E402
from core.doc_tools import location  # noqa: E402
from core.request_context import set_current_user  # noqa: E402
from tests.test_doc_ops import doc_files_db  # noqa: E402


class DocAppendSafeguardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(agent_tools, "COMMON_ROOT", self.root),
            mock.patch.object(doc_store, "_projects_db", doc_files_db()),
        ]
        for p in self._patches:
            p.start()
        set_current_user(99)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        set_current_user(None)
        self.tmp.cleanup()

    def test_file_arg_fallback_to_recent_file(self):
        ws = agent_tools.common_workspace()
        deck_file = ws / "Test_Presentation.pptx"
        deck_file.write_bytes(b"fake pptx content")

        resolved = location._file_arg({})
        self.assertEqual(resolved, "Test_Presentation.pptx")

    def test_doc_append_graceful_on_done_without_content(self):
        out = doc_tools.tool_doc_create_common({
            "file": "AutoFinish.pptx",
            "content": "# Title Slide\nSubtitle\n---\n## Agenda\n- Part 1\n- Part 2"
        })
        created_name = out.split("[DOWNLOAD: ")[1].split("]")[0]
        self.assertTrue(created_name.startswith("AutoFinish"))
        self.assertTrue(created_name.endswith(".pptx"))

        append_res = doc_tools.tool_doc_append_common({"done": True})
        self.assertIn(created_name, append_res)
        self.assertIn("complete", append_res)
        self.assertIn(f"[DOWNLOAD: {created_name}]", append_res)


if __name__ == "__main__":
    unittest.main()
