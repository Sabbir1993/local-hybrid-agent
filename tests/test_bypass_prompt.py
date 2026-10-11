import unittest

from core.agent_loop import AGENT_SYSTEM_PROMPT


class BypassPromptTests(unittest.TestCase):
    def test_approval_sentence_exists_for_bypass_rewrite(self):
        # routes/agent/setup.py rewrites this exact sentence in bypass mode; keep them in step
        self.assertIn("every run_python call needs the user's approval.", AGENT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
