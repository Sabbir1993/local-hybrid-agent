"""Multi-instance routing of the main model (core/main_router.py, core/serving_slots.py place, the gate in
routes/common/llm_stream.py): requests reach an idle instance, a chat sticks to its instance, single mode is unchanged."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

from core import main_router
from core import request_context as rc
from core import serving_slots
from core.config import LLAMA_SERVER_PORT
from core.state import state
from routes.common import llm_stream as common

URL = f"http://127.0.0.1:{LLAMA_SERVER_PORT}/v1/chat/completions"
EXTRA_PORT = 8191


class FakeProc:
    def poll(self):
        return None


def _extra(healthy=True, n_slots=2, index=1):
    return SimpleNamespace(index=index, port=EXTRA_PORT + index - 1, n_slots=n_slots, healthy=healthy,
                           is_running=lambda: True, client=None)


def _multi_state(primary_slots=2, extra_slots=2, extra_healthy=True):
    """Patches for a two-instance main model: patch objects are returned, start them with a stack."""
    info = {"mode": "multi", "n_slots": primary_slots, "instances": [
        {"index": 0, "port": LLAMA_SERVER_PORT, "gpus": [], "n_slots": primary_slots},
        {"index": 1, "port": EXTRA_PORT, "gpus": [], "n_slots": extra_slots}]}
    return [mock.patch.object(state, "launch_info", info, create=True),
            mock.patch.object(state, "process", FakeProc(), create=True),
            mock.patch.object(state, "healthy", True, create=True),
            mock.patch.object(state, "extras", [_extra(extra_healthy, extra_slots)], create=True)]


def _single_state(n_slots=4):
    return [mock.patch.object(state, "launch_info", {"n_slots": n_slots}, create=True),
            mock.patch.object(state, "process", FakeProc(), create=True),
            mock.patch.object(state, "healthy", True, create=True),
            mock.patch.object(state, "extras", [], create=True)]


class _Stack(unittest.TestCase):
    """Starts patches for one test and stops them afterwards."""

    def enter(self, patches):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: 1\n\n"
        yield b"data: 2\n\n"


class RouterTransportTests(_Stack):
    def setUp(self):
        self.enter([mock.patch.object(main_router, "inflight", {})])
        self.seen = []

    def _handler(self, request):
        self.seen.append(request)
        return httpx.Response(200, json={"ok": True})

    def _transport(self, handler=None):
        return main_router.RouterTransport(httpx.MockTransport(handler or self._handler))

    def test_single_mode_passes_the_request_through_untouched(self):
        self.enter(_single_state())
        req = httpx.Request("POST", URL)

        async def go():
            return await self._transport().handle_async_request(req)
        resp = asyncio.run(go())
        self.assertEqual(resp.status_code, 200)
        self.assertIs(self.seen[0], req)
        self.assertEqual(self.seen[0].url.port, LLAMA_SERVER_PORT)
        self.assertEqual(main_router.inflight, {})

    def test_multi_rewrites_the_port_to_the_chosen_instance(self):
        self.enter(_multi_state())

        async def go():
            tok = main_router._instance_choice.set(1)
            try:
                return await self._transport().handle_async_request(httpx.Request("POST", URL))
            finally:
                main_router._instance_choice.reset(tok)
        asyncio.run(go())
        self.assertEqual(self.seen[0].url.port, EXTRA_PORT)

    def test_without_a_choice_the_least_busy_instance_is_used(self):
        self.enter(_multi_state())
        main_router.inflight.update({0: 3})

        async def go():
            return await self._transport().handle_async_request(httpx.Request("POST", URL))
        asyncio.run(go())
        self.assertEqual(self.seen[0].url.port, EXTRA_PORT)

    def test_an_unhealthy_instance_is_skipped(self):
        self.enter(_multi_state(extra_healthy=False))

        async def go():
            tok = main_router._instance_choice.set(1)
            try:
                return await self._transport().handle_async_request(httpx.Request("POST", URL))
            finally:
                main_router._instance_choice.reset(tok)
        asyncio.run(go())
        self.assertEqual(self.seen[0].url.port, LLAMA_SERVER_PORT)

    def test_streamed_response_holds_the_count_until_closed(self):
        self.enter(_multi_state())

        def handler(request):
            return httpx.Response(200, stream=ChunkStream())

        async def go():
            tok = main_router._instance_choice.set(1)
            try:
                resp = await self._transport(handler).handle_async_request(httpx.Request("POST", URL))
            finally:
                main_router._instance_choice.reset(tok)
            during = main_router.inflight.get(1)
            await resp.aclose()
            await resp.aclose()                       # a second close must not count again
            return during, main_router.inflight.get(1)
        self.assertEqual(asyncio.run(go()), (1, 0))

    def test_full_client_stream_releases_the_count(self):
        self.enter(_multi_state())

        def handler(request):
            return httpx.Response(200, stream=ChunkStream())

        async def go():
            client = httpx.AsyncClient(transport=self._transport(handler))
            tok = main_router._instance_choice.set(1)
            try:
                async with client.stream("POST", URL) as r:
                    await r.aread()
            finally:
                main_router._instance_choice.reset(tok)
            await client.aclose()
            return main_router.inflight.get(1, 0)
        self.assertEqual(asyncio.run(go()), 0)

    def test_an_error_from_the_inner_transport_releases_the_count(self):
        self.enter(_multi_state())

        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        async def go():
            tok = main_router._instance_choice.set(1)
            try:
                await self._transport(handler).handle_async_request(httpx.Request("POST", URL))
            except httpx.ConnectError:
                pass
            finally:
                main_router._instance_choice.reset(tok)
            return main_router.inflight.get(1, 0)
        self.assertEqual(asyncio.run(go()), 0)


class SlotStateBase(_Stack):
    """Fresh slot tables, pools and sticky map for each test."""

    def setUp(self):
        self.enter([mock.patch.object(serving_slots, "table", serving_slots.SlotTable()),
                    mock.patch.object(serving_slots, "tables", {}),
                    mock.patch.object(serving_slots, "pool", serving_slots.PoolLedger()),
                    mock.patch.object(serving_slots, "pools", {}),
                    mock.patch.object(serving_slots, "chat_instance", {}),
                    mock.patch.object(main_router, "inflight", {}),
                    mock.patch.object(serving_slots, "_serving_cfg", lambda: {})])


class SingleModeTests(SlotStateBase):
    def test_counts_are_unchanged(self):
        self.enter(_single_state(n_slots=4))
        self.assertEqual(serving_slots.main_n_slots(), 4)
        self.assertEqual(serving_slots.total_user_slots(), 4)
        self.assertEqual(serving_slots.user_slot_count(4), 4)

    def test_helper_slot_counted_out_of_the_users(self):
        self.enter(_single_state(n_slots=16))
        with mock.patch.object(serving_slots, "_serving_cfg", lambda: {"helper_slot": 1}):
            self.assertEqual(serving_slots.total_user_slots(), 15)

    def test_helper_call_unchanged(self):
        self.enter(_single_state(n_slots=16))

        async def go():
            async with serving_slots.helper_call() as extra:
                return extra
        with mock.patch.object(serving_slots, "_serving_cfg", lambda: {"helper_slot": 1}):
            self.assertEqual(asyncio.run(go()), {"id_slot": 15, "cache_prompt": True})
        with mock.patch.object(serving_slots, "_serving_cfg", lambda: {}):
            self.assertEqual(asyncio.run(go()), {})

    def test_is_multi_is_false(self):
        self.enter(_single_state())
        self.assertFalse(main_router.is_multi())


class MultiCountTests(SlotStateBase):
    def test_main_n_slots_totals_the_healthy_instances(self):
        self.enter(_multi_state(primary_slots=2, extra_slots=4))
        self.assertEqual(serving_slots.main_n_slots(), 6)
        self.assertEqual(serving_slots.total_user_slots(), 6)

    def test_an_unhealthy_instance_is_not_counted(self):
        self.enter(_multi_state(primary_slots=2, extra_slots=4, extra_healthy=False))
        self.assertEqual(serving_slots.main_n_slots(), 2)
        self.assertEqual(serving_slots.total_user_slots(), 2)

    def test_total_user_slots_with_helper_slot_on_and_off(self):
        self.enter(_multi_state(primary_slots=16, extra_slots=4))
        self.assertEqual(serving_slots.total_user_slots(), 20)                       # helper off: all slots
        with mock.patch.object(serving_slots, "_serving_cfg", lambda: {"helper_slot": 1}):
            self.assertEqual(serving_slots.total_user_slots(), 15 + 3)               # one helper slot each


class PlaceTests(SlotStateBase):
    def setUp(self):
        super().setUp()
        self.enter(_multi_state(primary_slots=2, extra_slots=2))
        self.profile = {"n_slots": 2}

    def _busy(self, index, slots):
        for s in slots:
            serving_slots.table_for(index).begin(s)

    def test_a_chat_stays_on_its_instance(self):
        first = serving_slots.place(1, "a", 0, self.profile)
        self.assertEqual(first, 0)
        self._busy(0, [0])                                  # one slot busy, one free: still sticky
        self.assertEqual(serving_slots.place(1, "a", 0, self.profile), 0)

    def test_a_full_sticky_instance_moves_the_chat(self):
        self.assertEqual(serving_slots.place(1, "a", 0, self.profile), 0)
        self._busy(0, [0, 1])                               # both slots of instance 0 are generating
        self.assertEqual(serving_slots.place(1, "a", 0, self.profile), 1)
        self.assertEqual(serving_slots.chat_instance[(1, "a")], 1)

    def test_a_pool_that_does_not_fit_moves_the_chat(self):
        prof = {"n_slots": 2, "kv_unified": True, "context_size": 1000}     # 970 tokens per instance
        self.assertEqual(serving_slots.place(1, "a", 600, prof), 0)
        serving_slots.pool_for(0).used, serving_slots.pool_for(0).running = 500, 1
        self.assertEqual(serving_slots.place(1, "a", 600, prof), 1)

    def test_a_dead_instance_is_skipped(self):
        serving_slots.chat_instance[(1, "a")] = 1
        with mock.patch.object(state, "extras", [_extra(healthy=False)], create=True):
            self.assertEqual(serving_slots.place(1, "a", 0, self.profile), 0)

    def test_two_chats_of_one_user_can_land_on_different_instances(self):
        self.assertEqual(serving_slots.place(1, "a", 0, self.profile), 0)
        self._busy(0, [0, 1])
        self.assertEqual(serving_slots.place(1, "b", 0, self.profile), 1)
        self.assertEqual(serving_slots.place(1, "a", 0, self.profile), 1)   # a's sticky is full, so it moved too

    def test_user_instance_slot_reports_the_chat_slot(self):
        t = serving_slots.table_for(1)
        serving_slots.chat_instance[(1, "a")] = 1
        slot = t.choose((1, "a"), 2)
        self.assertEqual(serving_slots.user_instance_slot(1, "a"), (1, slot))
        self.assertIsNone(serving_slots.user_instance_slot(2, "a"))


class GateIntegration(SlotStateBase):
    """Through _llm_chat_stream, with two instances of two slots and four users sending at once."""

    def setUp(self):
        super().setUp()
        self.enter(_multi_state(primary_slots=2, extra_slots=2))
        self._raw, self._profile = common._llm_chat_stream_raw, state.profile
        self._serving = common.APP_CONFIG.get("serving")
        common.admission = common._Admission()
        state.profile = {"n_slots": 2}
        common.APP_CONFIG["serving"] = {"queue_enabled": True, "max_inflight_per_user": 1}
        self.seen = []

        async def fake_raw(client, msgs, *a, **k):
            self.seen.append((main_router._instance_choice.get(), common._slot_choice.get(),
                              common._main_slot_fields()))
            await asyncio.sleep(0.03)
            yield ("content_delta", "x")

        common._llm_chat_stream_raw = fake_raw

    def tearDown(self):
        common._llm_chat_stream_raw = self._raw
        state.profile = self._profile
        common.APP_CONFIG["serving"] = self._serving

    def test_four_users_split_over_two_instances_and_slots_stay_in_range(self):
        async def one(uid):
            rc.set_current_user(uid)
            rc.set_current_session(f"s{uid}")
            async for _ in common._llm_chat_stream(state.client, [{"role": "user", "content": "hi"}]):
                pass

        async def main():
            await asyncio.gather(*(one(u) for u in (1, 2, 3, 4)))
        asyncio.run(main())

        insts = [inst for inst, _, _ in self.seen]
        self.assertEqual(sorted(insts.count(i) for i in (0, 1)), [2, 2])
        self.assertEqual(len({(inst, slot) for inst, slot, _ in self.seen}), 4)   # four different slots
        for inst, slot, fields in self.seen:
            self.assertIn(slot, (0, 1))                       # 2 slots per instance
            self.assertEqual(fields["id_slot"], slot)
        self.assertEqual(serving_slots.total_user_slots(), 4)

    def test_context_vars_are_reset_after_the_stream(self):
        async def main():
            rc.set_current_user(9)
            rc.set_current_session("only")
            async for _ in common._llm_chat_stream(state.client, [{"role": "user", "content": "hi"}]):
                pass
            return main_router._instance_choice.get(), common._slot_choice.get(), serving_slots.table_for(0).inflight

        inst, slot, inflight = asyncio.run(main())
        self.assertIsNone(inst)
        self.assertIsNone(slot)
        self.assertFalse(any(inflight.values()))

    def test_no_fallback_slot_is_pinned_without_a_choice(self):
        with mock.patch.object(common, "_slot_choice", common.contextvars.ContextVar("t", default=None)):
            fields = common._main_slot_fields()
        self.assertEqual(fields, {"cache_prompt": True})


if __name__ == "__main__":
    unittest.main()
