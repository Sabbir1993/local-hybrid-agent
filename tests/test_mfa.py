"""tests/test_mfa.py - MFA-1: stdlib TOTP core, schema migration, db helpers.

Run: python -m unittest tests.test_mfa -v
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import auth_db
from core import totp


class TotpVectors(unittest.TestCase):
    """RFC 6238 Appendix B, SHA-1, key '12345678901234567890', truncated to 6 digits."""

    SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of b"12345678901234567890"

    def test_rfc_vectors(self):
        for t, want in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
                        (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130")):
            with self.subTest(t=t):
                self.assertEqual(totp.code_at(self.SECRET, t), want)

    def test_verify_window_and_replay(self):
        self.assertIsNotNone(totp.verify(self.SECRET, "287082", now=59))
        self.assertIsNone(totp.verify(self.SECRET, "287082", now=59 + 5 * 30))  # outside +-1 step
        c = totp.verify(self.SECRET, "287082", now=59)
        self.assertIsNone(totp.verify(self.SECRET, "287082", now=59, last_counter=c))  # replay
        self.assertIsNone(totp.verify(self.SECRET, "000000", now=59))
        self.assertIsNone(totp.verify("", "287082", now=59))

    def test_secret_and_uri_shape(self):
        s = totp.generate_secret()
        self.assertRegex(s, r"^[A-Z2-7]{32}$")
        uri = totp.otpauth_uri(s, "alice")
        self.assertTrue(uri.startswith("otpauth://totp/A770:alice?secret=" + s))

    def test_backup_code_shape(self):
        for c in totp.generate_backup_codes(8):
            self.assertRegex(c, r"^[0-9a-f]{4}-[0-9a-f]{4}$")


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


class MigrationTests(_TempAuthDb):
    def test_old_db_without_totp_columns_migrates(self):
        db = auth_db.db()
        for col in ("totp_secret", "totp_enabled", "totp_last_counter"):
            db.execute(f"ALTER TABLE users DROP COLUMN {col}")
        db.execute("DROP TABLE user_totp_backup")
        db.commit()
        from core.auth_db.schema import init_tables
        init_tables(db)
        cols = {r[1] for r in db.execute("PRAGMA table_info(users)")}
        self.assertTrue({"totp_secret", "totp_enabled", "totp_last_counter"} <= cols)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("user_totp_backup", tables)


class HelperTests(_TempAuthDb):
    def setUp(self):
        super().setUp()
        self.uid = auth_db.create_user(username="u", password_hash=None)

    def test_enroll_enable_disable_cycle(self):
        self.assertFalse(auth_db.totp_enabled(self.uid))
        auth_db.set_totp_secret(self.uid, "SECRET")
        self.assertFalse(auth_db.totp_enabled(self.uid))  # staged, not on
        auth_db.enable_totp(self.uid)
        self.assertTrue(auth_db.totp_enabled(self.uid))
        auth_db.record_totp_counter(self.uid, 42)
        self.assertEqual(auth_db.get_totp(self.uid)["last_counter"], 42)
        auth_db.disable_totp(self.uid)
        st = auth_db.get_totp(self.uid)
        self.assertFalse(st["enabled"])
        self.assertIsNone(st["secret"])

    def test_backup_codes_single_use(self):
        codes = totp.generate_backup_codes(8)
        auth_db.store_backup_codes(self.uid, codes)
        self.assertEqual(auth_db.remaining_backup_codes(self.uid), 8)
        self.assertTrue(auth_db.consume_backup_code(self.uid, codes[0]))
        self.assertFalse(auth_db.consume_backup_code(self.uid, codes[0]))  # used up
        self.assertFalse(auth_db.consume_backup_code(self.uid, "0000-0000"))  # unknown
        self.assertEqual(auth_db.remaining_backup_codes(self.uid), 7)
        auth_db.disable_totp(self.uid)  # disable wipes codes too
        self.assertEqual(auth_db.remaining_backup_codes(self.uid), 0)

    def test_reenroll_wipes_old_codes(self):
        codes = totp.generate_backup_codes(8)
        auth_db.store_backup_codes(self.uid, codes)
        auth_db.set_totp_secret(self.uid, "NEWSECRET")
        self.assertEqual(auth_db.remaining_backup_codes(self.uid), 0)

    def test_delete_user_cleans_backup_codes(self):
        auth_db.store_backup_codes(self.uid, totp.generate_backup_codes(8))
        auth_db.delete_user(self.uid)
        self.assertEqual(auth_db.remaining_backup_codes(self.uid), 0)


class EnrollmentEndpoints(_TempAuthDb):
    """MFA-2: enroll (password re-entry, secret+codes shown once) -> confirm
    (TOTP only) -> status -> disable (password re-entry, full wipe)."""

    def setUp(self):
        super().setUp()
        from core.auth_provider import hash_password
        from core.auth import Principal
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import deps
        from routes import auth as auth_routes
        self.uid = auth_db.create_user(username="u", password_hash=hash_password("s3cr3t99"))
        self.me = Principal(id=self.uid, username="u", display_name="u", is_super_admin=False,
                            must_change_password=False, role_names=[], permission_keys={"chat.use"},
                            mfa_verified=True)
        app = FastAPI()
        app.include_router(auth_routes.router)
        app.dependency_overrides[deps.get_current_user] = lambda: self.me
        self._audit = mock.patch.object(auth_routes, "audit_log", lambda *a, **k: None)
        self._audit.start()
        self.addCleanup(self._audit.stop)
        auth_routes._mfa_failures.clear()  # throttle state is module-global; ids restart at 1 per test
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_full_lifecycle(self):
        r = self.client.get("/auth/mfa/status")
        self.assertEqual((r.status_code, r.json()["enabled"]), (200, False))
        # wrong password binds nothing
        r = self.client.post("/auth/mfa/enroll", json={"password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(auth_db.get_totp(self.uid)["secret"])
        # enroll stages secret + codes, shown once
        r = self.client.post("/auth/mfa/enroll", json={"password": "s3cr3t99"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertRegex(body["secret"], r"^[A-Z2-7]{32}$")
        self.assertTrue(body["otpauth_uri"].startswith("otpauth://totp/"))
        self.assertEqual(len(body["backup_codes"]), 8)
        self.assertFalse(auth_db.totp_enabled(self.uid))  # staged, not on
        # backup codes do NOT confirm (they prove nothing about the app)
        r = self.client.post("/auth/mfa/confirm", json={"code": body["backup_codes"][0]})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(auth_db.totp_enabled(self.uid))
        # a real TOTP code confirms
        r = self.client.post("/auth/mfa/confirm", json={"code": totp.code_at(body["secret"], time.time())})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertTrue(auth_db.totp_enabled(self.uid))
        # re-enroll while enabled is refused
        r = self.client.post("/auth/mfa/enroll", json={"password": "s3cr3t99"})
        self.assertEqual(r.status_code, 400)
        # disable needs the password too, and wipes everything
        r = self.client.post("/auth/mfa/disable", json={"password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertTrue(auth_db.totp_enabled(self.uid))
        r = self.client.post("/auth/mfa/disable", json={"password": "s3cr3t99"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        st = auth_db.get_totp(self.uid)
        self.assertFalse(st["enabled"])
        self.assertIsNone(st["secret"])
        self.assertEqual(auth_db.remaining_backup_codes(self.uid), 0)

    def test_confirm_throttle(self):
        self.client.post("/auth/mfa/enroll", json={"password": "s3cr3t99"})
        for _ in range(10):
            r = self.client.post("/auth/mfa/confirm", json={"code": "000000"})
            self.assertEqual(r.status_code, 401)
        r = self.client.post("/auth/mfa/confirm", json={"code": "000000"})
        self.assertEqual(r.status_code, 429)

    def test_nonlocal_account_cannot_enroll(self):
        other = auth_db.create_user(username="sso", password_hash=None, auth_provider="sso")
        from core.auth import Principal
        self.me = Principal(id=other, username="sso", display_name="sso", is_super_admin=False,
                            must_change_password=False, role_names=[], permission_keys={"chat.use"})
        r = self.client.post("/auth/mfa/enroll", json={"password": "x"})
        self.assertEqual(r.status_code, 400)

    def test_disable_on_stale_session_needs_step_up_first(self):
        self.client.post("/auth/mfa/enroll", json={"password": "s3cr3t99"})
        from core.auth import Principal
        stale = Principal(id=self.uid, username="u", display_name="u", is_super_admin=False,
                          must_change_password=False, role_names=[], permission_keys={"chat.use"},
                          mfa_verified=False)
        secret = auth_db.get_totp(self.uid)["secret"]
        self.client.post("/auth/mfa/confirm", json={"code": totp.code_at(secret, time.time())})
        self.assertTrue(auth_db.totp_enabled(self.uid))
        self.me = stale  # pre-enrollment session: verified flag never set
        r = self.client.post("/auth/mfa/disable", json={"password": "s3cr3t99"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "mfa_step_up_required")
        self.assertTrue(auth_db.totp_enabled(self.uid))  # nothing wiped


class SecondStepTests(_TempAuthDb):
    """MFA-3: password -> ticket (no session) -> code -> verified session;
    enforced bootstrap via enrollment tickets; step-up elevates a stale session."""

    def setUp(self):
        super().setUp()
        from core.auth_provider import hash_password
        from core.auth import Principal
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import deps
        from routes import auth as auth_routes
        self.auth_routes = auth_routes
        self.uid = auth_db.create_user(username="u", password_hash=hash_password("s3cr3t99"))
        app = FastAPI()
        app.include_router(auth_routes.router)
        self.principal = Principal(id=self.uid, username="u", display_name="u", is_super_admin=False,
                                   must_change_password=False, role_names=[], permission_keys={"chat.use"})
        app.dependency_overrides[deps.get_current_user] = lambda: self.principal
        self._audit = mock.patch.object(auth_routes, "audit_log", lambda *a, **k: None)
        self._audit.start()
        self.addCleanup(self._audit.stop)
        auth_routes._mfa_failures.clear()
        auth_routes._mfa_tickets.clear()
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _enable_mfa(self):
        secret = totp.generate_secret()
        auth_db.set_totp_secret(self.uid, secret)
        auth_db.enable_totp(self.uid)
        return secret

    def test_login_with_mfa_returns_ticket_not_session(self):
        secret = self._enable_mfa()
        r = self.client.post("/auth/login", json={"username": "u", "password": "s3cr3t99"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertTrue(body.get("mfa_required"))
        self.assertNotIn("user", body)
        self.assertNotIn("a770_session", self.client.cookies)
        # wrong code fails, right code (TOTP or backup) opens a verified session
        r = self.client.post("/auth/mfa/verify", json={"ticket": body["ticket"], "code": "000000"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/auth/mfa/verify",
                             json={"ticket": body["ticket"], "code": totp.code_at(secret, time.time())})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("a770_session", self.client.cookies)
        # ticket is single-use
        r = self.client.post("/auth/mfa/verify",
                             json={"ticket": body["ticket"], "code": totp.code_at(secret, time.time())})
        self.assertEqual(r.status_code, 401)

    def test_verify_accepts_backup_code_once(self):
        self._enable_mfa()
        codes = totp.generate_backup_codes(8)
        auth_db.store_backup_codes(self.uid, codes)
        ticket = self.client.post("/auth/login", json={"username": "u", "password": "s3cr3t99"}).json()["ticket"]
        r = self.client.post("/auth/mfa/verify", json={"ticket": ticket, "code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text[:200])
        ticket2 = self.client.post("/auth/login", json={"username": "u", "password": "s3cr3t99"}).json()["ticket"]
        r = self.client.post("/auth/mfa/verify", json={"ticket": ticket2, "code": codes[0]})
        self.assertEqual(r.status_code, 401)  # consumed

    def test_ticket_guessing_burns_ticket(self):
        self._enable_mfa()
        ticket = self.client.post("/auth/login", json={"username": "u", "password": "s3cr3t99"}).json()["ticket"]
        for _ in range(5):
            r = self.client.post("/auth/mfa/verify", json={"ticket": ticket, "code": "000000"})
            self.assertEqual(r.status_code, 401)
        # 5th failure burned it: even the right code now fails
        r = self.client.post("/auth/mfa/verify",
                             json={"ticket": ticket, "code": totp.code_at(
                                 auth_db.get_totp(self.uid)["secret"], time.time())})
        self.assertEqual(r.status_code, 401)

    def test_enforced_bootstrap(self):
        from core.small_model import APP_CONFIG
        with mock.patch.dict(APP_CONFIG.setdefault("security", {}), {"mfa_required": True}):
            r = self.client.post("/auth/login", json={"username": "u", "password": "s3cr3t99"})
            body = r.json()
            self.assertTrue(body.get("enroll_required"))
            # enroll + confirm over the ticket -> enabled + verified session
            r = self.client.post("/auth/mfa/ticket/enroll", json={"ticket": body["ticket"]})
            self.assertEqual(r.status_code, 200, r.text[:200])
            secret = r.json()["secret"]
            r = self.client.post("/auth/mfa/ticket/confirm",
                                 json={"ticket": body["ticket"],
                                       "code": totp.code_at(secret, time.time())})
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertTrue(auth_db.totp_enabled(self.uid))
            self.assertIn("a770_session", self.client.cookies)

    def test_step_up_elevates_session(self):
        from core import auth as auth_core
        secret = self._enable_mfa()
        raw = auth_core.create_session(
            self.principal, ip="testclient", user_agent="t")  # unverified session
        # prove the code on the live session cookie
        self.client.cookies.set("a770_session", raw)
        row = auth_db.db().execute("SELECT mfa_verified FROM auth_sessions WHERE id = ?",
                                   (auth_core._hash_token(raw),)).fetchone()
        self.assertEqual(row["mfa_verified"], 0)
        r = self.client.post("/auth/mfa/step-up", json={"code": totp.code_at(secret, time.time())})
        self.assertEqual(r.status_code, 200, r.text[:200])
        row = auth_db.db().execute("SELECT mfa_verified FROM auth_sessions WHERE id = ?",
                                   (auth_core._hash_token(raw),)).fetchone()
        self.assertEqual(row["mfa_verified"], 1)

    def test_no_mfa_no_ticket_old_behavior(self):
        r = self.client.post("/auth/login", json={"username": "u", "password": "s3cr3t99"})
        body = r.json()
        self.assertIn("user", body)
        self.assertNotIn("mfa_required", body)
        self.assertIn("a770_session", self.client.cookies)


class StepUpWiring(_TempAuthDb):
    """MFA-4: the fenced endpoints stop unverified sessions of MFA-enrolled
    users (403 mfa_step_up_required); verified sessions and non-enrolled users
    pass through unchanged."""

    def setUp(self):
        super().setUp()
        from core.auth_provider import hash_password
        from core.auth import Principal
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import deps
        from routes import auth as auth_routes
        from routes import admin_rbac, api_tokens
        from routes import db_explorer
        from core.companion_bridge import routes as pair_routes
        self.uid = auth_db.create_user(username="u", password_hash=hash_password("s3cr3t99"))
        auth_db.assign_role(self.uid, "admin")
        self.holder = {"perms": {"chat.use", "users.manage", "roles.manage", "database.manage"},
                       "verified": False, "mfa": False}
        me = self

        def current():
            return Principal(id=me.uid, username="u", display_name="u", is_super_admin=False,
                             must_change_password=False, role_names=["admin"],
                             permission_keys=set(me.holder["perms"]),
                             mfa_verified=me.holder["verified"])
        app = FastAPI()
        app.include_router(auth_routes.router)
        app.include_router(admin_rbac.router)
        app.include_router(api_tokens.router)
        app.include_router(db_explorer.router)
        app.include_router(pair_routes.router)
        app.dependency_overrides[deps.get_current_user] = current
        patches = [mock.patch.object(m, "audit_log", lambda *a, **k: None)
                   for m in (auth_routes, admin_rbac.audit_endpoints, admin_rbac.role_endpoints,
                             admin_rbac.user_endpoints, api_tokens, pair_routes)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # db_explorer imports audit_log into its endpoint modules
        from routes.db_explorer.endpoints import browse, query
        for m in (browse, query):
            p = mock.patch.object(m, "audit_log", lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        auth_routes._mfa_failures.clear()
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _login_verified_session(self):
        """Real session cookie through the real login+mfa stack."""
        from core import auth as auth_core
        secret = totp.generate_secret()
        auth_db.set_totp_secret(self.uid, secret)
        auth_db.enable_totp(self.uid)
        ticket = self.client.post("/auth/login",
                                  json={"username": "u", "password": "s3cr3t99"}).json()["ticket"]
        r = self.client.post("/auth/mfa/verify",
                             json={"ticket": ticket, "code": totp.code_at(secret, time.time())})
        self.assertEqual(r.status_code, 200, r.text[:200])
        return secret

    def test_fenced_endpoints_stop_unverified_mfa_sessions(self):
        self._login_verified_session()
        # holder flag drives the override: enrolled but session NOT verified
        # (simulates a stale pre-enrollment session)
        self.holder.update(mfa=True, verified=False)
        for method, path, kwargs in (
                ("GET", "/admin/users", {}),
                ("GET", "/admin/roles", {}),
                ("GET", "/db/list", {}),
                ("POST", "/admin/api-tokens",
                 {"json": {"name": "t", "user_id": self.uid, "permissions": ["chat.use"]}}),
                ("POST", "/companion/pair", {"json": {"device_id": "dev_win_00aa11bb22cc33dd"}}),
                ("PUT", "/auth/mfa/policy", {"json": {"required": True}}),
        ):
            with self.subTest(method=method, path=path):
                r = self.client.request(method, path, **kwargs)
                self.assertEqual(r.status_code, 403, r.text[:200])
                self.assertEqual(r.json()["detail"], "mfa_step_up_required")

    def test_verified_session_passes_and_toggle_sticks(self):
        self._login_verified_session()
        self.holder.update(mfa=True, verified=True)
        self.assertEqual(self.client.get("/admin/users").status_code, 200)
        self.assertEqual(self.client.get("/db/list").status_code, 200)
        r = self.client.put("/auth/mfa/policy", json={"required": True})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertTrue(r.json()["required"])
        self.assertEqual(self.client.get("/auth/mfa/policy").json()["required"], True)
        r = self.client.put("/auth/mfa/policy", json={"required": False})
        self.assertEqual(r.status_code, 200)

    def test_non_enrolled_users_pass_through(self):
        self.holder.update(mfa=False, verified=False)
        self.assertEqual(self.client.get("/admin/users").status_code, 200)
        self.assertEqual(self.client.post("/companion/pair",
                                          json={"device_id": "dev_win_00aa11bb22cc33dd"}).status_code, 200)

    def test_admin_reset(self):
        victim = auth_db.create_user(username="v", password_hash=None)
        secret = totp.generate_secret()
        auth_db.set_totp_secret(victim, secret)
        auth_db.enable_totp(victim)
        self.holder.update(mfa=False, verified=False)  # admin without MFA passes through
        r = self.client.post(f"/admin/users/{victim}/mfa/reset")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertFalse(auth_db.totp_enabled(victim))
        # self-reset is refused (use /mfa/disable), unknown user 404s
        self.assertEqual(self.client.post(f"/admin/users/{self.uid}/mfa/reset").status_code, 400)
        self.assertEqual(self.client.post("/admin/users/424242/mfa/reset").status_code, 404)
        # unverified MFA admin is stopped even for reset
        self._login_verified_session()
        self.holder.update(mfa=True, verified=False)
        victim2 = auth_db.create_user(username="v2", password_hash=None)
        self.assertEqual(self.client.post(f"/admin/users/{victim2}/mfa/reset").status_code, 403)


if __name__ == "__main__":
    unittest.main()
