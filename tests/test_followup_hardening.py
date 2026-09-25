"""tests/test_followup_hardening.py - regressions for the follow-up hardening round.

SSE PAN masking, background polls vs idle expiry, admin-issued API tokens + gated
OpenAPI docs, companion device pairing / hashed device ids, the web search pipeline
(parsers, merge, rerank, extraction, cache, query redaction) and the no-inline-script
guard behind the strict CSP. Throwaway auth DB; no network, no model processes.
Run: python -m unittest tests.test_followup_hardening -v
"""

import asyncio
import glob
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth_db, deps

TEST_PAN = "4" + "1" * 15      # standard test number, not a real card


class _TempAuthDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = auth_db._auth_db
        with mock.patch.object(auth_db, "AUTH_DB_FILE", Path(self.tmp.name) / "auth.db"):
            auth_db._auth_db = auth_db._init_auth_db()
        self._audit = [mock.patch("core.audit.auth_db.insert_audit", lambda *a, **k: None)]
        for p in self._audit:
            p.start()

    def tearDown(self):
        for p in self._audit:
            p.stop()
        auth_db._auth_db.close()
        auth_db._auth_db = self._saved
        self.tmp.cleanup()


# ---------------- A: SSE masking ----------------
class SseMaskingTests(unittest.TestCase):
    def test_nested_values_masked(self):
        from core.sse import sse
        out = sse("tool_result", {"id": "t1", "args": {"q": f"card {TEST_PAN}", "list": [TEST_PAN]},
                                  "result": f"row: {TEST_PAN}", "ok": True, "n": 3})
        self.assertTrue(out.startswith("event: tool_result\ndata: "))
        self.assertNotIn(TEST_PAN, out)
        data = json.loads(out.split("data: ", 1)[1])
        self.assertEqual(data["result"], "row: [card ****1111]")
        self.assertEqual(data["args"]["list"], ["[card ****1111]"])
        self.assertEqual(data["n"], 3)

    def test_routes_use_helper_for_tool_events(self):
        for f in ("routes/agent.py", "routes/chat.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            self.assertNotRegex(src, r'f"event: (tool_call|tool_result|permission_request)', f)


# ---------------- B: background polls don't extend the session ----------------
class IdleTouchTests(_TempAuthDb):
    def test_background_request_does_not_slide_expiry(self):
        from core import auth
        from core.auth_provider import UserRecord
        uid = auth_db.create_user(username="idle", password_hash=None)
        raw = auth.create_session(UserRecord(id=uid, username="idle", display_name="idle", is_active=True,
                                             is_super_admin=False, must_change_password=False), None, None)
        h = auth._hash_token(raw)
        auth_db.db().execute("UPDATE auth_sessions SET expires_at = ? WHERE id = ?", (time.time() + 60, h))
        auth_db.db().commit()
        before = auth_db.get_session_row(h)["expires_at"]

        app = FastAPI()
        app.get("/probe")(lambda user=deps.Depends(deps.get_current_user): {"ok": True})
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, raw)
        self.assertEqual(c.get("/probe", headers={"X-A770-Background": "1"}).status_code, 200)
        self.assertEqual(auth_db.get_session_row(h)["expires_at"], before)
        self.assertEqual(c.get("/probe").status_code, 200)
        self.assertGreater(auth_db.get_session_row(h)["expires_at"], before + 60)
        c.close()


# ---------------- D: API tokens + docs ----------------
class ApiTokenTests(_TempAuthDb):
    def setUp(self):
        super().setUp()
        from core.auth_provider import hash_password
        from routes import api_docs, api_tokens
        self.admin = auth_db.create_user(username="admin1", password_hash=None, is_super_admin=True)
        self.alice = auth_db.create_user(username="alice", password_hash=hash_password("[PLACEHOLDER]-pw"))
        auth_db.assign_role(self.alice, "user")          # chat.use
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.include_router(api_docs.router)
        app.include_router(api_tokens.router)
        app.get("/whoami")(lambda user=deps.Depends(deps.get_current_user):
                          {"id": user.id, "perms": sorted(user.permission_keys), "super": user.is_super_admin})
        app.get("/needs-chat")(lambda user=deps.Depends(deps.require_permission("chat.use")): {"ok": True})
        app.get("/needs-db")(lambda user=deps.Depends(deps.require_permission("database.manage")): {"ok": True})
        app.get("/auth/me")(lambda user=deps.Depends(deps.get_current_user): {"ok": True})
        self.app = app
        self.client = TestClient(app)
        self._audit.append(mock.patch.object(api_tokens, "audit_log", lambda *a, **k: None))
        self._audit[-1].start()

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def _as(self, uid):
        from core.auth import _to_principal
        self.app.dependency_overrides[deps.get_current_user] = lambda: _to_principal(uid)

    def _issue(self, perms, user_id=None, days=30):
        self._as(self.admin)
        r = self.client.post("/admin/api-tokens", json={"name": "script", "user_id": user_id or self.alice,
                                                        "permissions": perms, "expires_days": days})
        self.app.dependency_overrides.clear()
        return r

    def test_issue_needs_users_manage(self):
        self._as(self.alice)
        r = self.client.post("/admin/api-tokens", json={"name": "x", "user_id": self.alice,
                                                        "permissions": ["chat.use"]})
        self.assertEqual(r.status_code, 403)

    def test_token_authenticates_with_scopes_only(self):
        r = self._issue(["chat.use"])
        self.assertEqual(r.status_code, 200, r.text)
        tok = r.json()["token"]
        self.assertTrue(tok.startswith("a770_pat_"))
        row = auth_db.db().execute("SELECT token_hash FROM api_tokens").fetchone()
        self.assertNotIn(tok, row["token_hash"])            # only the hash is stored
        h = {"Authorization": f"Bearer {tok}"}
        me = self.client.get("/whoami", headers=h).json()
        self.assertEqual(me["perms"], ["chat.use"])
        self.assertEqual(self.client.get("/needs-chat", headers=h).status_code, 200)
        self.assertEqual(self.client.get("/needs-db", headers=h).status_code, 403)
        self.assertEqual(self.client.get("/auth/me", headers=h).status_code, 403)

    def test_forbidden_and_unheld_permissions_refused(self):
        self.assertEqual(self._issue(["database.manage"]).status_code, 400)
        self.assertEqual(self._issue(["users.manage"], user_id=self.admin).status_code, 400)
        self.assertEqual(self._issue(["audit.view"]).status_code, 400)     # alice doesn't hold it
        self.assertEqual(self._issue(["chat.use"], days=365).status_code, 422)

    def test_super_admin_token_is_not_super(self):
        tok = self._issue(["chat.use", "audit.view"], user_id=self.admin).json()["token"]
        me = self.client.get("/whoami", headers={"Authorization": f"Bearer {tok}"}).json()
        self.assertFalse(me["super"])
        self.assertEqual(me["perms"], ["audit.view", "chat.use"])

    def test_revoked_and_expired_tokens_rejected(self):
        tok = self._issue(["chat.use"]).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        auth_db.db().execute("UPDATE api_tokens SET expires_at = ?", (time.time() - 1,))
        auth_db.db().commit()
        self.assertEqual(self.client.get("/whoami", headers=h).status_code, 401)
        tok2 = self._issue(["chat.use"]).json()["token"]
        tid = auth_db.list_api_tokens()[0]["id"]
        auth_db.revoke_api_token(tid)
        self.assertEqual(self.client.get("/whoami", headers={"Authorization": f"Bearer {tok2}"}).status_code, 401)

    def test_role_removal_narrows_token(self):
        tok = self._issue(["chat.use"]).json()["token"]
        auth_db.db().execute("DELETE FROM user_roles WHERE user_id = ?", (self.alice,))
        auth_db.db().commit()
        self.assertEqual(self.client.get("/needs-chat", headers={"Authorization": f"Bearer {tok}"}).status_code, 403)

    def test_openapi_and_docs_gated(self):
        self.assertEqual(self.client.get("/openapi.json").status_code, 401)
        r = self.client.get("/docs", follow_redirects=False)
        self.assertEqual(r.status_code, 307)
        self.assertIn("/login", r.headers["location"])
        tok = self._issue(["chat.use"]).json()["token"]
        r = self.client.get("/openapi.json", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("bearerAuth", r.json()["components"]["securitySchemes"])

    def test_csrf_still_required_with_cookie_plus_token(self):
        from core.auth import CSRF_COOKIE, SESSION_COOKIE
        from core.csrf import CSRFMiddleware
        app = FastAPI()
        app.add_middleware(CSRFMiddleware)
        app.post("/act")(lambda: {"ok": True})
        c = TestClient(app)
        self.assertEqual(c.post("/act", headers={"Authorization": "Bearer a770_pat_x"}).status_code, 200)
        c.cookies.set(SESSION_COOKIE, "s")
        c.cookies.set(CSRF_COOKIE, "[PLACEHOLDER_CSRF]")
        self.assertEqual(c.post("/act", headers={"Authorization": "Bearer a770_pat_x"}).status_code, 403)
        c.close()


# ---------------- F: device ids + pairing ----------------
class DeviceTests(_TempAuthDb):
    def test_hardware_ids_hashed_consistently(self):
        from core.request_context import device_hash, normalize_device_id
        raw = "dev_win_0123456789abcdef"
        h = normalize_device_id(raw)
        self.assertTrue(h.startswith("dev_h_"))
        self.assertNotIn("0123456789abcdef", h)
        self.assertEqual(normalize_device_id(h), h)            # idempotent (new companions send h)
        self.assertEqual(h, device_hash(raw))
        self.assertEqual(normalize_device_id("dev_abc123_xyz"), "dev_abc123_xyz")   # random browser ids

    def test_companion_js_hash_matches_server(self):
        import shutil
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node not installed")
        from core.request_context import device_hash
        js = ('const c=require("crypto");process.stdout.write("dev_h_"+c.createHash("sha256")'
              '.update("a770-device:dev_win_0123456789abcdef").digest("hex").slice(0,24))')
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True).stdout
        self.assertEqual(out, device_hash("dev_win_0123456789abcdef"))
        src = (ROOT / "companion" / "main.js").read_text(encoding="utf-8")
        self.assertIn('update("a770-device:" + raw)', src)

    def test_companion_router_mounted_before_proxy_catch_all(self):
        # the proxy's /{path:path} route would otherwise 404 POST /companion/pair
        src = (ROOT / "server_manager.py").read_text(encoding="utf-8")
        comp = src.index("app.include_router(companion_router")
        proxy = src.index("app.include_router(proxy_router")
        self.assertLess(comp, proxy)

    def _ws_app(self):
        from core import companion_bridge as cb
        app = FastAPI()
        app.include_router(cb.router)
        return cb, app

    def test_device_key_pairing_and_binding(self):
        from core.auth import _to_principal
        cb, app = self._ws_app()
        uid = auth_db.create_user(username="dev", password_hash=None)
        app.dependency_overrides[deps.get_current_user] = lambda: _to_principal(uid)
        with mock.patch.object(cb, "audit_log", lambda *a, **k: None):
            c = TestClient(app)
            r = c.post("/companion/pair", json={"device_id": "dev_win_00aa11bb22cc33dd", "name": "laptop"})
            self.assertEqual(r.status_code, 200, r.text)
            key = r.json()["device_key"]
            rows = auth_db.db().execute("SELECT device_hash, key_hash FROM companion_devices").fetchall()
            self.assertNotIn("00aa11bb22cc33dd", json.dumps([dict(x) for x in rows]))
            hdr = {"Authorization": f"Bearer {key}"}

            with c.websocket_connect("/ws/companion", headers=hdr) as ws:
                ws.send_text(json.dumps({"type": "hello", "hostname": "h", "device_id": "dev_win_00aa11bb22cc33dd"}))
                time.sleep(0.2)
                self.assertTrue(cb.is_connected(uid))
            # same key from another machine -> 4403
            from starlette.websockets import WebSocketDisconnect
            with c.websocket_connect("/ws/companion", headers=hdr) as ws:
                ws.send_text(json.dumps({"type": "hello", "hostname": "h", "device_id": "dev_win_ffffffffffffffff"}))
                with self.assertRaises(WebSocketDisconnect) as cm:
                    ws.receive_text()
                self.assertEqual(cm.exception.code, 4403)
            # revoked key -> 4401
            rid = auth_db.list_companion_devices(uid)[0]["id"]
            self.assertEqual(c.delete(f"/companion/devices/{rid}").status_code, 200)
            with self.assertRaises(WebSocketDisconnect) as cm:
                with c.websocket_connect("/ws/companion", headers=hdr) as ws:
                    ws.receive_text()
            self.assertEqual(cm.exception.code, 4401)
            c.close()


# ---------------- H: web search pipeline ----------------
DDG_HTML = """<html><body>
<div class="result results_links results_links_deep web-result">
  <h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.bb.org.bd%2Fen%2Fmps&rut=x">Monetary Policy - Bangladesh Bank</a></h2>
  <a class="result__snippet" href="#">Policy rate cut to 9.5 percent.</a>
</div>
<div class="result results_links web-result">
  <h2><a class="result__a" href="https://example.org/no-snippet">No snippet result</a></h2>
</div>
<div class="result results_links web-result">
  <h2><a class="result__a" href="https://news.example.com/bb?utm_source=x">BB cuts rate</a></h2>
  <a class="result__snippet" href="#">Bangladesh Bank cut its policy rate.</a>
</div>
<div class="result result--ad"><a class="result__a" href="https://ads.example/">Ad</a></div>
</body></html>"""

BING_HTML = """<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="https://news.example.com/bb/">BB cuts rate (Bing)</a></h2>
  <div class="b_caption"><p>Bangladesh Bank cut its policy rate by 50 basis points to 9.5 percent.</p></div></li>
<li class="b_algo"><h2><a href="https://other.example/x">Other</a></h2><p>Unrelated text.</p></li>
</ol></body></html>"""

ARTICLE_HTML = """<html><head><meta charset="windows-1252"><title>BB cuts rate</title></head><body>
<nav><a href="/">Home</a><a href="/news">News</a></nav>
<div class="cookie-banner">We use cookies. Accept all?</div>
<article><h1>BB cuts policy rate</h1>
<p>Bangladesh Bank cut its key policy interest rate by 50 basis points to 9.5 percent.</p>
<p>The new repo rate becomes effective from August 2, the central bank said in a statement.</p>
<p>Analysts expect lending rates to ease gradually over the coming quarter as a result.</p>
<p>The decision was made at the Monetary Policy Committee meeting chaired by the governor.</p>
</article>
<aside class="sidebar">Most read: cricket scores</aside>
<footer>Copyright</footer></body></html>"""


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        from core import web_search as ws
        self.ws = ws
        ws._cache.clear()
        self._p = mock.patch.object(ws, "embed", self._no_embed)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    @staticmethod
    async def _no_embed(texts):
        return None

    def test_ddg_parser_keeps_snippets_aligned(self):
        rows = self.ws.parse_ddg(DDG_HTML)
        self.assertEqual([r["url"] for r in rows], ["https://www.bb.org.bd/en/mps", "https://example.org/no-snippet",
                                                   "https://news.example.com/bb?utm_source=x"])
        self.assertEqual(rows[1]["snippet"], "")
        self.assertEqual(rows[2]["snippet"], "Bangladesh Bank cut its policy rate.")

    def test_bing_parser(self):
        rows = self.ws.parse_bing(BING_HTML)
        self.assertEqual(rows[0]["url"], "https://news.example.com/bb/")
        self.assertIn("50 basis points", rows[0]["snippet"])

    def test_merge_dedups_and_boosts_multi_engine(self):
        merged = self.ws.merge({"duckduckgo": self.ws.parse_ddg(DDG_HTML), "bing": self.ws.parse_bing(BING_HTML)})
        urls = [self.ws.norm_url(m["url"]) for m in merged]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(self.ws.norm_url(merged[0]["url"]), "news.example.com/bb")   # found by both
        self.assertEqual(merged[0]["engines"], ["duckduckgo", "bing"])
        self.assertIn("50 basis points", merged[0]["snippet"])                        # longer snippet kept

    def test_rerank_lexical_fallback(self):
        items = self.ws.merge({"bing": self.ws.parse_bing(BING_HTML)})
        ranked = asyncio.run(self.ws.rerank("bangladesh bank policy rate", items))
        self.assertEqual(ranked[0]["url"], "https://news.example.com/bb/")

    def test_rerank_uses_embeddings(self):
        items = [{"title": "a", "snippet": "x", "url": "https://a.example", "rrf": 1.0},
                 {"title": "b", "snippet": "y", "url": "https://b.example", "rrf": 1.0}]

        async def fake(texts):
            return [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]     # query ~ doc b
        with mock.patch.object(self.ws, "embed", fake):
            ranked = asyncio.run(self.ws.rerank("q", items))
        self.assertEqual(ranked[0]["url"], "https://b.example")

    def test_extract_main_content_and_charset(self):
        page = self.ws.extract_html(self.ws.decode_html(ARTICLE_HTML.encode("cp1252"), None), "https://x.example/a")
        self.assertEqual(page["title"], "BB cuts rate")
        self.assertIn("9.5 percent", page["text"])
        for junk in ("cookies", "cricket", "Copyright", "Home"):
            self.assertNotIn(junk, page["text"])

    def test_best_passages(self):
        text = "\n".join(["filler text about weather " * 20] * 5 + ["the repo rate is 9.5 percent now"])
        top = asyncio.run(self.ws.best_passages("repo rate percent", text, k=1))
        self.assertIn("9.5 percent", top[0])

    def test_region_and_recency_params(self):
        seen = []

        def fake_get(url, headers):
            seen.append((url, headers))
            return mock.Mock(status_code=200, text=BING_HTML if "bing" in url else DDG_HTML)
        with mock.patch.object(self.ws, "_get", fake_get), \
             mock.patch.dict(self.ws.APP_CONFIG, {"web": {"region": "bd-en"}}):
            self.ws.bing_search("x", "week")
            self.ws.ddg_search("x", "day")
        self.assertIn("cc=BD", seen[0][0])
        self.assertIn("filters=", seen[0][0])
        self.assertIn("df=d", seen[1][0])
        self.assertTrue(seen[0][1]["Accept-Language"].startswith("en-BD"))

    def test_search_caches(self):
        calls = []

        def ddg(q, r=""):
            calls.append(q)
            return self.ws.parse_ddg(DDG_HTML)
        with mock.patch.dict(self.ws.BACKENDS, {"duckduckgo": ddg, "bing": lambda q, r="": []}), \
             mock.patch.dict(self.ws.APP_CONFIG, {"web": {"auto_fetch_top": 0}}):
            a = asyncio.run(self.ws.search("bangladesh bank"))
            b = asyncio.run(self.ws.search("Bangladesh Bank"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(a["results"], b["results"])

    def test_query_pii_redacted(self):
        q, kinds = self.ws.redact_query("status of wallet 01712345678 for someone@example.com txn 998877665544")
        self.assertNotIn("01712345678", q)
        self.assertNotIn("@example.com", q)
        self.assertNotIn("998877665544", q)
        self.assertEqual(set(kinds), {"email", "phone number", "account-like number"})

    def test_tool_output_has_numbered_sources(self):
        from core import web_tools

        async def fake_search(q, recency="", auto_fetch=None):
            return {"query": q, "engines": ["duckduckgo"], "errors": {},
                    "results": [{"title": "T", "url": "https://t.example", "snippet": "S",
                                 "passages": ["excerpt one"]}]}
        with mock.patch.object(self.ws, "search", fake_search):
            out = asyncio.run(web_tools.tool_web_search({"query": "q"}))
            self.assertIn("[1] T", out)
            self.assertIn("> excerpt one", out)
            refused = asyncio.run(web_tools.tool_web_search({"query": f"card {TEST_PAN}"}))
        self.assertTrue(refused.startswith("error: refusing to send a payment card number"), refused)


# ---------------- C: no inline script (strict CSP) ----------------
class NoInlineScriptTests(unittest.TestCase):
    def test_pages_and_templates_have_no_inline_handlers(self):
        bad = []
        files = [ROOT / "ui.html", ROOT / "settings.html", ROOT / "login.html"] + \
            [Path(p) for p in glob.glob(str(ROOT / "static" / "js" / "*.js"))]
        for f in files:
            s = f.read_text(encoding="utf-8")
            bad += [(f.name, m.group(0)[:60]) for m in re.finditer(r'<[a-zA-Z][^<>]*\son[a-z]+\s*=\s*["\']', s)]
            if f.suffix == ".html":
                bad += [(f.name, m.group(0)) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>", s)]
        self.assertEqual(bad, [])

    def test_cdn_scripts_have_sri(self):
        s = (ROOT / "ui.html").read_text(encoding="utf-8")
        for tag in re.findall(r'<script src="https://[^>]+>', s):
            self.assertIn('integrity="sha384-', tag)


if __name__ == "__main__":
    unittest.main()
