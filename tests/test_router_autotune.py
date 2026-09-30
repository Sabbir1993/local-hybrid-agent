"""Tuner auto-apply, watch window and rollback (core/router_tuner.py)."""
import sqlite3
import time
import unittest
from unittest import mock

from core import route_log, router_tuner as rt


class _Cfg:
    """Stands in for config/app.json: settings() / write() / validate()."""
    def __init__(self, **kw):
        self.d = {"auto_apply": True, "start_on_main_categories": [], "repeat_streak_limit": 2,
                  "confidence_threshold": 0.85}
        self.d.update(kw)
        self.writes = []

    def settings(self):
        return dict(self.d)

    def write(self, changes):
        old = {k: self.d.get(k) for k in changes}
        self.d.update(changes)
        self.writes.append(dict(changes))
        return old, ""

    def validate(self, changes):
        return dict(changes), ""


class TunerTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db = route_log._usage_db
        route_log._usage_db = sqlite3.connect(":memory:")
        route_log._init()
        self.cfg = _Cfg()
        self.audit = mock.Mock()
        self.patches = [
            mock.patch.object(rt.router_config, "settings", self.cfg.settings),
            mock.patch.object(rt.router_config, "write", self.cfg.write),
            mock.patch.object(rt.router_config, "validate", self.cfg.validate),
            mock.patch.object(rt, "audit_log", self.audit),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        route_log._usage_db = self._orig_db

    # helpers
    def runs(self, n, bad=0, category="action", ts=None):
        base = ts or time.time() - 1000
        for i in range(n):
            rid = f"{category}-{base}-{i}-{time.time_ns()}"
            route_log._usage_db.execute(
                "INSERT INTO route_runs (run_id, ts, user_id, mode, category, steps, outcome) VALUES (?,?,?,?,?,?,?)",
                (rid, base + i * 0.001, 1, "m", category, 1, "loop" if i < bad else "answered"))
        route_log._usage_db.commit()

    def suggest(self, key="start_on_main_categories", current=(), proposed=("action",)):
        return route_log.add_suggestion(key, list(current), list(proposed), {"why": "test"})


class AutoApplyTests(TunerTestCase):
    def test_off_by_default_applies_nothing(self):
        self.cfg.d["auto_apply"] = False
        self.runs(30)
        self.suggest()
        self.assertEqual(rt.auto_apply(), [])
        self.assertEqual(self.cfg.writes, [])

    def test_applies_with_enough_history_audits_and_starts_watching(self):
        self.runs(30, bad=3)
        sid = self.suggest()
        ids = rt.auto_apply()
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.cfg.d["start_on_main_categories"], ["action"])
        row = route_log.get_applied(ids[0])
        self.assertEqual((row["status"], row["category"], row["old"], row["new"]),
                         ("watching", "action", [], ["action"]))
        self.assertEqual(row["baseline"], {"runs": 30, "bad": 3})
        self.assertEqual(route_log.get_suggestion(sid)["status"], "applied")
        self.assertEqual(self.audit.call_args.kwargs["action"], "router.tune.auto_apply")

    def test_too_little_history_is_left_for_a_person(self):
        self.runs(5)
        sid = self.suggest()
        self.assertEqual(rt.auto_apply(), [])
        self.assertEqual(route_log.get_suggestion(sid)["status"], "pending")
        self.assertEqual(self.cfg.writes, [])

    def test_only_one_change_is_watched_at_a_time(self):
        self.runs(30)
        self.suggest()
        self.suggest("repeat_streak_limit", current=[2], proposed=[1])
        self.assertEqual(len(rt.auto_apply()), 1)
        self.assertEqual(rt.auto_apply(), [])
        self.assertEqual(len(self.cfg.writes), 1)

    def test_stale_suggestion_is_not_applied(self):
        self.runs(30)
        sid = self.suggest(current=("greeting",))       # live value is [] not ["greeting"]
        self.assertEqual(rt.auto_apply(), [])
        self.assertEqual(route_log.get_suggestion(sid)["status"], "stale")

    def test_a_change_that_was_just_rolled_back_is_not_tried_again(self):
        self.runs(30)
        self.suggest()
        aid = rt.auto_apply()[0]
        rt.rollback_applied(aid, "test")
        self.assertTrue(route_log.recently_rolled_back("start_on_main_categories", ["action"]))
        self.suggest()
        self.assertEqual(rt.auto_apply(), [])


class WatchAndRollbackTests(TunerTestCase):
    def _applied(self, baseline_bad=2):
        self.runs(30, bad=baseline_bad, ts=time.time() - 5000)
        self.suggest()
        aid = rt.auto_apply()[0]
        # applied "now"; runs recorded after this moment belong to the after-window
        route_log._usage_db.execute("UPDATE router_applied SET applied_at = ? WHERE id = ?", (time.time() - 100, aid))
        route_log._usage_db.commit()
        return aid

    def test_clearly_worse_after_the_change_rolls_it_back_and_audits(self):
        aid = self._applied()
        self.runs(20, bad=10, ts=time.time() - 50)            # 50% bad vs ~7% before
        verdicts = rt.evaluate_applied()
        self.assertEqual(verdicts, [(aid, "rolled_back")])
        self.assertEqual(self.cfg.d["start_on_main_categories"], [])
        row = route_log.get_applied(aid)
        self.assertEqual((row["status"], row["result"]["reason"]), ("rolled_back", "quality_worse"))
        self.assertEqual(self.audit.call_args.kwargs["action"], "router.tune.rollback")

    def test_not_worse_is_kept(self):
        aid = self._applied()
        self.runs(20, bad=1, ts=time.time() - 50)
        self.assertEqual(rt.evaluate_applied(), [(aid, "kept")])
        self.assertEqual(self.cfg.d["start_on_main_categories"], ["action"])
        self.assertEqual(route_log.get_applied(aid)["status"], "kept")

    def test_no_verdict_before_enough_runs(self):
        aid = self._applied()
        self.runs(5, bad=5, ts=time.time() - 50)
        self.assertEqual(rt.evaluate_applied(), [])
        self.assertEqual(route_log.get_applied(aid)["status"], "watching")

    def test_too_little_traffic_is_kept_after_the_watch_period(self):
        aid = self._applied()
        later = time.time() + (rt.WATCH_DAYS + 1) * 86400
        self.assertEqual(rt.evaluate_applied(now=later), [(aid, "kept")])

    def test_rollback_refuses_when_someone_changed_the_setting_since(self):
        aid = self._applied()
        self.cfg.d["start_on_main_categories"] = ["question"]      # an admin edit
        ok, _ = rt.rollback_applied(aid, "admin")
        self.assertFalse(ok)
        self.assertEqual(route_log.get_applied(aid)["status"], "superseded")
        self.assertEqual(self.cfg.d["start_on_main_categories"], ["question"])

    def test_manual_rollback(self):
        aid = self._applied()
        ok, _ = rt.rollback_applied(aid, "admin")
        self.assertTrue(ok)
        self.assertEqual(self.cfg.d["start_on_main_categories"], [])
        self.assertFalse(rt.rollback_applied(aid, "admin")[0])     # already rolled back

    def test_cycle_judges_every_tick_but_suggests_once_a_day(self):
        with mock.patch.object(rt, "run_tuner", return_value=[]) as tune, \
                mock.patch.object(rt, "evaluate_applied") as ev:
            t0 = time.time()
            last = rt.tuner_cycle(0.0, now=t0)
            self.assertEqual((tune.call_count, ev.call_count), (1, 1))
            last = rt.tuner_cycle(last, now=t0 + 3600)
            self.assertEqual((tune.call_count, ev.call_count), (1, 2))
            rt.tuner_cycle(last, now=t0 + rt.TUNE_INTERVAL_S + 1)
            self.assertEqual(tune.call_count, 2)


class RunQualityTests(TunerTestCase):
    def test_counts_bad_and_thumbs_down_and_ignores_cancelled(self):
        self.runs(10, bad=2)
        route_log._usage_db.execute("UPDATE route_runs SET rating = -1 WHERE outcome = 'answered' AND rowid = "
                                    "(SELECT MIN(rowid) FROM route_runs WHERE outcome = 'answered')")
        route_log._usage_db.execute("INSERT INTO route_runs (run_id, ts, category, outcome) "
                                    "VALUES ('x', ?, 'action', 'cancelled')", (time.time(),))
        route_log._usage_db.commit()
        self.assertEqual(route_log.run_quality("action"), {"runs": 10, "bad": 3})
        self.assertEqual(route_log.run_quality("question"), {"runs": 0, "bad": 0})


if __name__ == "__main__":
    unittest.main()
