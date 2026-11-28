"""Telemetry and the /slots read are computed once per tick and shared by every viewer (15 users = 15 dashboards and
status bars). Each viewer used to repeat the GPU queries and the llama-server /slots call, and every snapshot appended a
point to the sparkline history, so the charts ran faster the more people watched."""
import asyncio
import unittest
from unittest import mock

from core import serving_slots
from core import telemetry as tm


class SharedTelemetry(unittest.TestCase):
    def test_many_viewers_one_computation(self):
        calls = []

        async def compute():
            calls.append(1)
            await asyncio.sleep(0.01)
            return {"n": len(calls)}

        async def main():
            snaps = await asyncio.gather(*(tm.get_telemetry_snapshot() for _ in range(15)))
            again = await tm.get_telemetry_snapshot()          # within the tick: the same snapshot
            return snaps, again

        with mock.patch.object(tm, "_compute_telemetry_snapshot", compute), \
                mock.patch.object(tm, "_snap", None), mock.patch.object(tm, "_snap_task", None):
            snaps, again = asyncio.run(main())
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(s == {"n": 1} for s in snaps))
        self.assertEqual(again, {"n": 1})


class SharedSlots(unittest.TestCase):
    def test_slots_read_once_for_many_callers(self):
        gets = []

        class Resp:
            status_code = 200

            def json(self):
                return [{"id": 0, "n_ctx": 131072}]

        class Client:
            async def get(self, path, timeout=None):
                gets.append(path)
                await asyncio.sleep(0.01)
                return Resp()

        proc = mock.Mock()
        proc.poll.return_value = None

        async def main():
            return await asyncio.gather(*(serving_slots.main_slots() for _ in range(15)))

        with mock.patch.object(serving_slots, "_slots_cache", (0.0, None)), \
                mock.patch.object(serving_slots, "_slots_task", None), \
                mock.patch("core.state.state.process", proc), mock.patch("core.state.state.client", Client()):
            res = asyncio.run(main())
        self.assertEqual(gets, ["/slots"])
        self.assertTrue(all(r == [{"id": 0, "n_ctx": 131072}] for r in res))

    def test_server_down_means_no_slots(self):
        with mock.patch.object(serving_slots, "_slots_cache", (0.0, None)), mock.patch("core.state.state.process", None):
            self.assertIsNone(asyncio.run(serving_slots.main_slots()))


class UserSlotForStatus(unittest.TestCase):
    def test_status_shows_the_users_sticky_slot(self):
        t = serving_slots.SlotTable()
        with mock.patch.object(serving_slots, "table", t), \
                mock.patch.object(serving_slots, "_serving_cfg", lambda: {}):
            t.choose(7, 15)
            t.choose(22, 15)                                  # 22 % 15 == 7 % 15: would have shown the same slot
            self.assertNotEqual(serving_slots.user_slot(7, 15), serving_slots.user_slot(22, 15))


if __name__ == "__main__":
    unittest.main()
