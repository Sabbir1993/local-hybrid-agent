"""tests/test_prompt_templates.py - system prompts that go through str.format() must not
contain stray braces (a literal JSON example crashed every /agent/run with KeyError).

Run: python -m unittest tests.test_prompt_templates
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_loop import AGENT_SYSTEM_PROMPT
from core.subagent import SUBAGENT_SYSTEM_PROMPT


class PromptTemplateTests(unittest.TestCase):
    def test_agent_prompt_formats(self):
        out = AGENT_SYSTEM_PROMPT.format(workspace="W")
        self.assertIn('{"op":"set_text"', out)

    def test_subagent_prompt_formats(self):
        SUBAGENT_SYSTEM_PROMPT.format(workspace="W")


if __name__ == "__main__":
    unittest.main()
