"""Sticky llama-server slots (core/serving_slots.py): each user keeps their own slot, so their cached conversation is
not evicted by another user; a second parallel generation borrows a free slot; the helper slot is never a user's."""
import asyncio
import unittest
from unittest import mock

import core.agent_tools  # noqa: F401  (import order)
from core import serving_slots
from routes.common import llm_stream as ls


def _serving(**cfg):
    return mock.patch.object(serving_slots, "_serving_cfg", lambda: cfg)


class SlotTableTests(unittest.TestCase):
    def setUp(self):
        self.t = serving_slots.SlotTable()

    def test_fifteen_users_get_fifteen_different_slots(self):
        uids = [7, 22, 37, 52, 67, 1, 16, 31, 46, 61, 76, 91, 3, 18, 33]     # uid % 15 gave only 3 distinct slots
        slots = [self.t.choose(u, 15) for u in uids]
        self.assertEqual(sorted(slots), list(range(15)))

    def test_a_user_keeps_their_slot(self):
        a = self.t.choose(5, 4)
        self.t.choose(9, 4)
        self.assertEqual(self.t.choose(5, 4), a)

    def test_busy_own_slot_borrows_a_free_one_without_taking_it_over(self):
        a = self.t.choose(5, 4)
        self.t.begin(a)
        b = self.t.choose(5, 4)
        self.assertNotEqual(a, b)
        self.assertEqual(self.t.slot_of[5], a)          # the cached conversation stays where it is
        self.assertNotIn(b, self.t.owner)                # borrowed, not owned

    def test_new_user_takes_the_slot_used_longest_ago(self):
        for u in (1, 2, 3):
            self.t.choose(u, 3)
        self.t.last_used[self.t.slot_of[2]] = 0          # user 2 idle the longest
        old = self.t.slot_of[2]
        self.assertEqual(self.t.choose(4, 3), old)
        self.assertNotIn(2, self.t.slot_of)

    def test_slots_in_use_are_never_reassigned(self):
        for u in (1, 2):
            self.t.begin(self.t.choose(u, 2))
        got = self.t.choose(3, 2)                        # everything busy: shares, does not evict an owner's mapping
        self.assertIn(got, (0, 1))
        self.assertEqual(len(self.t.slot_of), 2)                # user 3 owns nothing yet

    def test_resize_starts_over_but_keeps_the_dict(self):
        ref = self.t.inflight
        self.t.choose(1, 4)
        self.t.choose(1, 8)
        self.assertIs(self.t.inflight, ref)


class HelperSlotTests(unittest.TestCase):
    def test_off_by_default_so_existing_setups_keep_every_slot(self):
        with _serving():
            self.assertIsNone(serving_slots.helper_slot_index(16))
            self.assertEqual(serving_slots.user_slot_count(5), 5)

    def test_reserved_from_four_slots_when_on(self):
        with _serving(helper_slot=1):
            self.assertIsNone(serving_slots.helper_slot_index(3))
            self.assertEqual(serving_slots.helper_slot_index(16), 15)
            self.assertEqual(serving_slots.user_slot_count(16), 15)
            self.assertEqual(serving_slots.user_slot_count(2), 2)

    def test_users_never_get_the_helper_slot(self):
        t = serving_slots.SlotTable()
        with _serving(helper_slot=1), mock.patch.object(serving_slots, "table", t):
            got = {ls._choose_slot(u, 16) for u in range(40)}
        self.assertNotIn(15, got)

    def test_helper_call_pins_the_helper_slot(self):
        async def go():
            async with serving_slots.helper_call() as extra:
                return extra
        with _serving(helper_slot=1), mock.patch.object(serving_slots, "main_n_slots", return_value=16), \
                mock.patch.object(serving_slots, "_helper_sem", None):
            self.assertEqual(asyncio.run(go()), {"id_slot": 15, "cache_prompt": True})
        with _serving(helper_slot=1), mock.patch.object(serving_slots, "main_n_slots", return_value=2):
            self.assertEqual(asyncio.run(go()), {})
        with _serving(), mock.patch.object(serving_slots, "main_n_slots", return_value=16):
            self.assertEqual(asyncio.run(go()), {})           # off: helper calls behave as before


class PayloadTests(unittest.TestCase):
    def test_payload_uses_the_chosen_slot(self):
        t = serving_slots.SlotTable()
        with _serving(), mock.patch.object(serving_slots, "table", t), \
                mock.patch.object(ls, "get_current_user_id", return_value=5), \
                mock.patch.object(ls.state, "profile", {"n_slots": 4}):
            mine = ls._choose_slot(5, 4)
            self.assertEqual(ls._main_slot_fields()["id_slot"], mine)
            tok = ls._slot_choice.set(2)
            try:
                self.assertEqual(ls._main_slot_fields()["id_slot"], 2)
            finally:
                ls._slot_choice.reset(tok)


if __name__ == "__main__":
    unittest.main()
