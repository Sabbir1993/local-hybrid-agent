"""tests/test_multi_status.py - the multi-instance main model as the status, telemetry, log and preflight views show it.

Covers: the context window of a chat is the smallest window over the healthy instances; telemetry lists every
instance; /control/status reports instances and the caller's own slot; /control/log picks an instance (404 for an
unknown one); /control/preflight plans every instance. Single mode keeps its old output (no new keys).
Run: python -m pytest tests/test_multi_status.py -q -p no:cacheprovider
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core import context_budget
from core import main_router
from core import serving_slots
from core import telemetry as tm
from core.auth import Principal
from core.state import state
from routes.control import server_endpoints as se
from routes.control import status_endpoints as st


def _principal(uid=1, super_admin=False):
    return Principal(id=uid, username=f"u{uid}", display_name=f"u{uid}", is_super_admin=super_admin,
                     must_change_password=False, role_names=[], permission_keys=set())


def _body(resp):
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body).decode("utf-8"))
    return resp


def _inst_status(index, port, n_slots, healthy=True, gpus=None):
    return {"index": index, "port": port, "gpus": gpus if gpus is not None else [index], "n_slots": n_slots,
            "healthy": healthy, "loading": False, "pid": None, "uptime_s": None, "restart_count": 0,
            "degraded": False, "error": None}


def _live(rows):
    """main_router.live_instances() shape from [(index, port, n_slots, healthy)]."""
    return [{"index": i, "port": p, "n_slots": n, "healthy": h} for i, p, n, h in rows]


class MinWindowAcrossInstances(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        context_budget.reset("main")

    def tearDown(self):
        context_budget.reset("main")

    async def test_multi_uses_smallest_window_of_healthy_instances(self):
        slots = {0: [{"id": 0, "n_ctx": 131072}], 1: [{"id": 0, "n_ctx": 65536}, {"id": 1, "n_ctx": 65536}],
                 2: [{"id": 0, "n_ctx": 16384}]}     # instance 2 is down: its window must not count
        live = _live([(0, 8080, 1, True), (1, 8191, 2, True), (2, 8192, 1, False)])
        with mock.patch.object(main_router, "is_multi", lambda: True), \
                mock.patch.object(main_router, "live_instances", lambda: live), \
                mock.patch.object(serving_slots, "all_main_slots", mock.AsyncMock(return_value=slots)):
            win = await context_budget.probe_window("main", None)
        self.assertEqual(win, 65536)
        self.assertEqual(context_budget._state("main")["window"], 65536)

    async def test_single_mode_probe_unchanged(self):
        class Resp:
            status_code = 200

            def json(self):
                return [{"id": 0, "n_ctx": 131072}]

        class Client:
            async def get(self, path, timeout=None):
                return Resp()

        all_slots = mock.AsyncMock(side_effect=AssertionError("not called in single mode"))
        with mock.patch.object(main_router, "is_multi", lambda: False), \
                mock.patch.object(serving_slots, "all_main_slots", all_slots):
            win = await context_budget.probe_window("main", Client())
        self.assertEqual(win, 131072)
        all_slots.assert_not_called()

    async def test_no_client_and_single_mode_is_none(self):
        with mock.patch.object(main_router, "is_multi", lambda: False):
            self.assertIsNone(await context_budget.probe_window("main", None))


class TelemetryInstances(unittest.TestCase):
    def setUp(self):
        self.t1 = serving_slots.SlotTable()
        self.t1.choose("u-a", 2)

    def _serving(self, multi):
        live = _live([(0, 8080, 4, True), (1, 8191, 2, True), (2, 8192, 2, False)])
        per = {0: [{"id": 0, "n_ctx": 8192, "is_processing": True, "n_prompt_tokens": 10,
                    "next_token": [{"n_decoded": 2}]}],
               1: [{"id": 0, "n_ctx": 4096, "is_processing": False, "n_prompt_tokens": 0}],
               2: None}
        status = [_inst_status(0, 8080, 4), _inst_status(1, 8191, 2), _inst_status(2, 8192, 2, healthy=False)]
        profile = {"kv_unified": True, "n_slots": 4, "context_size": 65536}
        with mock.patch.object(main_router, "is_multi", lambda: multi), \
                mock.patch.object(main_router, "live_instances", lambda: live), \
                mock.patch.object(serving_slots, "all_main_slots", mock.AsyncMock(return_value=per)), \
                mock.patch.dict(serving_slots.tables, {1: self.t1}, clear=False), \
                mock.patch.dict(main_router.inflight, {1: 3}, clear=True), \
                mock.patch.object(state, "instances_status", lambda: status), \
                mock.patch.object(state, "profile", profile), \
                mock.patch.object(state, "process", mock.Mock(poll=lambda: None)), \
                mock.patch.object(tm, "get_gpu_stats", mock.AsyncMock(return_value={})), \
                mock.patch.object(tm, "get_gpu_sensors", mock.AsyncMock(return_value=None)):
            snap = asyncio.run(tm._compute_telemetry_snapshot())
        return snap

    def test_multi_lists_every_instance(self):
        serving = self._serving(multi=True)["serving"]
        rows = serving["instances"]
        self.assertEqual([r["index"] for r in rows], [0, 1, 2])
        self.assertEqual(rows[1]["port"], 8191)
        self.assertEqual(rows[1]["owned"], 1)
        self.assertEqual(rows[1]["inflight"], 3)
        self.assertEqual(rows[1]["n_slots"], 2)
        self.assertTrue(rows[1]["healthy"])
        self.assertFalse(rows[2]["healthy"])
        self.assertEqual(rows[2]["slots_fill"], [])
        self.assertEqual(rows[0]["slots_fill"][0]["n_past"], 12)
        self.assertIn("pool_used", rows[0])
        self.assertIn("pool_capacity", rows[0])

    def test_multi_top_level_slots_fill_is_tagged_and_concatenated(self):
        snap = self._serving(multi=True)
        fill = snap["serving"]["slots_fill"]
        self.assertEqual(sorted({r["instance"] for r in fill}), [0, 1])
        self.assertEqual(len(fill), 2)
        self.assertIn("slot_table", snap["serving"])        # existing keys kept

    def test_single_mode_has_no_instances_section(self):
        with mock.patch.object(serving_slots, "main_slots", mock.AsyncMock(return_value=[
                {"id": 0, "n_ctx": 8192, "is_processing": False, "n_prompt_tokens": 0}])):
            snap = self._serving(multi=False)
        self.assertNotIn("instances", snap["serving"])
        self.assertTrue(all("instance" not in r for r in snap["serving"]["slots_fill"]))


class StatusKeys(unittest.IsolatedAsyncioTestCase):
    def _patches(self, multi, process=None):
        status_rows = [_inst_status(0, 8080, 4), _inst_status(1, 8191, 2, gpus=[1])]
        return [
            mock.patch.object(main_router, "is_multi", lambda: multi),
            mock.patch.object(state, "instances_status", lambda: status_rows if multi else status_rows[:1]),
            mock.patch.object(state, "process", process),
            mock.patch.object(state, "profile", None),
            mock.patch("core.cloud.cloud_lane", lambda *a, **k: None),
            mock.patch.object(st, "refresh_hw_async", mock.AsyncMock(return_value=None)),
            mock.patch.object(st, "get_hardware_engine_summary", lambda: {}),
        ]

    async def _status(self, multi, user, process=None, slots=None):
        patches = self._patches(multi, process)
        if slots is not None:
            patches.append(mock.patch.object(serving_slots, "main_slots", mock.AsyncMock(side_effect=slots)))
        for p in patches:
            p.start()
        try:
            return await st.get_system_status(user)
        finally:
            for p in patches:
                p.stop()

    async def test_single_mode_adds_only_the_two_instance_keys(self):
        proc = mock.Mock(poll=lambda: None, pid=123)
        out = await self._status(False, _principal(), process=proc,
                                 slots=mock.AsyncMock(return_value=[{"id": 0, "n_ctx": 32768}]))
        self.assertEqual(out["main_mode"], "single")
        self.assertEqual(len(out["main_instances"]), 1)
        self.assertNotIn("my_instance", out)
        self.assertIn("context", out)

    async def test_multi_reports_instances_and_callers_own_slot(self):
        slot_rows = {0: [{"id": 0, "n_ctx": 8192}], 1: [{"id": 0, "n_ctx": 4096}, {"id": 1, "n_ctx": 4096},
                                                       {"id": 2, "n_ctx": 4096, "n_prompt_tokens": 100,
                                                        "next_token": [{"n_decoded": 5}]}]}

        async def fake_slots(timeout=1.5, index=None):
            return slot_rows[0 if index is None else index]

        with mock.patch.object(serving_slots, "user_instance_slot", lambda uid, session=None: (1, 2)):
            out = await self._status(True, _principal(7), process=None, slots=fake_slots)
        self.assertEqual(out["main_mode"], "multi")
        self.assertEqual([i["index"] for i in out["main_instances"]], [0, 1])
        self.assertEqual(out["my_instance"], 2)                     # instance index 1, shown 1-based
        self.assertEqual(out["context"]["n_ctx"], 4096)
        self.assertEqual(out["context"]["n_past"], 105)

    async def test_multi_without_any_slot_falls_back_to_primary(self):
        async def fake_slots(timeout=1.5, index=None):
            return [{"id": 0, "n_ctx": 8192}] if index is None else None

        with mock.patch.object(serving_slots, "user_instance_slot", lambda uid, session=None: None), \
                mock.patch.object(serving_slots, "latest_user_instance", lambda uid: None):
            out = await self._status(True, _principal(7), process=None, slots=fake_slots)
        self.assertIsNone(out["my_instance"])
        self.assertEqual(out["context"]["n_ctx"], 8192)


class InstanceLog(unittest.IsolatedAsyncioTestCase):
    def _extra(self, index, lines, running=True):
        return mock.Mock(index=index, log_tail=lambda n: lines[-n:], is_running=lambda: running, last_error=None)

    async def test_selects_extra_instance_log(self):
        extras = [self._extra(1, ["one-a", "one-b"])]
        with mock.patch.object(state, "extras", extras), \
                mock.patch.object(state, "log_tail", lambda n: ["primary"]):
            out = await se.server_log(n=10, instance=1, user=_principal(super_admin=True))
        self.assertEqual(out["lines"], ["one-a", "one-b"])
        self.assertTrue(out["running"])

    async def test_zero_is_primary(self):
        with mock.patch.object(state, "extras", [self._extra(1, ["x"])]), \
                mock.patch.object(state, "log_tail", lambda n: ["primary"]):
            out = await se.server_log(n=10, instance=0, user=_principal(super_admin=True))
        self.assertEqual(out["lines"], ["primary"])

    async def test_unknown_instance_is_404(self):
        with mock.patch.object(state, "extras", [self._extra(1, ["x"])]):
            with self.assertRaises(HTTPException) as ctx:
                await se.server_log(n=10, instance=5, user=_principal(super_admin=True))
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_non_super_admin_still_refused(self):
        with mock.patch.object(se, "audit_log", lambda *a, **k: None):
            with self.assertRaises(HTTPException) as ctx:
                await se.server_log(n=10, instance=1, user=_principal(super_admin=False))
        self.assertEqual(ctx.exception.status_code, 403)


class PreflightPerInstance(unittest.IsolatedAsyncioTestCase):
    def _profile(self, multi):
        p = {"name": "m", "model_path": "/models/m.gguf", "n_slots": 4, "gpu_devices": [0, 1],
             "tuned": {}}
        if multi:
            p["tuned"] = {"main_mode": "multi", "main_instances": [
                {"gpus": [0], "n_slots": 4, "tensor_split": None},
                {"gpus": [1], "n_slots": 2, "tensor_split": None}]}
        return p

    async def test_multi_plans_each_server_and_reports_all_ok(self):
        calls = []

        def fake_plan(profile, overrides=None, **kw):
            calls.append(dict(overrides or {}))
            fits = (overrides or {}).get("gpu_devices") != [1]
            return {"status": "fits" if fits else "nofit", "allow": fits, "message": "", "effective": {}}

        with mock.patch.object(state, "profile", self._profile(True)), \
                mock.patch("core.config.apply_runtime", lambda p: p), \
                mock.patch.object(se.vram, "plan_launch", fake_plan):
            out = await se.preflight(target=None, engine=None, ctx=None, ngl=None, kv=None, user=_principal())
        self.assertEqual(len(out["instances"]), 2)
        self.assertEqual(out["instances"][0]["ok"], True)
        self.assertEqual(out["instances"][1]["ok"], False)
        self.assertEqual(out["instances"][1]["gpus"], [1])
        self.assertEqual(out["instances"][1]["n_slots"], 2)
        self.assertEqual(out["instances"][1]["port"], 8191)
        self.assertFalse(out["allow"])
        self.assertFalse(out["ok"])
        self.assertEqual(calls[0]["n_slots"], 4)
        self.assertEqual(calls[0]["tensor_split"], "1")
        self.assertEqual(calls[1]["gpu_devices"], [1])

    async def test_single_mode_is_one_plan_without_instances(self):
        plan = {"status": "fits", "allow": True, "message": "", "effective": {}}
        mocked = mock.Mock(return_value=plan)
        with mock.patch.object(state, "profile", self._profile(False)), \
                mock.patch.object(se.vram, "plan_launch", mocked):
            out = await se.preflight(target=None, engine=None, ctx=None, ngl=None, kv=None, user=_principal())
        self.assertEqual(out, plan)
        self.assertNotIn("instances", out)
        mocked.assert_called_once()


if __name__ == "__main__":
    unittest.main()
