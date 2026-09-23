"""tests/test_project_context.py - AGENTS.md loader for /init + prompt injection.

Run: python -m unittest tests.test_project_context -v
Card numbers are public issuer test PANs, not real cards.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import project_context, agent_tools


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self._patches = [
            mock.patch.object(agent_tools, "active_workspace", lambda: self.ws),
            mock.patch.object(agent_tools, "_remote_uid", lambda: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def load(self):
        return asyncio.run(project_context.load_project_instructions())

    def test_none_when_absent(self):
        self.assertIsNone(self.load())

    def test_reads_agents_md(self):
        (self.ws / "AGENTS.md").write_text("# Proj\nRun: pytest", encoding="utf-8")
        name, text = self.load()
        self.assertEqual(name, "AGENTS.md")
        self.assertIn("pytest", text)

    def test_agents_md_wins_over_claude_md(self):
        (self.ws / "AGENTS.md").write_text("agents", encoding="utf-8")
        (self.ws / "CLAUDE.md").write_text("claude", encoding="utf-8")
        self.assertEqual(self.load()[0], "AGENTS.md")

    def test_falls_back_to_claude_md(self):
        (self.ws / "CLAUDE.md").write_text("use npm test", encoding="utf-8")
        self.assertEqual(self.load(), ("CLAUDE.md", "use npm test"))

    def test_empty_file_ignored(self):
        (self.ws / "AGENTS.md").write_text("   \n", encoding="utf-8")
        self.assertIsNone(self.load())

    def test_truncates_large_file(self):
        (self.ws / "AGENTS.md").write_text("x" * 20000, encoding="utf-8")
        _, text = self.load()
        self.assertLess(len(text), project_context.MAX_CHARS + 200)
        self.assertIn("truncated", text)

    def test_masks_pan_and_secrets(self):
        (self.ws / "AGENTS.md").write_text(
            "test card 4111 1111 1111 1111\nAPI_KEY=abcd1234efgh5678\n"
            "password: \"hunter2hunter2\"\nstore_id = [PLACEHOLDER]\n", encoding="utf-8")
        _, text = self.load()
        self.assertNotIn("4111 1111 1111 1111", text)
        self.assertNotIn("abcd1234efgh5678", text)
        self.assertNotIn("hunter2hunter2", text)
        self.assertIn("API_KEY=[REDACTED]", text)
        self.assertIn("[PLACEHOLDER]", text)


class PromptBlockTests(unittest.TestCase):
    def test_empty_without_file(self):
        self.assertEqual(project_context.prompt_block(None), "")

    def test_block_names_file(self):
        b = project_context.prompt_block(("AGENTS.md", "run make"))
        self.assertIn("PROJECT INSTRUCTIONS (from AGENTS.md", b)
        self.assertIn("run make", b)

    def test_init_prompt_targets_agents_md(self):
        self.assertIn("AGENTS.md", project_context.INIT_PROMPT)
        self.assertIn("[PLACEHOLDER]", project_context.INIT_PROMPT)


if __name__ == "__main__":
    unittest.main()
