"""tests/test_router_tuner_rules.py - F2: the tuner loop produces from telemetry.

The auto-apply/watch/rollback machinery is covered (test_router_autotune.py),
but the RULES that turn telemetry into suggestions were not: no test ever fed
realistic route_events through _category_rules/_threshold_rule/_streak_rule
and got a suggestion out, and the apply/dismiss endpoint was untested. If the
rules misfire on real data (bad SQL, wrong thresholds) the whole loop is
telemetry theater. These tests seed realistic telemetry and prove each stage.

Run: python -m unittest tests.test_router_tuner_rules -v
"""

import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import route_log, router_tuner as rt


class _SeededDb(unittest.TestCase):
    def setUp(self):
        # route_log.* and router_tuner.* each hold their own `_usage_db`
        # binding (from .db import _usage_db): both must point at the temp DB
        self._orig_log = route_log._usage_db
        self._orig_tuner = rt._usage_db
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        route_log._usage_db = conn
        rt._usage_db = conn
        route_log._init()

    def tearDown(self):
        route_log._usage_db = self._orig_log
        rt._usage_db = self._orig_tuner

    def exec_step0(self, category, n, esc_rate=0.0, reason="executor_default"):
        """n executor first-steps, a share of them escalated."""
        base = time.time() - 1000
        n_esc = round(n * esc_rate)
        for i in range(n):
            esc = 1 if i < n_esc else 0
            route_log._usage_db.execute(
                "INSERT INTO route_events (ts, run_id, step, category, lane, reason, "
                "escalated, escalate_reason, duration_s) VALUES (?,?,?,?,?,?,?,?,?)",
                (base + i, f"r-{category}-{i}-{time.time_ns()}", 0, category, "executor",
                 reason, esc, "loop" if esc else None, 2.0))
        route_log._usage_db.commit()

    def router_hits(self, n, ok_rate=1.0, down_rate=0.0):
        base = time.time() - 1000
        for i in range(n):
            ok = 1 if i < round(n * ok_rate) else 0
            rid = f"rh-{i}-{time.time_ns()}"
            route_log._usage_db.execute(
                "INSERT INTO route_runs (run_id, ts, user_id, mode, category, steps, outcome, rating) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rid, base + i, 1, "m", "action", 1, "answered",
                 -1 if i < round(n * down_rate) else (1 if i % 2 else None)))
            route_log._usage_db.execute(
                "INSERT INTO route_events (ts, run_id, step, category, lane, reason, tool_name, tool_ok) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (base + i, rid, 0, "action", "router", "router_hit", "web_search", ok))
        route_log._usage_db.commit()

    def repeat_runs(self, n, fail_rate=1.0):
        base = time.time() - 1000
        for i in range(n):
            rid = f"rs-{i}-{time.time_ns()}"
            bad = i < round(n * fail_rate)
            route_log._usage_db.execute(
                "INSERT INTO route_runs (run_id, ts, user_id, mode, category, steps, outcome) "
                "VALUES (?,?,?,?,?,?,?)",
                (rid, base + i, 1, "m", "action", 5, "loop" if bad else "answered"))
            route_log._usage_db.execute(
                "INSERT INTO route_events (ts, run_id, step, category, lane, reason) "
                "VALUES (?,?,?,?,?,?)", (base + i, rid, 2, "action", "main", "repeat_streak"))
        route_log._usage_db.commit()


class CategoryRuleTests(_SeededDb):
    def test_heavy_escalation_proposes_start_on_main(self):
        self.exec_step0("action", 30, esc_rate=0.8)
        out = rt._category_rules({"start_on_main_categories": []}, time.time() - 86400)
        self.assertEqual(len(out), 1)
        key, _cur, proposed, ev = out[0]
        self.assertEqual((key, proposed), ("start_on_main_categories", ["action"]))
        self.assertGreaterEqual(ev["escalation_rate"], 0.6)

    def test_quiet_category_proposes_nothing(self):
        self.exec_step0("action", 30, esc_rate=0.1)
        self.assertEqual(rt._category_rules({"start_on_main_categories": []}, time.time() - 86400), [])

    def test_below_min_samples_proposes_nothing(self):
        self.exec_step0("action", 29, esc_rate=1.0)
        self.assertEqual(rt._category_rules({"start_on_main_categories": []}, time.time() - 86400), [])

    def test_greeting_never_proposed(self):
        self.exec_step0("greeting", 30, esc_rate=1.0)
        self.assertEqual(rt._category_rules({"start_on_main_categories": []}, time.time() - 86400), [])

    def test_already_on_main_not_reproposed(self):
        self.exec_step0("action", 30, esc_rate=1.0)
        out = rt._category_rules({"start_on_main_categories": ["action"]}, time.time() - 86400)
        self.assertFalse([o for o in out if o[0] == "start_on_main_categories" and o[2] == ["action"]])


class ThresholdRuleTests(_SeededDb):
    def test_failing_router_tools_raise_the_threshold(self):
        from core.small_model import APP_CONFIG
        self.router_hits(30, ok_rate=0.5)
        with mock.patch.dict(APP_CONFIG, {"router": {"confidence_threshold": 0.7}}):
            out = rt._threshold_rule({}, time.time() - 86400)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][:3], ("confidence_threshold", 0.7, 0.75))

    def test_reliable_router_tools_lower_it(self):
        from core.small_model import APP_CONFIG
        self.router_hits(30, ok_rate=1.0)
        with mock.patch.dict(APP_CONFIG, {"router": {"confidence_threshold": 0.7}}):
            out = rt._threshold_rule({}, time.time() - 86400)
        # lowering also needs enough router_pass volume
        route_log._usage_db.execute(
            "INSERT INTO route_events (ts, run_id, step, category, lane, reason) "
            "SELECT ?, run_id || '-p', 0, 'action', 'main', 'router_pass' FROM route_events LIMIT 30",
            (time.time() - 500,))
        route_log._usage_db.commit()
        with mock.patch.dict(APP_CONFIG, {"router": {"confidence_threshold": 0.7}}):
            out = rt._threshold_rule({}, time.time() - 86400)
        self.assertTrue(out and out[0][2] < 0.7)

    def test_below_min_samples_quiet(self):
        self.router_hits(29, ok_rate=0.0)
        self.assertEqual(rt._threshold_rule({}, time.time() - 86400), [])


class StreakRuleTests(_SeededDb):
    def test_doomed_repeats_lower_the_limit(self):
        self.repeat_runs(30, fail_rate=0.8)
        out = rt._streak_rule({"repeat_streak_limit": 2}, time.time() - 86400)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][:3], ("repeat_streak_limit", 2, 1))

    def test_rescued_runs_keep_the_limit(self):
        self.repeat_runs(30, fail_rate=0.1)
        self.assertEqual(rt._streak_rule({"repeat_streak_limit": 2}, time.time() - 86400), [])

    def test_already_at_one_is_terminal(self):
        self.repeat_runs(30, fail_rate=1.0)
        self.assertEqual(rt._streak_rule({"repeat_streak_limit": 1}, time.time() - 86400), [])


class RunTunerTests(_SeededDb):
    def test_telemetry_becomes_a_pending_suggestion(self):
        self.exec_step0("action", 30, esc_rate=0.9)
        with mock.patch.object(rt, "rcfg", return_value={"start_on_main_categories": []}):
            ids = rt.run_tuner(days=2)
        self.assertEqual(len(ids), 1)
        sug = route_log.get_suggestion(ids[0])
        self.assertEqual((sug["status"], sug["key"], sug["proposed"]),
                         ("pending", "start_on_main_categories", ["action"]))

    def test_second_run_dedupes(self):
        self.exec_step0("action", 30, esc_rate=0.9)
        with mock.patch.object(rt, "rcfg", return_value={"start_on_main_categories": []}):
            first = rt.run_tuner(days=2)
            second = rt.run_tuner(days=2)
        self.assertEqual(first, second)
        self.assertEqual(len(route_log.list_suggestions("pending")), 1)


class DecideEndpointTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import deps
        from core.auth import Principal
        from routes.capabilities.endpoints import router_cfg
        self._orig = route_log._usage_db
        route_log._usage_db = sqlite3.connect(":memory:", check_same_thread=False)
        route_log._usage_db.row_factory = sqlite3.Row
        route_log._init()
        app = FastAPI()
        app.include_router(router_cfg.router)
        me = Principal(id=7, username="u7", display_name="u7", is_super_admin=False,
                       must_change_password=False, role_names=[],
                       permission_keys={"settings.router.configure"})
        app.dependency_overrides[deps.get_current_user] = lambda: me
        self.patches = [
            mock.patch.object(router_cfg, "audit_log", lambda *a, **k: None),
            mock.patch.object(router_cfg, "_validate_router_changes",
                              lambda changes: (dict(changes), "")),
            mock.patch.object(router_cfg, "_router_settings",
                              lambda: {"start_on_main_categories": []}),
            mock.patch.object(router_cfg, "_write_router",
                              lambda changes: ({"start_on_main_categories": []}, None)),
            mock.patch.object(router_cfg, "_router_view", lambda: {"ok": True}),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        for p in self.patches:
            p.stop()
        route_log._usage_db = self._orig

    def _suggest(self):
        return route_log.add_suggestion("start_on_main_categories", [], ["action"], {"why": "t"})

    def test_bad_decision_400(self):
        self.assertEqual(self.client.post("/control/router/suggestions/1/maybe").status_code, 400)

    def test_unknown_or_decided_404(self):
        self.assertEqual(self.client.post("/control/router/suggestions/4242/apply").status_code, 404)

    def test_dismiss_marks_and_audits(self):
        sid = self._suggest()
        r = self.client.post(f"/control/router/suggestions/{sid}/dismiss")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(route_log.get_suggestion(sid)["status"], "dismissed")
        self.assertEqual(self.client.post(f"/control/router/suggestions/{sid}/apply").status_code, 404)

    def test_apply_writes_and_marks(self):
        sid = self._suggest()
        r = self.client.post(f"/control/router/suggestions/{sid}/apply")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(route_log.get_suggestion(sid)["status"], "applied")

    def test_stale_setting_is_409_not_applied(self):
        sid = self._suggest()
        from routes.capabilities.endpoints import router_cfg
        with mock.patch.object(router_cfg, "_router_settings",
                               lambda: {"start_on_main_categories": ["question"]}):
            r = self.client.post(f"/control/router/suggestions/{sid}/apply")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(route_log.get_suggestion(sid)["status"], "stale")


if __name__ == "__main__":
    unittest.main()
