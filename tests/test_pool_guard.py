"""Shared KV pool guard (core/serving_slots.py). llama-server b12810 fails EVERY running request when they need more
than the -kvu pool holds ("Context size has been exceeded"), so a request that would overflow it waits instead."""
import asyncio
import unittest
from unittest import mock

from core import request_context as rc
from core import serving_slots
from core.state import state
from routes.common import llm_stream as common

POOL = {"n_slots": 16, "kv_unified": True, "context_size": 1_000_000, "kv_unified_per_slot": 131072}


class PoolMath(unittest.TestCase):
    def test_capacity_only_for_a_shared_pool_with_several_slots(self):
        self.assertEqual(serving_slots.pool_capacity(POOL), int(1_000_000 * (1 - serving_slots.POOL_MARGIN)))
        self.assertEqual(serving_slots.pool_capacity({**POOL, "kv_unified": False}), 0)
        self.assertEqual(serving_slots.pool_capacity({**POOL, "n_slots": 1}), 0)
        self.assertEqual(serving_slots.pool_capacity(None), 0)

    def test_need_is_prompt_plus_answer_capped_at_one_slot(self):
        self.assertEqual(serving_slots.request_need(1000, 500, POOL), 1500)
        self.assertEqual(serving_slots.request_need(1000, -1, POOL), 1000 + serving_slots.GEN_RESERVE_TOKENS)
        self.assertEqual(serving_slots.request_need(500_000, 100, POOL), 131072)


class Calibration(unittest.TestCase):
    def test_learns_the_real_tokenizer_ratio(self):
        c = serving_slots.TokenCalibration()
        self.assertEqual(c.scale(10_000), 10_000)            # nothing learned yet: the plain estimate
        c.observe(10_000, 6_000)                             # Spark read 6K where the app estimated 10K
        self.assertEqual(c.scale(10_000), int(10_000 * 0.6 * c.SAFETY))

    def test_ignores_tiny_prompts_and_clamps(self):
        c = serving_slots.TokenCalibration()
        c.observe(50, 400)
        self.assertEqual(c.samples, 0)
        c.observe(1000, 100_000)
        self.assertLessEqual(c.ratio, c.HIGH)


class Ledger(unittest.TestCase):
    def test_waits_until_room_then_runs(self):
        order = []

        async def main():
            led = serving_slots.PoolLedger()
            await led.acquire(600, 1000)               # a long conversation is running

            async def late():
                await led.acquire(600, 1000)           # would overflow: waits
                order.append("late started")
                led.release(600)

            t = asyncio.create_task(late())
            await asyncio.sleep(0.02)
            order.append("first done")
            led.release(600)
            await asyncio.wait_for(t, 1)
            return led

        led = asyncio.run(main())
        self.assertEqual(order, ["first done", "late started"])
        self.assertEqual((led.used, led.running, led.waiting), (0, 0, 0))

    def test_one_request_alone_always_runs(self):
        async def main():
            led = serving_slots.PoolLedger()
            await asyncio.wait_for(led.acquire(5000, 1000), 1)
            return led.running
        self.assertEqual(asyncio.run(main()), 1)


class GateIntegration(unittest.TestCase):
    """Through _llm_chat_stream, the way chat and agent requests reach the main model."""

    def setUp(self):
        self._raw, self._profile = common._llm_chat_stream_raw, state.profile
        self._serving = common.APP_CONFIG.get("serving")
        common.admission = common._Admission()
        self.peak_used = 0

        async def fake_raw(client, msgs, *a, **k):
            self.peak_used = max(self.peak_used, serving_slots.pool.used)
            await asyncio.sleep(0.02)
            yield ("content_delta", "x")

        common._llm_chat_stream_raw = fake_raw
        p = mock.patch.object(serving_slots, "pool", serving_slots.PoolLedger())
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        common._llm_chat_stream_raw = self._raw
        state.profile = self._profile
        common.APP_CONFIG["serving"] = self._serving

    def test_long_prompts_that_would_overflow_wait_and_are_told_why(self):
        # pool of ~100K tokens; three users each send ~60K-token conversations at once
        state.profile = {**POOL, "context_size": 100_000, "kv_unified_per_slot": 0}
        common.APP_CONFIG["serving"] = {"queue_enabled": True, "max_inflight_per_user": 1}
        long_msgs = [{"role": "user", "content": "x" * 180_000}]       # ~60K tokens by the app's estimate
        events = []

        async def one(uid):
            rc.set_current_user(uid)
            async for ev, val in common._llm_chat_stream(state.client, long_msgs, max_tokens=1000):
                if ev == "queued":
                    events.append(val)

        async def main():
            await asyncio.gather(*(one(u) for u in (1, 2, 3)))

        asyncio.run(main())
        self.assertLessEqual(self.peak_used, serving_slots.pool_capacity(state.profile))
        self.assertTrue(events and all(e.get("reason") == "memory" for e in events))
        self.assertEqual(serving_slots.pool.used, 0)

    def test_short_chats_never_wait_for_memory(self):
        state.profile = dict(POOL)
        common.APP_CONFIG["serving"] = {"queue_enabled": True, "max_inflight_per_user": 1}
        events = []

        async def one(uid):
            rc.set_current_user(uid)
            async for ev, val in common._llm_chat_stream(state.client, [{"role": "user", "content": "hi"}]):
                if ev == "queued":
                    events.append(val)

        async def main():
            await asyncio.gather(*(one(u) for u in range(15)))

        asyncio.run(main())
        self.assertEqual(events, [])


class LiveQueue(unittest.TestCase):
    """The waiting user's place in line is updated while they wait; leaving the queue gives nothing away."""

    def setUp(self):
        self._raw, self._profile = common._llm_chat_stream_raw, state.profile
        self._serving = common.APP_CONFIG.get("serving")
        common.admission = common._Admission()
        state.profile = {"n_slots": 1}
        common.APP_CONFIG["serving"] = {"queue_enabled": True, "max_inflight_per_user": 1}
        p = mock.patch.object(common, "QUEUE_UPDATE_S", 0.01)
        p.start()
        self.addCleanup(p.stop)

        async def slow_raw(client, msgs, *a, **k):
            await asyncio.sleep(0.08)
            yield ("content_delta", "x")

        common._llm_chat_stream_raw = slow_raw

    def tearDown(self):
        common._llm_chat_stream_raw = self._raw
        state.profile = self._profile
        common.APP_CONFIG["serving"] = self._serving

    def test_position_counts_down(self):
        seen = {}

        async def one(uid, delay):
            await asyncio.sleep(delay)
            rc.set_current_user(uid)
            async for ev, val in common._llm_chat_stream(state.client, []):
                if ev == "queued":
                    seen.setdefault(uid, []).append(val["position"])

        async def main():
            await asyncio.gather(one(1, 0), one(2, 0.01), one(3, 0.02))

        asyncio.run(main())
        self.assertEqual(seen[3][0], 2)
        self.assertEqual(seen[3][-1], 1)                  # moved up when user 2 got the slot
        stats = common.admission.stats()
        self.assertEqual(stats["queue_depth"], 0)
        self.assertEqual(stats["requests_5m"], 3)
        self.assertIsNotNone(stats["ttft_avg_s"])

    def test_leaving_the_queue_gives_nothing_away(self):
        async def main():
            rc.set_current_user(1)
            first = common._llm_chat_stream(state.client, [])
            await first.__anext__()                          # user 1 is generating
            rc.set_current_user(2)
            waiting = common._llm_chat_stream(state.client, [])
            ev, _ = await waiting.__anext__()                 # user 2 is told it waits
            self.assertEqual(ev, "queued")
            await waiting.aclose()                            # ... and gives up
            async for _ in first:
                pass
            await asyncio.sleep(0.02)
            glob, _mine = common.admission._sems(2)
            return glob._value, common.admission.waiting

        value, waiting = asyncio.run(main())
        self.assertEqual((value, waiting), (1, 0))


if __name__ == "__main__":
    unittest.main()
