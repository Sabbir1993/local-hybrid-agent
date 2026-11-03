"""'complete this' after a reply that stopped inside a code block: recognised as "continue", routed to the 3D lane, and told to resume
at the last character (not start over)."""
import unittest

import core.agent_tools  # noqa: F401  (import order: core.doc_tools needs core.agent_tools first)
from core import prompt_scope
from core.doc_tools import builds

CUT = [{"role": "user", "content": "Make a 3D village"},
       {"role": "assistant", "content": "Here it is:\n```html\n<script>const g = new THREE.Group();\ngroup.position.set(x, 0,"}]


class ContinueTests(unittest.TestCase):
    def test_continue_phrases(self):
        for t in ("complete this", "continue", "Continue the code", "finish that", "complete the rest", "carry on from where you stopped"):
            self.assertTrue(builds.is_continue(t), t)
        self.assertFalse(builds.is_continue("write a story"))

    def test_cut_off_reply_is_detected_and_closed_one_is_not(self):
        self.assertTrue(prompt_scope.cut_off_tail(CUT).endswith("group.position.set(x, 0,"))
        done = [{"role": "assistant", "content": "```html\n<p>x</p>\n```\nDone."}]
        self.assertEqual(prompt_scope.cut_off_tail(done), "")

    def test_note_carries_the_tail(self):
        note = prompt_scope.CONTINUE_CUT_NOTE.format(tail=prompt_scope.cut_off_tail(CUT))
        self.assertIn("group.position.set(x, 0,", note)
        self.assertIn("do not start the file again", note)

    def test_continue_after_a_3d_page_goes_to_the_3d_lane(self):
        self.assertTrue(prompt_scope.three_d_wanted("complete this", [], CUT))
        self.assertFalse(prompt_scope.three_d_wanted("complete this", [], [{"role": "assistant", "content": "A poem about rain."}]))


if __name__ == "__main__":
    unittest.main()
