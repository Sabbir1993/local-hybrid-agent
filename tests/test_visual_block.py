"""Visual (flow / cards) chat blocks: when the prompt block is added."""
import json
import unittest

from core import prompt_scope as ps


class VisualGate(unittest.TestCase):
    def test_how_it_works_and_diagram_questions_get_the_block(self):
        for q in ("How caddy works for expose local domain to public",
                  "explain the architecture of a payment gateway",
                  "draw a diagram of the OAuth login flow",
                  "how does DNS resolution happen?",
                  "show the cyber threat trend for 2025 as a chart"):
            self.assertTrue(ps.visual_wanted(q), q)

    def test_code_documents_voice_mermaid_and_small_talk_do_not(self):
        for q in ("hi", "write a python function to sort lists", "make a pptx deck about our architecture",
                  "draw the architecture as a mermaid diagram", "```js\nx()\n``` how does this work"):
            self.assertFalse(ps.visual_wanted(q), q)
        self.assertFalse(ps.visual_wanted("how does caddy work", voice=True))
        self.assertFalse(ps.visual_wanted("how does caddy work", enabled=False))

    def test_prompt_example_is_valid_json_the_client_accepts(self):
        body = ps.VISUAL_STYLE.split("```visual\n", 1)[1].split("\n```", 1)[0]
        spec = json.loads(body)
        self.assertEqual(spec["type"], "flow")
        self.assertEqual(len(spec["steps"]), 3)


if __name__ == "__main__":
    unittest.main()
