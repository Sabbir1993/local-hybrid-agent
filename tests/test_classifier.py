"""Laya routing classifier: gating, thresholds, timeouts and how routing uses it.
The model itself is stubbed; scripts/eval_classifier.py measures the real thing."""
import time
import unittest
from unittest import mock

from core import router_config
from core.small_model import APP_CONFIG, classifier as clf


class _Cfg:
    def __init__(self, **classifier_cfg):
        self.value = classifier_cfg

    def __enter__(self):
        self._old = (APP_CONFIG.get("router") or {}).get("classifier")
        APP_CONFIG.setdefault("router", {})["classifier"] = dict(self.value)
        return self

    def __exit__(self, *a):
        if self._old is None:
            APP_CONFIG["router"].pop("classifier", None)
        else:
            APP_CONFIG["router"]["classifier"] = self._old


def stub(label, conf):
    return mock.patch.object(clf, "_predict", return_value=(label, conf))


class ClassifierTests(unittest.TestCase):
    def test_off_by_default_returns_none_without_touching_the_model(self):
        with _Cfg(), mock.patch.object(clf, "_predict") as p:
            self.assertIsNone(clf.request_profile("run the tests"))
            self.assertFalse(clf.enabled())
            p.assert_not_called()

    def test_confident_flag_follows_the_per_question_threshold(self):
        with _Cfg(enabled=True, thresholds={"request_profile": 0.6}):
            with stub("tools", 0.9):
                r = clf.request_profile("run the tests")
                self.assertEqual((r["label"], r["category"], r["confident"]), ("tools", "action", True))
            with stub("chat", 0.45):
                r = clf.request_profile("hmm")
                self.assertEqual((r["category"], r["confident"]), ("question", False))

    def test_category_mapping(self):
        with _Cfg(enabled=True):
            for label, cat in (("tools", "action"), ("create", "creation"), ("chat", "question")):
                with stub(label, 0.99):
                    self.assertEqual(clf.request_profile("x")["category"], cat)

    def test_slow_model_falls_back_to_the_rules(self):
        def slow(question, text):
            time.sleep(0.6)
            return "tools", 0.99
        with _Cfg(enabled=True, timeout_s=0.1), mock.patch.object(clf, "_predict", side_effect=slow):
            t0 = time.time()
            self.assertIsNone(clf.request_profile("run the tests"))
            self.assertLess(time.time() - t0, 0.5)          # gave up at the timeout

    def test_model_failure_returns_none(self):
        with _Cfg(enabled=True), mock.patch.object(clf, "_predict", side_effect=RuntimeError("boom")):
            self.assertIsNone(clf.reply_state("Let me look."))

    def test_empty_text_is_not_classified(self):
        with _Cfg(enabled=True), mock.patch.object(clf, "_predict") as p:
            self.assertIsNone(clf.reply_state("   "))
            p.assert_not_called()

    def test_only_evaluated_questions_may_act_in_active_mode(self):
        with _Cfg(enabled=True, mode="shadow"):
            self.assertFalse(clf.active("request_profile"))
        with _Cfg(enabled=True, mode="active"):
            self.assertTrue(clf.active("request_profile"))
            self.assertFalse(clf.active("reply_state"))       # failed the held-out eval: log only
        with _Cfg(enabled=False, mode="active"):
            self.assertFalse(clf.active("request_profile"))

    def test_log_code_holds_labels_and_confidence_only(self):
        code = clf.log_code({"label": "tools", "confidence": 0.93}, {"label": "announced", "confidence": 0.71})
        self.assertEqual(code, "p:tools@0.93|r:announced@0.71")
        self.assertIsNone(clf.log_code(None, None))

    def test_a_failed_load_is_retried_not_latched(self):
        clf._router = None
        clf._router_failed_at = time.time() - clf._RETRY_AFTER_S - 1
        with mock.patch.dict("sys.modules", {"laya": None}):
            self.assertIsNone(clf._get_router())
        self.assertGreater(clf._router_failed_at, 0)
        clf._router_failed_at = 0.0


class EscalationUsesTheResolvedCategoryTests(unittest.TestCase):
    def test_classifier_category_overrides_the_keyword_guess(self):
        from core import router_policy as rp
        q = "how do I read a file in python?"       # keywords say action
        args = dict(step=0, content="Use open().", tool_calls=[], query=q, is_loop=False)
        self.assertEqual(rp.escalate_reason(**args, category="question"), "")
        # keywords miss it, classifier says the request needs tools
        q2 = "see if the site loads"
        args2 = dict(step=0, content="It probably does.", tool_calls=[], query=q2, is_loop=False)
        self.assertEqual(rp.escalate_reason(**args2), "")
        self.assertEqual(rp.escalate_reason(**args2, category="action"), "no_tool_call")


class ConfigTests(unittest.TestCase):
    def test_validate_classifier_settings(self):
        ok, err = router_config.validate({"classifier": {"enabled": True, "mode": "active", "timeout_s": 1.5}})
        self.assertEqual((err, ok["classifier"]["mode"]), ("", "active"))
        for bad in ({"mode": "on"}, {"enabled": "yes"}, {"timeout_s": 99}, {"nope": 1}):
            self.assertTrue(router_config.validate({"classifier": bad})[1], bad)
        self.assertTrue(router_config.validate({"classifier": "x"})[1])


if __name__ == "__main__":
    unittest.main()
