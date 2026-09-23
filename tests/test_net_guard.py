"""tests/test_net_guard.py - SSRF guard (core/net_guard.py) and path sandbox.

Run: python -m unittest tests.test_net_guard -v
"""

import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import net_guard
from core.net_guard import BlockedURLError, check_url


class CheckUrlTests(unittest.TestCase):
    def test_internal_targets_blocked(self):
        for url in ("http://127.0.0.1:8090/v1/models", "http://localhost:8000/control/status",
                    "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
                    "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                    "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://printer.local/",
                    "http://db.internal/", "file:///C:/Windows/win.ini", "gopher://x/"):
            with self.assertRaises(BlockedURLError, msg=url):
                check_url(url)

    def test_public_literal_allowed(self):
        self.assertEqual(check_url("https://8.8.8.8/"), "https://8.8.8.8/")


class _Redirector(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://blocked.test/secret")
        self.end_headers()

    def log_message(self, *a):
        pass


class RedirectTests(unittest.TestCase):
    def test_every_redirect_hop_is_checked(self):
        srv = HTTPServer(("127.0.0.1", 0), _Redirector)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        seen = []
        real = net_guard.check_url

        def fake_check(url):        # let the local test server through, record hops
            seen.append(url)
            if "blocked.test" in url:
                raise BlockedURLError("blocked hop")
            return url

        net_guard.check_url = fake_check
        try:
            with self.assertRaises(BlockedURLError):
                net_guard.guarded_get(f"http://127.0.0.1:{srv.server_port}/start", 5)
        finally:
            net_guard.check_url = real
            srv.shutdown()
            srv.server_close()
        self.assertEqual(len(seen), 2)
        self.assertIn("blocked.test", seen[1])


class PathSandboxTests(unittest.TestCase):
    def test_sibling_directory_is_not_inside(self):
        from core.agent_tools import _within
        root = Path("C:/work/proj")
        self.assertTrue(_within(Path("C:/work/proj"), root))
        self.assertTrue(_within(Path("C:/work/proj/a/b.txt"), root))
        self.assertTrue(_within(Path("c:/WORK/Proj/a.txt"), root))       # Windows case
        self.assertFalse(_within(Path("C:/work/proj2/a.txt"), root))
        self.assertFalse(_within(Path("C:/work/projX"), root))
        self.assertFalse(_within(Path("C:/work"), root))


if __name__ == "__main__":
    unittest.main()
