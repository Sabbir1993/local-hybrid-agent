import unittest

from core import shell_tools as s
from core.agent_loop.loop_guard import LoopGuard


class QuotedGtAndRefusalRepeat(unittest.TestCase):
    def test_gt_inside_quotes_is_not_a_redirect(self):
        cmd = 'mysql -h HOST -u USER -p"[PASSWORD]" db -e "SELECT COUNT(*) FROM t WHERE n > 5"'
        self.assertIsNone(s.personal_write_violation(cmd))

    def test_real_redirects_are_still_refused(self):
        self.assertTrue(s.personal_write_violation('mysql db -e "select 1" > out.txt'))
        self.assertTrue(s.personal_write_violation('echo "a>b" > f.txt'))

    def test_a_refused_command_sent_twice_gets_a_nudge(self):
        g = LoopGuard()
        for _ in range(2):
            g.record("run_shell", {"command": "x"}, "error: a Personal Agent cannot run this command: `>`", False)
        self.assertEqual(g.check().level, "nudge")


if __name__ == "__main__":
    unittest.main()
