"""Chat mode: HTML / website requests get the frontend-design skill's guidance silently (chat has no read_skill tool).

Run: python -m unittest tests.test_chat_design_skill -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import skills

RUN_SRC = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")


class SilentSkillTests(unittest.TestCase):
    def _skills_dir(self, body="# Design\nPick a bold palette."):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sk = Path(d.name) / "frontend-design"
        sk.mkdir()
        (sk / "SKILL.md").write_text(f"---\nname: frontend-design\ndescription: x\n---\n{body}", encoding="utf-8")
        return Path(d.name)

    def test_body_without_frontmatter_and_told_to_stay_quiet(self):
        with mock.patch.object(skills, "SKILLS_DIR", self._skills_dir()):
            out = skills.silent_skill_prompt("frontend-design")
        self.assertIn("apply silently", out)
        self.assertIn("never tell the user a skill is in use", out)
        self.assertIn("Pick a bold palette.", out)
        self.assertNotIn("description: x", out)

    def test_falls_back_to_the_reviewed_catalog_copy(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        with mock.patch.object(skills, "SKILLS_DIR", Path(empty.name)), \
                mock.patch.object(skills, "CATALOG_DIR", self._skills_dir("catalog body")):
            self.assertIn("catalog body", skills.silent_skill_prompt("frontend-design"))

    def test_unknown_or_unsafe_names_give_nothing(self):
        for name in ("no-such-skill", "../etc/passwd", "", "A B"):
            self.assertEqual(skills.silent_skill_prompt(name), "")

    def test_output_is_capped(self):
        with mock.patch.object(skills, "SKILLS_DIR", self._skills_dir("x" * 50000)):
            self.assertLess(len(skills.silent_skill_prompt("frontend-design", max_chars=500)), 500 + len(skills.SILENT_PREFACE) + 1)

    def test_real_skill_loads_when_present(self):
        out = skills.silent_skill_prompt("frontend-design")
        if out:
            self.assertIn("Frontend Design", out)

    def test_chat_run_wires_it_for_html_and_project_builds_only(self):
        self.assertIn('skills.silent_skill_prompt("frontend-design")', RUN_SRC)
        self.assertIn("if has_file and (html_build or project_build or prompt_scope.html_wanted(", RUN_SRC)


if __name__ == "__main__":
    unittest.main()
