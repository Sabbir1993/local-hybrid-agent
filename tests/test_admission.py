"""tests/test_admission.py - fair-share admission gate in routes/common.py.

Run: python -m unittest tests.test_admission -v
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import request_context as rc
from core.state import state
from routes import common


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


if __name__ == "__main__":
    unittest.main()
