import unittest

from tests import eval_kb


class KbEvalGateTests(unittest.TestCase):
    """The KB question set must stay fully green: a miss means a tool returns the wrong evidence."""

    def test_question_set_passes(self):
        report = eval_kb.run()
        failed = [f"{r['category']}: {r['question']} -> {r['output'][:120]!r}" for r in report["results"] if not r["ok"]]
        self.assertEqual(failed, [], f"{report['passed']}/{report['total']} passed")


if __name__ == "__main__":
    unittest.main()
