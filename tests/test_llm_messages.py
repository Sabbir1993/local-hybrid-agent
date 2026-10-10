"""tests/test_llm_messages.py - only the first message may be a system message.

Run: python -m unittest tests.test_llm_messages -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.llm_messages import CONTEXT_PREFIX, normalize_system_messages as norm


def m(role, text):
    return {"role": role, "content": text}


class NormalizeTests(unittest.TestCase):
    def test_a_clean_conversation_is_returned_untouched(self):
        msgs = [m("system", "s"), m("user", "u"), m("assistant", "a")]
        self.assertIs(norm(msgs), msgs)

    def test_a_late_system_message_becomes_a_user_context_message_in_place(self):
        # the earlier compaction marker ends up in the kept tail of the next one
        msgs = [m("system", "[COMPACTED] new"), m("user", "q"), m("system", "[COMPACTED] old"), m("user", "continue")]
        out = norm(msgs)
        self.assertEqual([x["role"] for x in out], ["system", "user", "user", "user"])
        self.assertEqual(out[2]["content"], CONTEXT_PREFIX + "[COMPACTED] old")

    def test_leading_system_messages_are_merged(self):
        out = norm([m("system", "a"), m("system", "b"), m("user", "u")])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "a\n\nb")

    def test_the_input_is_not_modified(self):
        msgs = [m("system", "s"), m("user", "u"), m("system", "late")]
        before = [dict(x) for x in msgs]
        norm(msgs)
        self.assertEqual(msgs, before)

    def test_no_system_message_at_all_is_fine(self):
        msgs = [m("user", "u")]
        self.assertIs(norm(msgs), msgs)

    def test_a_conversation_that_starts_without_system_converts_a_later_one(self):
        out = norm([m("user", "u"), m("system", "late"), m("assistant", "a")])
        self.assertEqual([x["role"] for x in out], ["user", "user", "assistant"])


if __name__ == "__main__":
    unittest.main()
