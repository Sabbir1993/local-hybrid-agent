import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core.web_search import blocklist


class BlocklistTests(unittest.TestCase):
    def fresh(self, **web_cfg):
        tmp = Path(tempfile.mkdtemp())
        for target, val in (("PATH", tmp / "web_blocked.json"), ("_state", None), ("cfg", lambda: web_cfg)):
            p = mock.patch.object(blocklist, target, val)
            p.start()
            self.addCleanup(p.stop)
        return tmp

    def test_blocks_after_two_refusals_and_shares_subdomains(self):
        tmp = self.fresh()
        blocklist.record_failure("https://en.wikipedia.org/wiki/Dhaka", 403)
        self.assertFalse(blocklist.is_blocked("https://en.wikipedia.org/wiki/Dhaka"))
        blocklist.record_failure("https://m.wikipedia.org/wiki/Dhaka", 403)
        self.assertTrue(blocklist.is_blocked("https://de.wikipedia.org/wiki/X"))
        self.assertTrue((tmp / "web_blocked.json").exists())

    def test_other_statuses_ignored(self):
        self.fresh()
        for code in (404, 500, 200):
            blocklist.record_failure("https://example.com/a", code)
            blocklist.record_failure("https://example.com/a", code)
        self.assertFalse(blocklist.is_blocked("https://example.com/a"))

    def test_expiry_and_overrides(self):
        self.fresh(allow_hosts=["ok.com"], blocked_hosts=["bad.com"])
        self.assertTrue(blocklist.is_blocked("https://www.bad.com/x"))
        blocklist.record_failure("https://ok.com/x", 403)
        blocklist.record_failure("https://ok.com/x", 403)
        self.assertFalse(blocklist.is_blocked("https://ok.com/x"))
        blocklist.record_failure("https://slow.com/x", 429)
        self.assertTrue(blocklist.is_blocked("https://slow.com/x"))
        blocklist._state["slow.com"]["until"] = time.time() - 1
        self.assertFalse(blocklist.is_blocked("https://slow.com/x"))


if __name__ == "__main__":
    unittest.main()
