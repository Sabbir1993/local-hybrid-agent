import unittest

from core import router_config, router_policy as rp


class PlanFirstTests(unittest.TestCase):
    def test_default_plans_creation_only(self):
        cfg = dict(rp.DEFAULTS)
        self.assertEqual(rp.plan_first_reason("creation", cfg), "plan_first")
        for cat in ("action", "question", "other", "greeting"):
            self.assertEqual(rp.plan_first_reason(cat, cfg), "")

    def test_off_when_no_categories_or_zero_steps(self):
        self.assertEqual(rp.plan_first_reason("creation", dict(rp.DEFAULTS, plan_first_categories=[])), "")
        self.assertEqual(rp.plan_first_reason("creation", dict(rp.DEFAULTS, plan_first_max_steps=0)), "")

    def test_not_the_same_as_start_on_main(self):
        cfg = dict(rp.DEFAULTS)
        self.assertEqual(rp.main_first_reason("creation", dict(cfg, adaptive=False)), "")

    def test_validation(self):
        out, err = router_config.validate({"plan_first_categories": ["Creation", "action"], "plan_first_max_steps": 2})
        self.assertEqual(err, "")
        self.assertEqual(out, {"plan_first_categories": ["action", "creation"], "plan_first_max_steps": 2})
        _, err = router_config.validate({"plan_first_categories": ["nonsense"]})
        self.assertIn("unknown categories", err)
        _, err = router_config.validate({"plan_first_max_steps": 9})
        self.assertIn("between 0 and 6", err)

    def test_settings_expose_the_new_keys(self):
        s = router_config.settings()
        self.assertIn("plan_first_categories", s)
        self.assertIn("plan_first_max_steps", s)


if __name__ == "__main__":
    unittest.main()
