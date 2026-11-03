"""A user's second parallel generation goes to a free llama-server slot instead of waiting behind their first one."""
import unittest
from unittest import mock

import core.agent_tools  # noqa: F401  (import order)
from routes.common import llm_stream as ls


class SlotChoiceTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(ls._slot_inflight, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_own_slot_when_free(self):
        self.assertEqual(ls._choose_slot(5, 4), 1)

    def test_busy_own_slot_moves_to_a_free_one(self):
        ls._slot_inflight[1] = 1
        got = ls._choose_slot(5, 4)
        self.assertNotEqual(got, 1)
        self.assertIn(got, (0, 2, 3))

    def test_all_busy_keeps_own_slot(self):
        ls._slot_inflight.update({0: 1, 1: 1, 2: 1, 3: 1})
        self.assertEqual(ls._choose_slot(5, 4), 1)

    def test_payload_uses_the_chosen_slot(self):
        with mock.patch.object(ls, "get_current_user_id", return_value=5), \
                mock.patch.object(ls.state, "profile", {"n_slots": 4}):
            self.assertEqual(ls._main_slot_fields()["id_slot"], 1)
            tok = ls._slot_choice.set(3)
            try:
                self.assertEqual(ls._main_slot_fields()["id_slot"], 3)
            finally:
                ls._slot_choice.reset(tok)


if __name__ == "__main__":
    unittest.main()
