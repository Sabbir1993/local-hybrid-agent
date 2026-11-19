import unittest

from core import shell_tools as s
from core.agent_loop.loop_guard import LoopGuard


class QuotedGtAndRefusalRepeat(unittest.TestCase):
    def test_gt_inside_quotes_is_not_a_redirect(self):
        cmd = 'mysql -h HOST -u USER -p"[PASSWORD]" db -e "SELECT COUNT(*) FROM t WHERE n > 5"'
        self.assertIsNone(s.personal_write_violation(cmd))

    def test_gt_inside_an_escaped_or_doubled_quote_is_not_a_redirect(self):
        # pentest payloads: < and > live inside a JSON / HTML string escaped with \" or doubled ""
        self.assertIsNone(s.personal_write_violation(
            r'''$r = Select-String -Path a.html -Pattern "value=\">([^\"]+)<\"" | ForEach-Object { $_.Line }'''))
        self.assertIsNone(s.personal_write_violation(
            'Write-Output "found ""<script>alert(1)</script>"" here"'))

    def test_real_redirects_are_still_refused(self):
        self.assertTrue(s.personal_write_violation('mysql db -e "select 1" > out.txt'))
        self.assertTrue(s.personal_write_violation('echo "a>b" > f.txt'))
        # a real redirect that comes after a quoted ">" must still be caught
        self.assertTrue(s.personal_write_violation('Write-Output "n > 5" > dump.txt'))

    def test_a_refused_command_sent_twice_gets_a_nudge(self):
        g = LoopGuard()
        for _ in range(2):
            g.record("run_shell", {"command": "x"}, "error: a Personal Agent cannot run this command: `>`", False)
        self.assertEqual(g.check().level, "nudge")


if __name__ == "__main__":
    unittest.main()
