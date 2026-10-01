"""tests/test_ldap_auth.py - G1a: corporate directory passwords (LDAP/AD).

No network is touched: ldap3.Connection is faked at the seam where the
provider constructs it, so these tests prove OUR logic (DN building, empty
password rejection, fail-closed config, local-first chain, provisioning) --
not the ldap3 wire protocol.

Run: python -m unittest tests.test_ldap_auth -v
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ldap3

from core import auth_db
from core.auth_provider import LdapAuthProvider, get_auth_provider


class FakeConn:
    """Minimal ldap3.Connection stand-in. DIRECTORY maps DN -> password."""
    DIRECTORY = {}
    binds = []

    def __init__(self, server, user=None, password=None, auto_bind=False, **kw):
        self.user = user
        self.password = password
        self.entries = []

    def bind(self):
        FakeConn.binds.append(self.user)
        want = FakeConn.DIRECTORY.get(self.user)
        return bool(self.user) and want is not None and want == self.password

    def unbind(self):
        pass

    def search(self, base, filtr, attributes=None):
        m = re.search(r"\(uid=([^)]+)\)", filtr or "")
        if not m:
            return False
        for dn in FakeConn.DIRECTORY:
            if f"uid={m.group(1)}," in dn:
                self.entries = [SimpleNamespace(entry_dn=dn)]
                return True
        return False


TPL = "uid={username},ou=people,dc=example,dc=com"
ALICE = "uid=alice,ou=people,dc=example,dc=com"


def _provider(**kw):
    cfg = {"host": "ad.example.com", "user_dn_template": TPL}
    cfg.update(kw)
    return LdapAuthProvider(cfg, _server=object())


class _TempAuthDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = auth_db._auth_db
        with mock.patch.object(auth_db, "AUTH_DB_FILE", Path(self.tmp.name) / "auth.db"):
            auth_db._auth_db = auth_db._init_auth_db()
        FakeConn.DIRECTORY = {ALICE: "s3cret!"}
        FakeConn.binds = []
        self._conn = mock.patch.object(ldap3, "Connection", FakeConn)
        self._conn.start()
        self.addCleanup(self._conn.stop)

    def tearDown(self):
        auth_db._auth_db.close()
        auth_db._auth_db = self._saved
        self.tmp.cleanup()


class ConfigTests(unittest.TestCase):
    def test_host_required(self):
        with self.assertRaises(ValueError):
            LdapAuthProvider({}, _server=object())

    def test_plaintext_past_loopback_refused(self):
        with self.assertRaises(ValueError):
            LdapAuthProvider({"host": "ad.example.com", "use_ssl": False,
                              "user_dn_template": TPL}, _server=object())

    def test_plaintext_loopback_allowed(self):
        p = LdapAuthProvider({"host": "127.0.0.1", "use_ssl": False,
                              "user_dn_template": TPL}, _server=object())
        self.assertEqual((p.port, p.use_ssl), (389, False))

    def test_needs_template_or_search(self):
        with self.assertRaises(ValueError):
            LdapAuthProvider({"host": "ad.example.com"}, _server=object())


class VerifyTests(_TempAuthDb):
    def test_success_provisions_ldap_row(self):
        p = _provider()
        rec = p.verify_credentials("alice", "s3cret!")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.username, "alice")
        self.assertFalse(rec.is_super_admin)
        row = auth_db.get_user_by_username("alice")
        self.assertEqual((row["auth_provider"], row["password_hash"]), ("ldap", None))
        self.assertEqual(row["external_id"], ALICE)
        self.assertIn("user", auth_db.get_user_role_names(row["id"]))

    def test_second_login_reuses_row(self):
        p = _provider()
        first = p.verify_credentials("alice", "s3cret!")
        second = p.verify_credentials("alice", "s3cret!")
        self.assertEqual(first.id, second.id)
        self.assertEqual(auth_db.db().execute("SELECT COUNT(*) c FROM users").fetchone()["c"], 1)

    def test_wrong_password_unknown_user_rejected(self):
        p = _provider()
        self.assertIsNone(p.verify_credentials("alice", "wrong"))
        self.assertIsNone(p.verify_credentials("nobody", "whatever"))
        self.assertIsNone(auth_db.get_user_by_username("nobody"))

    def test_empty_password_never_binds(self):
        # some servers SUCCEED an unauthenticated bind: reject before dialling
        p = _provider()
        self.assertIsNone(p.verify_credentials("alice", ""))
        self.assertNotIn(ALICE, FakeConn.binds)

    def test_path_and_wildcard_usernames_rejected(self):
        p = _provider()
        for bad in ("a/b", "a\\b", "a*b", ""):
            self.assertIsNone(p.verify_credentials(bad, "x"), bad)
        self.assertEqual(FakeConn.binds, [])

    def test_inactive_row_denied(self):
        p = _provider()
        rec = p.verify_credentials("alice", "s3cret!")
        auth_db.db().execute("UPDATE users SET is_active = 0 WHERE id = ?", (rec.id,))
        auth_db.db().commit()
        self.assertIsNone(p.verify_credentials("alice", "s3cret!"))

    def test_local_break_glass_wins_without_directory(self):
        from core.auth_provider import hash_password
        auth_db.create_user(username="root", password_hash=hash_password("br3ak-glass9"),
                            is_super_admin=True)
        FakeConn.DIRECTORY = {}  # directory knows nobody: local must still work
        p = _provider()
        rec = p.verify_credentials("root", "br3ak-glass9")
        self.assertIsNotNone(rec)
        self.assertTrue(rec.is_super_admin)
        self.assertIsNone(p.verify_credentials("root", "wrong-password"))

    def test_check_password_binds_without_provisioning(self):
        p = _provider()
        self.assertTrue(p.check_password("alice", "s3cret!"))
        self.assertFalse(p.check_password("alice", "wrong"))
        self.assertIsNone(auth_db.get_user_by_username("alice"))


class SearchModeTests(_TempAuthDb):
    def test_service_account_search_finds_dn(self):
        FakeConn.DIRECTORY["uid=svc,ou=svc,dc=example,dc=com"] = "svcpw"
        p = _provider(user_dn_template="",
                      bind_dn="uid=svc,ou=svc,dc=example,dc=com", bind_password="svcpw",
                      search_base="ou=people,dc=example,dc=com")
        rec = p.verify_credentials("alice", "s3cret!")
        self.assertIsNotNone(rec)
        self.assertEqual(auth_db.get_user_by_username("alice")["external_id"], ALICE)


class FactoryTests(unittest.TestCase):
    def test_default_is_local(self):
        from core.auth_provider import LocalAuthProvider
        with mock.patch.dict("core.small_model.APP_CONFIG", {}):
            self.assertIsInstance(get_auth_provider(), LocalAuthProvider)

    def test_ldap_selected_by_config(self):
        with mock.patch.dict("core.small_model.APP_CONFIG",
                             {"auth": {"provider": "ldap",
                                       "ldap": {"host": "ad.example.com",
                                                "user_dn_template": TPL}}}):
            self.assertIsInstance(get_auth_provider(), LdapAuthProvider)


class LdapMfaTests(_TempAuthDb):
    """G1b: directory accounts enroll/disable MFA with a live directory bind
    (never a stored hash); a leftover LDAP row with the provider off verifies
    nothing -- fail closed, never passwordless."""

    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import deps
        from core.auth import Principal
        from routes import auth as auth_routes
        self.uid = auth_db.create_user(username="alice", password_hash=None, auth_provider="ldap")
        auth_db.db().execute("UPDATE users SET external_id = ? WHERE id = ?", (ALICE, self.uid))
        auth_db.db().commit()
        app = FastAPI()
        app.include_router(auth_routes.router)
        app.dependency_overrides[deps.get_current_user] = lambda: Principal(
            id=self.uid, username="alice", display_name="alice", is_super_admin=False,
            must_change_password=False, role_names=[], permission_keys={"chat.use"},
            mfa_verified=True)
        self._audit = mock.patch.object(auth_routes, "audit_log", lambda *a, **k: None)
        self._audit.start()
        self.addCleanup(self._audit.stop)
        auth_routes._mfa_failures.clear()
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self._cfg = {"provider": "ldap",
                     "ldap": {"host": "ad.example.com", "user_dn_template": TPL}}

    def test_enroll_with_directory_password(self):
        from core.small_model import APP_CONFIG
        with mock.patch.dict(APP_CONFIG, {"auth": self._cfg}):
            r = self.client.post("/auth/mfa/enroll", json={"password": "s3cret!"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertTrue(r.json()["secret"])

    def test_enroll_wrong_directory_password(self):
        from core.small_model import APP_CONFIG
        with mock.patch.dict(APP_CONFIG, {"auth": self._cfg}):
            r = self.client.post("/auth/mfa/enroll", json={"password": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertIsNone(auth_db.get_totp(self.uid)["secret"])

    def test_leftover_ldap_row_with_provider_off_verifies_nothing(self):
        r = self.client.post("/auth/mfa/enroll", json={"password": "s3cret!"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
