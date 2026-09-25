"""tests/test_security_fixes.py - regressions for the 2026-09 security audit.

RBAC escalation, catch-all proxy allow-list, DB explorer read-only console.
Uses a throwaway auth DB (never touches auth.db).
Run: python -m unittest tests.test_security_fixes -v
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth_db, deps
from core.auth import Principal


def _principal(uid, perms, super_admin=False):
    return Principal(id=uid, username=f"u{uid}", display_name=f"u{uid}", is_super_admin=super_admin,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class _TempAuthDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = auth_db._auth_db
        with mock.patch.object(auth_db, "AUTH_DB_FILE", Path(self.tmp.name) / "auth.db"):
            auth_db._auth_db = auth_db._init_auth_db()

    def tearDown(self):
        auth_db._auth_db.close()
        auth_db._auth_db = self._saved
        self.tmp.cleanup()


class RbacEscalationTests(_TempAuthDb):
    def setUp(self):
        super().setUp()
        from routes import admin_rbac
        self.mgr = auth_db.create_user(username="mgr", password_hash=None)
        self.victim = auth_db.create_user(username="victim", password_hash=None)
        self.root = auth_db.create_user(username="root", password_hash=None, is_super_admin=True)
        self.patches = [mock.patch.object(admin_rbac, "audit_log", lambda *a, **k: None),
                        mock.patch.object(deps, "audit_log", lambda *a, **k: None)]
        for p in self.patches:
            p.start()
        app = FastAPI()
        app.include_router(admin_rbac.router)
        self.caller = _principal(self.mgr, {"users.manage", "roles.manage", "chat.use"})
        app.dependency_overrides[deps.get_current_user] = lambda: self.caller
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for p in self.patches:
            p.stop()
        super().tearDown()

    def test_cannot_change_own_roles(self):
        r = self.client.patch(f"/admin/users/{self.mgr}", json={"roles": ["admin"]})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("admin", auth_db.get_user_role_names(self.mgr))

    def test_cannot_assign_role_with_permissions_caller_lacks(self):
        r = self.client.patch(f"/admin/users/{self.victim}", json={"roles": ["admin"]})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/admin/users", json={"username": "x", "password": "[PLACEHOLDER]", "roles": ["admin"]})
        self.assertEqual(r.status_code, 403)

    def test_can_assign_role_within_own_permissions(self):
        r = self.client.patch(f"/admin/users/{self.victim}", json={"roles": ["user"]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_cannot_touch_super_admin(self):
        self.assertEqual(self.client.patch(f"/admin/users/{self.root}", json={"is_active": False}).status_code, 403)
        self.assertEqual(self.client.delete(f"/admin/users/{self.root}").status_code, 403)
        self.assertTrue(auth_db.get_user_by_id(self.root)["is_active"])

    def test_cannot_grant_permission_caller_lacks(self):
        role_id = auth_db.db().execute("SELECT id FROM roles WHERE name = 'user'").fetchone()["id"]
        r = self.client.patch(f"/admin/roles/{role_id}/permissions", json={"grant": ["database.manage"]})
        self.assertEqual(r.status_code, 403)

    def test_super_admin_unrestricted(self):
        self.caller = _principal(self.root, set(), super_admin=True)
        r = self.client.patch(f"/admin/users/{self.victim}", json={"roles": ["admin"]})
        self.assertEqual(r.status_code, 200, r.text)


class ProxyAllowListTests(unittest.TestCase):
    def setUp(self):
        from routes import proxy
        app = FastAPI()
        app.include_router(proxy.router)
        self.perms = {"chat.use"}
        app.dependency_overrides[deps.get_current_user] = lambda: _principal(1, self.perms)
        self.patch = mock.patch.object(deps, "audit_log", lambda *a, **k: None)
        self.patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.patch.stop()

    def test_llama_admin_endpoints_not_proxied(self):
        for path in ("/slots", "/slots/0?action=erase", "/props", "/lora-adapters", "/metrics"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.post("/slots/0?action=erase", json={}).status_code, 404)

    def test_requires_chat_use(self):
        self.perms.clear()
        self.assertEqual(self.client.get("/v1/models").status_code, 403)


class DbExplorerTests(unittest.TestCase):
    def setUp(self):
        from routes import db_explorer
        self.dx = db_explorer
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        c = sqlite3.connect(self.path)
        c.execute("CREATE TABLE users (id INTEGER, username TEXT, password_hash TEXT)")
        c.execute("INSERT INTO users VALUES (1, 'alice', '[PLACEHOLDER_HASH]')")
        c.commit()
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_only_by_default(self):
        with self.dx._connect(self.path, 3.0) as c:
            with self.assertRaises(sqlite3.Error):
                c.execute("DELETE FROM users")
            for sql in ("ATTACH DATABASE 'x.db' AS x", "PRAGMA user_version = 3", "SELECT load_extension('x')"):
                with self.assertRaises(sqlite3.Error, msg=sql):
                    c.execute(sql)
            self.assertTrue(c.execute('PRAGMA table_info("users")').fetchall())

    def test_auth_db_never_writable_and_hides_hashes(self):
        with mock.patch.object(self.dx, "AUTH_DB_FILE", self.path):
            with self.dx._connect(self.path, 3.0, writable=True) as c:
                self.assertEqual(c.execute("SELECT username, password_hash FROM users").fetchone(),
                                 ("alice", None))
                with self.assertRaises(sqlite3.Error):
                    c.execute("UPDATE users SET username = 'x'")

    def test_vacuum_rejected(self):
        self.assertTrue(self.dx._VACUUM_RE.search("vacuum into 'C:/x.db'"))


class HeaderAndCsrfTests(unittest.TestCase):
    def setUp(self):
        from fastapi.responses import HTMLResponse, PlainTextResponse
        from core.auth import CSRF_COOKIE, SESSION_COOKIE
        from core.csrf import CSRFMiddleware, SecurityHeadersMiddleware
        app = FastAPI()
        app.add_middleware(CSRFMiddleware)
        app.add_middleware(SecurityHeadersMiddleware)
        app.get("/page")(lambda: HTMLResponse("<p>ok</p>"))
        app.get("/data")(lambda: {"ok": True})
        app.get("/raw")(lambda: PlainTextResponse("ok", headers={"Content-Security-Policy": "sandbox"}))
        app.post("/act")(lambda: {"ok": True})
        self.client = TestClient(app)
        self.client.cookies.set(SESSION_COOKIE, "s")
        self.client.cookies.set(CSRF_COOKIE, "[PLACEHOLDER_CSRF]")

    def tearDown(self):
        self.client.close()

    def test_baseline_headers(self):
        h = self.client.get("/page").headers
        self.assertEqual(h["x-frame-options"], "SAMEORIGIN")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        csp = h["content-security-policy"]
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertIn("script-src 'self' https://cdn.jsdelivr.net https://cdn.sheetjs.com;", csp)
        self.assertNotIn("unsafe-inline';", csp.split("script-src", 1)[1].split(";", 1)[0] + ";")
        # non-documents (JSON, PDFs) get no CSP: object-src 'none' would break the PDF viewer
        self.assertNotIn("content-security-policy", self.client.get("/data").headers)

    def test_handler_csp_not_overridden(self):
        self.assertEqual(self.client.get("/raw").headers["content-security-policy"], "sandbox")

    def test_csrf(self):
        self.assertEqual(self.client.post("/act").status_code, 403)
        self.assertEqual(self.client.post("/act", headers={"X-CSRF-Token": "wrong"}).status_code, 403)
        self.assertEqual(self.client.post("/act", headers={"X-CSRF-Token": "[PLACEHOLDER_CSRF]"}).status_code, 200)


class ToolTrustTests(unittest.TestCase):
    def test_risky_args_never_auto_approved(self):
        from core.shell_tools import command_allowed
        pats = ["git status*", "git diff*", "git log*", "dir *", "type *"]
        for cmd in ("git diff --output=x.bat", "git log -o x", "git -c core.fsmonitor=x status",
                    r"type C:\auth.db", r"type ..\..\x", r"type \\host\share\x", "type /etc/passwd",
                    "echo !PATH!"):
            self.assertFalse(command_allowed(cmd, pats), cmd)
        for cmd in ("git status", "git diff src/a.py", "dir /s", r"type src\a.py", "git log --oneline"):
            self.assertTrue(command_allowed(cmd, pats), cmd)

    def test_saved_pattern_must_derive_from_command(self):
        from routes.agent import _pattern_error
        rec = {"cmd": "npm test -- --watch=false"}
        self.assertIsNone(_pattern_error("npm *", rec))
        self.assertIsNone(_pattern_error("npm test -- --watch=false", rec))
        for bad in ("*", "rm *", "npm test*x"):
            self.assertIsNotNone(_pattern_error(bad, rec), bad)
        self.assertIsNotNone(_pattern_error("python *", {"cmd": "run_python:\nprint(1)", "kind": "python"}))

    def test_sandbox_blocks_git_short_name_in_every_lane(self):
        import asyncio
        from core.agent_loop import fast_sandbox_check, run_tool
        for p in ("GIT~1/hooks/pre-commit", "sub/.git/config", ".git/hooks/x"):
            self.assertFalse(fast_sandbox_check("write_file", {"path": p})[0], p)
        out = asyncio.run(run_tool("write_file", {"path": "GIT~1/hooks/pre-commit", "content": "x"}))
        self.assertTrue(out.startswith("error: sandbox violation"), out)

    def test_run_python_denied_to_subagents(self):
        from core.subagent import DENIED_TOOLS
        self.assertIn("run_python", DENIED_TOOLS)


class CloudProviderTests(unittest.TestCase):
    def setUp(self):
        from core import cloud, credentials
        self.cloud = cloud
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = {}
        self.patches = [
            mock.patch.object(cloud, "PROVIDERS_DIR", Path(self.tmp.name)),
            mock.patch.object(credentials, "set_token", lambda k, v: self.vault.__setitem__(k, v)),
            mock.patch.object(credentials, "get_token", lambda k: self.vault.get(k)),
            mock.patch.object(credentials, "delete_token", lambda k: self.vault.pop(k, None)),
            mock.patch.object(cloud, "check_provider_url", lambda u: u),   # no DNS in tests
        ]
        for p in self.patches:
            p.start()
        cloud.reload()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.cloud.reload()
        self.tmp.cleanup()

    def test_api_key_goes_to_keychain_not_file(self):
        self.cloud.save_provider(42, "p", {"base_url": "https://api.example/v1", "api_key": "[PLACEHOLDER_KEY]",
                                           "models": [{"id": "m"}]})
        on_disk = (Path(self.tmp.name) / "user_42.json").read_text(encoding="utf-8")
        self.assertNotIn("[PLACEHOLDER_KEY]", on_disk)
        self.assertIn('"apiKeyRef": "keyring"', on_disk)
        self.assertEqual(self.cloud.providers(42)["p"]["options"]["apiKey"], "[PLACEHOLDER_KEY]")
        # re-saving without a key keeps the stored one
        self.cloud.save_provider(42, "p", {"name": "renamed"})
        self.assertEqual(self.cloud.providers(42)["p"]["options"]["apiKey"], "[PLACEHOLDER_KEY]")
        self.cloud.delete_provider(42, "p")
        self.assertEqual(self.vault, {})

    def test_plaintext_key_migrated_on_read(self):
        import json
        f = Path(self.tmp.name) / "user_7.json"
        f.write_text(json.dumps({"provider": {"q": {"options": {"baseURL": "https://x", "apiKey": "[PLACEHOLDER_OLD]"}}}}),
                     encoding="utf-8")
        self.assertEqual(self.cloud.providers(7)["q"]["options"]["apiKey"], "[PLACEHOLDER_OLD]")
        self.assertNotIn("[PLACEHOLDER_OLD]", f.read_text(encoding="utf-8"))

    def test_rejects_internal_base_url_and_host_header(self):
        from core import cloud
        self.patches[-1].stop()
        try:
            for url in ("http://127.0.0.1:8090/v1", "https://169.254.169.254/", "https://localhost/v1"):
                with self.assertRaises(ValueError, msg=url):
                    cloud.check_provider_url(url)
        finally:
            self.patches[-1].start()
        with self.assertRaises(ValueError):
            cloud.save_provider(1, "h", {"base_url": "https://api.example", "extra_headers": {"Host": "x"}})


class PanTests(unittest.TestCase):
    def test_variants_detected(self):
        from core import pan
        tv = "4111111111111111"   # standard test number, not a real card
        bengali = "".join(chr(0x09E6 + int(c)) for c in tv)
        for t in (tv, "4111.1111.1111.1111", "4111 1111 1111 1111",
                  "4111​1111​1111​1111", bengali):
            self.assertTrue(pan.contains_pan(t), ascii(t))
            self.assertEqual(pan.mask_pans(t)[0], "[card ****1111]")
        self.assertFalse(pan.contains_pan("order 1234567890123"))

    def test_tool_call_arguments_masked(self):
        from core import pan
        m = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"arguments": '{"c":"4111111111111111"}'}}]}]
        self.assertEqual(pan.mask_messages(m), 1)
        self.assertNotIn("4111111111111111", m[0]["tool_calls"][0]["function"]["arguments"])

    def test_password_policy(self):
        from core.auth import password_policy_error
        for bad in ("short1", "alllettersnodigits", "123456789012", "alice12345678"):
            self.assertIsNotNone(password_policy_error(bad, "alice12345678"), bad)
        self.assertIsNone(password_policy_error("correct horse 42", "alice"))


class SseHelperNotShadowedTests(unittest.TestCase):
    """Routes call core.sse.sse(event, data); a local def named `sse` would shadow it
    and every tool event would raise TypeError."""

    def test_no_local_sse_definition(self):
        import ast
        root = Path(__file__).resolve().parent.parent
        for rel in ("routes/chat.py", "routes/agent.py"):
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
            defs = [n.lineno for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "sse"]
            self.assertEqual(defs, [], f"{rel} defines sse() at lines {defs}")


if __name__ == "__main__":
    unittest.main()
