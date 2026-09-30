"""The connector directory: catalog lint, per-user installs with native OAuth, admin-registered OAuth clients,
and the allow-list for sensitive connectors.

Run: python -m unittest tests.test_connector_catalog -v
"""

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import deps, mcp_catalog
from core.auth import Principal
from core.small_model import APP_CONFIG
from routes import customize as cz
from routes import mcp_manager as mm
from routes.customize import client_endpoints
from routes.mcp_manager import validation as mv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _patching import patch_in_package  # noqa: E402

CLIENT_ID = "PLACEHOLDER-client-id.apps.example"
CLIENT_SECRET = "[PLACEHOLDER_CLIENT_SECRET]"


def _principal(perms, uid):
    return Principal(id=uid, username=f"u{uid}", display_name="t", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class CatalogLintTests(unittest.TestCase):
    def test_every_entry_is_well_formed(self):
        ids = [p["id"] for p in mcp_catalog.PRESETS]
        self.assertEqual(len(ids), len(set(ids)), "duplicate preset id")
        self.assertGreaterEqual(len(ids), 25)
        for p in mcp_catalog.PRESETS:
            with self.subTest(p["id"]):
                self.assertRegex(p["id"], r"^[a-z0-9_-]{1,32}$")
                for k in ("name", "description", "category", "author", "homepage", "transport", "egress"):
                    self.assertIn(k, p)
                self.assertTrue(p["homepage"].startswith("https://"))
                self.assertIn(p["transport"], ("stdio", "http"))
                if p["transport"] == "stdio":
                    self.assertIn(p["command"], mv.DEFAULT_ALLOWED_COMMANDS)
                else:
                    self.assertTrue(p["url"].startswith("https://"))
                    sample = re.sub(r"\{[a-z_]+\}", "x.example.com", p["url"])
                    host = urlparse(sample).hostname
                    self.assertTrue(host and "." in host)
                    self.assertFalse(mv._is_internal_host(host))
                    self.assertTrue(p["egress"], "a remote connector always leaves this host")
                    self.assertIn(p["client"], ("dcr", "auto", "preregistered", "none"))
                    self.assertEqual(bool(p.get("auth")), p["client"] != "none")
                    if p.get("auth"):
                        req = mm.McpServerReq(name=p["id"], transport="http", url=sample,
                                              auth=dict(p["auth"]))
                        self.assertIsNone(mv._validate_auth(req, restricted=True))
                if p.get("sensitivity"):
                    self.assertIn(p["sensitivity"], ("pii", "payments", "data"))
                    self.assertEqual(p["scope"], "user", "sensitive connectors are always per user")
                self.assertIn(p.get("scope", "global"), ("user", "global"))
                for s in p.get("secret_env", []):
                    self.assertRegex(s["key"], mv._ENV_KEY_RX)
                for f in p.get("fields", []):
                    re.compile(f["pattern"])
                    self.assertIn("{" + f["key"] + "}", p["url"])
                    self.assertRegex(f["placeholder"], f["pattern"])

    def test_named_vendors_are_present(self):
        have = {p["id"] for p in mcp_catalog.PRESETS}
        for want in ("google-drive", "gmail", "google-calendar", "canva", "notion", "figma", "slack", "atlassian",
                     "hubspot", "higgsfield", "supabase", "vercel", "sentry", "gamma", "windsor", "salesforce",
                     "shopify-dev", "shopify-storefront", "stripe", "cloudflare", "linear", "microsoft-learn"):
            self.assertIn(want, have)

    def test_url_fields_reject_anything_but_the_pattern(self):
        pre = mcp_catalog.get_preset("shopify-storefront")
        self.assertEqual(mcp_catalog.resolve_url(pre, {"shop": "my-store.myshopify.com"})[0],
                         "https://my-store.myshopify.com/api/mcp")
        for bad in ("", "evil.com/../x", "a b.com", "host:8080", "http://x.com", "localhost"):
            self.assertIsNone(mcp_catalog.resolve_url(pre, {"shop": bad})[0], bad)

    def test_a_shared_client_is_only_used_for_the_real_connector(self):
        with mock.patch.object(mcp_catalog, "shared_client_id", return_value=CLIENT_ID), \
             mock.patch("core.credentials.get_token", return_value=CLIENT_SECRET):
            ok = mcp_catalog.shared_client("slack", {"url": "https://mcp.slack.com/mcp"})
            self.assertEqual(ok, {"client_id": CLIENT_ID, "secret": CLIENT_SECRET})
            # a personal server that only reuses the name must never receive the admin's client secret
            self.assertIsNone(mcp_catalog.shared_client("slack", {"url": "https://attacker.example/mcp"}))
            self.assertIsNone(mcp_catalog.shared_client("not-a-preset", {"url": "https://mcp.slack.com/mcp"}))


class ConnectorRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_file = Path(self.tmp.name) / "app.json"
        self.cfg_file.write_text(json.dumps({"capabilities": {"mcp": True, "mcp_servers": {}}}), encoding="utf-8")
        self.saved_caps = copy.deepcopy(APP_CONFIG.get("capabilities", {}))
        APP_CONFIG.setdefault("capabilities", {})["mcp_servers"] = {}
        for k in ("connectors_enabled", "mcp_catalog_overrides", "mcp_allowed_commands"):
            APP_CONFIG["capabilities"].pop(k, None)
        APP_CONFIG["capabilities"]["mcp_user_allowed_packages"] = []
        self.saved_top = APP_CONFIG.pop("mcpServers", None)
        self.keychain, self.connected, self.user_db = {}, [], {}

        async def fake_connect(name, cfg, owner=None):
            self.connected.append((name, cfg, owner))
            return {"name": name, "status": "ready", "tools": []}

        self.patches = [
            *patch_in_package(mm, "CONFIG_FILE", self.cfg_file),
            mock.patch.object(client_endpoints, "CONFIG_FILE", self.cfg_file),
            *patch_in_package(mm, "audit_log", lambda *a, **k: None),
            *patch_in_package(cz, "audit_log", lambda *a, **k: None),
            mock.patch.object(client_endpoints, "audit_log", lambda *a, **k: None),
            *patch_in_package(mm, "check_url", lambda u: u),
            mock.patch.object(deps, "audit_log", lambda *a, **k: None),
            mock.patch("core.credentials.set_token", lambda k, v: self.keychain.__setitem__(k, v)),
            mock.patch("core.credentials.get_token", lambda k: self.keychain.get(k)),
            mock.patch("core.credentials.delete_token", lambda k: self.keychain.pop(k, None)),
            mock.patch.object(mm.mcp_core, "connect_one", fake_connect),
            mock.patch.object(mm.mcp_core, "disconnect_one", lambda n, owner=None: None),
            mock.patch.object(mm.auth_db, "upsert_user_mcp_server",
                              lambda uid, n, c: self.user_db.__setitem__((uid, n), c)),
            mock.patch.object(mm.auth_db, "delete_user_mcp_server", lambda uid, n: self.user_db.pop((uid, n), None)),
            mock.patch.object(mm.mcp_core, "user_servers",
                              lambda uid: {n: c for (u, n), c in self.user_db.items() if u == uid}),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()
        app.include_router(cz.router)
        self.who = ("user", 7)
        perms = {"user": {"chat.use"}, "admin": {"chat.use", "capabilities.install", "settings.orchestration.configure"}}
        app.dependency_overrides[deps.get_current_user] = lambda: _principal(perms[self.who[0]], self.who[1])
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for p in self.patches:
            p.stop()
        APP_CONFIG["capabilities"] = self.saved_caps
        if self.saved_top is not None:
            APP_CONFIG["mcpServers"] = self.saved_top
        self.tmp.cleanup()

    def as_admin(self, uid=1):
        self.who = ("admin", uid)

    def as_user(self, uid=7):
        self.who = ("user", uid)

    def _item(self, item_id):
        return next(i for i in self.client.get("/customize/connectors").json()["items"] if i["id"] == item_id)

    # ---- per-user install

    def test_a_user_installs_a_connector_just_for_themselves(self):
        r = self.client.post("/customize/connectors/linear/install", json={"scope": "user"})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = self.user_db[(7, "linear")]
        self.assertEqual(cfg["url"], "https://mcp.linear.app/mcp")
        self.assertEqual(cfg["auth"]["type"], "oauth")
        self.assertEqual(self.connected[-1][2], 7)                         # connected under that user
        self.assertEqual(json.loads(self.cfg_file.read_text())["capabilities"]["mcp_servers"], {})   # not global

    def test_listing_shows_only_the_callers_own_install(self):
        self.client.post("/customize/connectors/linear/install", json={"scope": "user"})
        mine = self._item("linear")
        self.assertTrue(mine["installed"])
        self.assertEqual(mine["installed_scope"], "user")
        self.as_user(8)
        other = self._item("linear")
        self.assertFalse(other["installed"])
        self.assertIsNone(other["installed_scope"])

    def test_installing_for_everyone_needs_the_install_permission(self):
        r = self.client.post("/customize/connectors/linear/install", json={"scope": "global"})
        self.assertEqual(r.status_code, 403)
        self.as_admin()
        r = self.client.post("/customize/connectors/linear/install", json={"scope": "global"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("linear", json.loads(self.cfg_file.read_text())["capabilities"]["mcp_servers"])

    def test_a_global_name_blocks_a_personal_copy_and_vice_versa(self):
        self.as_admin()
        self.client.post("/customize/connectors/canva/install", json={"scope": "global"})
        self.as_user()
        self.assertEqual(self.client.post("/customize/connectors/canva/install", json={"scope": "user"}).status_code, 409)
        self.assertEqual(self._item("canva")["installed_scope"], "global")

    def test_the_default_scope_follows_the_preset(self):
        r = self.client.post("/customize/connectors/notion/install", json={})   # personal-data: per user
        self.assertEqual(r.status_code, 403)                                   # ...but not enabled yet (sensitive)
        r = self.client.post("/customize/connectors/higgsfield/install", json={})
        self.assertEqual(r.status_code, 200)
        self.assertIn((7, "higgsfield"), self.user_db)

    # ---- allow-list for sensitive connectors

    def test_sensitive_connectors_wait_for_an_admin(self):
        self.assertFalse(self._item("gmail")["allowed"])
        r = self.client.post("/customize/connectors/gmail/install", json={})
        self.assertEqual(r.status_code, 403)
        self.assertIn("administrator", r.json()["error"])
        self.as_admin()
        self.assertEqual(self.client.put("/customize/connectors/gmail/enabled", json={"enabled": True}).status_code, 200)
        self.as_user()
        self.assertTrue(self._item("gmail")["allowed"])
        r = self.client.post("/customize/connectors/gmail/install", json={"scope": "global"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn((7, "gmail"), self.user_db)                               # forced per user even when asked global

    def test_turning_one_on_keeps_the_ordinary_ones_on(self):
        self.as_admin()
        self.client.put("/customize/connectors/gmail/enabled", json={"enabled": True})
        self.assertTrue(self._item("linear")["allowed"])
        self.assertFalse(self._item("stripe")["allowed"])
        self.client.put("/customize/connectors/linear/enabled", json={"enabled": False})
        self.assertFalse(self._item("linear")["allowed"])

    def test_only_admins_change_the_allow_list_or_the_shared_client(self):
        self.assertEqual(self.client.put("/customize/connectors/gmail/enabled", json={"enabled": True}).status_code, 403)
        r = self.client.put("/customize/connectors/slack/oauth-client", json={"client_id": CLIENT_ID})
        self.assertEqual(r.status_code, 403)

    # ---- url fields and trusted stdio presets

    def test_store_domain_is_required_and_checked(self):
        self.as_admin()
        self.assertEqual(self.client.post("/customize/connectors/shopify-storefront/install", json={}).status_code, 400)
        bad = self.client.post("/customize/connectors/shopify-storefront/install", json={"fields": {"shop": "evil.com/x"}})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/customize/connectors/shopify-storefront/install",
                              json={"scope": "global", "fields": {"shop": "my-store.myshopify.com"}})
        self.assertEqual(ok.status_code, 200, ok.text)
        saved = json.loads(self.cfg_file.read_text())["capabilities"]["mcp_servers"]["shopify-storefront"]
        self.assertEqual(saved["url"], "https://my-store.myshopify.com/api/mcp")
        self.assertNotIn("auth", saved)

    def test_a_user_may_run_the_exact_catalog_command_but_nothing_else(self):
        pre = mcp_catalog.get_preset("shopify-dev")
        req = mm.McpServerReq(name="shopify-dev", scope="user", transport="stdio", command=pre["command"],
                              args=list(pre["args"]))
        self.assertIn("not approved", mv._validate(req, restricted=True) or "")
        self.assertIsNone(mv._validate(req, restricted=True, preset=pre))
        tampered = mm.McpServerReq(name="shopify-dev", scope="user", transport="stdio", command="npx",
                                   args=["-y", "@shopify/dev-mcp@latest", "--extra"])
        self.assertIsNotNone(mv._validate(tampered, restricted=True, preset=pre))
        r = self.client.post("/customize/connectors/shopify-dev/install", json={"scope": "user"})
        self.assertEqual(r.status_code, 200, r.text)

    # ---- uninstall

    def test_uninstall_clears_the_token_client_and_secret(self):
        self.client.post("/customize/connectors/linear/install", json={"scope": "user"})
        for k in ("OAUTH_TOKEN", "OAUTH_CLIENT", "OAUTH_CLIENT_SECRET"):
            self.keychain[f"u7:linear:env:{k}"] = "[PLACEHOLDER]"
        self.keychain["u8:linear:env:OAUTH_TOKEN"] = "[PLACEHOLDER_OTHER_USER]"
        r = self.client.delete("/customize/connectors/linear")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn((7, "linear"), self.user_db)
        self.assertEqual([k for k in self.keychain if k.startswith("u7:")], [])
        self.assertIn("u8:linear:env:OAUTH_TOKEN", self.keychain)          # someone else's sign-in is untouched

    def test_removing_the_shared_install_needs_the_install_permission(self):
        self.as_admin()
        self.client.post("/customize/connectors/canva/install", json={"scope": "global"})
        self.as_user()
        self.assertEqual(self.client.delete("/customize/connectors/canva").status_code, 403)
        self.as_admin()
        self.keychain["canva:env:OAUTH_TOKEN"] = "[PLACEHOLDER]"
        self.assertEqual(self.client.delete("/customize/connectors/canva").status_code, 200)
        self.assertNotIn("canva:env:OAUTH_TOKEN", self.keychain)

    # ---- admin-registered OAuth client

    def test_admin_registers_a_client_once(self):
        self.as_admin()
        self.assertTrue(self._item("slack")["needs_client"])
        r = self.client.put("/customize/connectors/slack/oauth-client",
                            json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.keychain["oauthapp:slack:secret"], CLIENT_SECRET)
        saved = json.loads(self.cfg_file.read_text())["capabilities"]["mcp_catalog_overrides"]["slack"]
        self.assertEqual(saved, {"client_id": CLIENT_ID})                  # the secret is never written to the config
        item = self._item("slack")
        self.assertTrue(item["has_client"])
        self.assertFalse(item["needs_client"])
        self.assertEqual(self.client.delete("/customize/connectors/slack/oauth-client").status_code, 200)
        self.assertNotIn("oauthapp:slack:secret", self.keychain)
        self.assertTrue(self._item("slack")["needs_client"])

    def test_client_setup_only_for_oauth_connectors_and_sane_values(self):
        self.as_admin()
        self.assertEqual(self.client.put("/customize/connectors/time/oauth-client", json={"client_id": CLIENT_ID}).status_code, 404)
        self.assertEqual(self.client.put("/customize/connectors/slack/oauth-client", json={"client_id": "a b"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
