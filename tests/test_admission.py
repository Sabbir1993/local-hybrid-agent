"""tests/test_admission.py - fair-share admission gate in routes/common.py.

Run: python -m unittest tests.test_admission -v
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import request_context as rc
from core.state import state
# the gate and the raw stream live together in routes/common/llm_stream.py
from routes.common import llm_stream as common


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self._raw = common._llm_chat_stream_raw
        self._profile = state.profile
        self._serving = common.APP_CONFIG.get("serving")
        common.admission = common._Admission()
        self.live = {"total": 0, "peak": 0, "per_user": {}, "per_user_peak": {}}

        async def fake_raw(client, msgs, *a, **k):
            uid = rc.get_current_user_id()
            lv = self.live
            lv["total"] += 1
            lv["peak"] = max(lv["peak"], lv["total"])
            lv["per_user"][uid] = lv["per_user"].get(uid, 0) + 1
            lv["per_user_peak"][uid] = max(lv["per_user_peak"].get(uid, 0), lv["per_user"][uid])
            try:
                await asyncio.sleep(0.02)
                yield ("content_delta", "x")
            finally:
                lv["total"] -= 1
                lv["per_user"][uid] -= 1

        common._llm_chat_stream_raw = fake_raw

    def tearDown(self):
        common._llm_chat_stream_raw = self._raw
        state.profile = self._profile
        common.APP_CONFIG["serving"] = self._serving

    def _run(self, users, requests_per_user, n_slots):
        state.profile = {"n_slots": n_slots}
        common.APP_CONFIG["serving"] = {"queue_enabled": True, "max_inflight_per_user": 1}
        queued = []

        async def one(uid):
            rc.set_current_user(uid)
            async for ev, val in common._llm_chat_stream(state.client, []):
                if ev == "queued":
                    queued.append(val["position"])

        async def main():
            await asyncio.gather(*(one(u) for u in users for _ in range(requests_per_user)))

        asyncio.run(main())
        return queued

    def test_global_limit_is_slot_count(self):
        queued = self._run(users=[1, 2, 3, 4, 5], requests_per_user=1, n_slots=2)
        self.assertEqual(self.live["peak"], 2)
        self.assertEqual(len(queued), 3)          # 3 of 5 had to wait
        self.assertEqual(self.live["total"], 0)   # everything released

    def test_one_inflight_per_user(self):
        self._run(users=[7], requests_per_user=3, n_slots=5)
        self.assertEqual(self.live["per_user_peak"][7], 1)

    def test_five_users_five_slots_never_queue(self):
        queued = self._run(users=[1, 2, 3, 4, 5], requests_per_user=1, n_slots=5)
        self.assertEqual(queued, [])
        self.assertEqual(self.live["peak"], 5)

    def test_non_main_client_bypasses_gate(self):
        state.profile = {"n_slots": 1}
        seen = []

        async def main():
            rc.set_current_user(1)
            async for ev, val in common._llm_chat_stream(object(), []):
                seen.append(ev)

        asyncio.run(main())
        self.assertEqual(seen, ["content_delta"])


class PerUserEntriesArePruned(unittest.TestCase):
    """_users held one semaphore per user id that ever made a request, kept for the life of
    the process. Bounded by user count rather than truly unbounded, but it never shrank."""

    def setUp(self):
        self._profile = state.profile
        self._serving = common.APP_CONFIG.get("serving")
        common.admission = common._Admission()
        common.APP_CONFIG["serving"] = {"queue_enabled": True, "max_inflight_per_user": 1}
        state.profile = {"n_slots": 1}

    def tearDown(self):
        # APP_CONFIG is process-global: leaving a trimmed "serving" behind silently broke
        # every later test that reads the shipped limits
        common.APP_CONFIG["serving"] = self._serving
        state.profile = self._profile

    def test_a_long_idle_tail_of_users_is_dropped(self):
        adm = common.admission
        for uid in range(50):
            adm._sems(uid)
        self.assertEqual(len(adm._users), 50)
        # backdate everything well past the idle window
        for uid in list(adm._users_seen):
            adm._users_seen[uid] -= adm._USER_IDLE_S + 60
        adm._sems(999)          # one live request triggers the prune
        self.assertEqual(len(adm._users), 1)
        self.assertIn(999, adm._users)
        self.assertEqual(len(adm._users_seen), 1)

    def test_a_user_mid_request_is_never_pruned(self):
        adm = common.admission

        async def go():
            adm._sems(7)
            for uid in list(adm._users_seen):
                adm._users_seen[uid] -= adm._USER_IDLE_S + 60
            await adm._users[7].acquire()      # holds the slot: someone is generating
            self.assertTrue(adm._users[7].locked())
            adm._sems(8)
            self.assertIn(7, adm._users, "an in-flight user must keep its semaphore")
            adm._users[7].release()

        asyncio.run(go())

    def test_recently_seen_users_survive(self):
        adm = common.admission
        for uid in range(5):
            adm._sems(uid)
        adm._sems(6)
        self.assertEqual(len(adm._users), 6)


class PreviewEntriesArePruned(unittest.TestCase):
    """Previews were capped per user but only collected on POST, so a user's HTML sat in a
    module global until that same user previewed something else again."""

    def setUp(self):
        from routes.agent import preview
        self.pv = preview
        self._saved = dict(preview._previews)
        preview._previews.clear()

    def tearDown(self):
        self.pv._previews.clear()
        self.pv._previews.update(self._saved)

    def _expired(self, uid, pid):
        self.pv._previews[pid] = (uid, time.time() - self.pv._PREVIEW_TTL_S - 60, "<p>x</p>")

    def test_the_read_path_also_collects(self):
        self._expired(3, "old1")
        self._expired(3, "old2")
        self.pv._previews["live"] = (3, time.time(), "<p>y</p>")
        asyncio.run(self.pv.get_preview_html("old1", mock.Mock(id=3)))
        self.assertNotIn("old1", self.pv._previews)
        self.assertNotIn("old2", self.pv._previews)
        self.assertIn("live", self.pv._previews)


if __name__ == "__main__":
    unittest.main()
