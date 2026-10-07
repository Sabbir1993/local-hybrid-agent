"""tests/test_kb_policy.py - what cloud models may read from the knowledge base:
categories (per source) + sensitive-content rules (per chunk). Run: python -m unittest tests.test_kb_policy -v"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import auth_db, knowledge_access as ka, knowledge_rules as kr
from core.small_model import APP_CONFIG
from tests.test_knowledge_routes import _KbApp


class PolicyApiTests(_KbApp):
    def _src(self, title="doc"):
        return self.client.post("/knowledge/text", json={"title": title, "text": "x"}).json()["source"]["id"]

    def test_defaults_are_seeded_and_deny_by_default(self):
        j = self.client.get("/knowledge/policy").json()
        cats = {c["name"]: c["cloud_ok"] for c in j["categories"]}
        self.assertEqual(cats["Public"], 1)
        self.assertEqual(cats["HR"], 0)
        self.assertTrue(any(r["builtin"] and r["enabled"] for r in j["rules"]))
        self.assertNotIn(self._src(), auth_db.cloud_ok_source_ids())      # new source: local only

    def test_category_decides_cloud_access_over_the_source_switch(self):
        sid = self._src()
        self.client.put(f"/knowledge/{sid}/cloud", json={"allowed": True})
        self.assertIn(sid, auth_db.cloud_ok_source_ids())                 # own switch, no category
        r = self.client.put(f"/knowledge/{sid}/category", json={"category": "HR"})
        self.assertEqual(r.json()["source"]["category"], "HR")
        self.assertNotIn(sid, auth_db.cloud_ok_source_ids())              # category says local only
        self.client.put(f"/knowledge/{sid}/category", json={"category": "Public"})
        self.assertIn(sid, auth_db.cloud_ok_source_ids())
        self.assertTrue(self.client.get("/knowledge").json()["sources"][0]["cloud_effective"])

    def test_unknown_category_and_missing_source(self):
        sid = self._src()
        self.assertEqual(self.client.put(f"/knowledge/{sid}/category", json={"category": "Nope"}).status_code, 400)
        self.assertEqual(self.client.put("/knowledge/999/category", json={"category": "HR"}).status_code, 404)

    def test_deleting_a_category_returns_sources_to_their_own_switch(self):
        sid = self._src()
        self.client.put(f"/knowledge/{sid}/category", json={"category": "Public"})
        self.client.delete("/knowledge/categories/Public")
        self.assertNotIn(sid, auth_db.cloud_ok_source_ids())
        self.assertIsNone(self.client.get("/knowledge").json()["sources"][0]["category"])

    def test_new_category_and_rule_validation(self):
        self.client.put("/knowledge/categories", json={"name": " Product  docs ", "cloud_ok": True})
        names = [c["name"] for c in self.client.get("/knowledge/policy").json()["categories"]]
        self.assertIn("Product docs", names)
        bad = self.client.post("/knowledge/rules", json={"name": "r", "kind": "regex", "pattern": "("})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.post("/knowledge/rules", json={"name": "r", "kind": "regex", "pattern": "a*"}).status_code, 400)
        ok = self.client.post("/knowledge/rules", json={"name": "Project Falcon", "kind": "keywords", "pattern": "falcon, project x"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.client.post("/knowledge/rules", json={"name": "Project Falcon", "kind": "keywords", "pattern": "z"}).status_code, 409)

    def test_builtin_rules_can_be_turned_off_but_not_deleted(self):
        rid = next(r["id"] for r in self.client.get("/knowledge/policy").json()["rules"] if r["builtin"])
        self.assertEqual(self.client.delete(f"/knowledge/rules/{rid}").status_code, 404)
        self.client.put(f"/knowledge/rules/{rid}", json={"enabled": False})
        self.assertFalse(next(r for r in auth_db.list_knowledge_rules() if r["id"] == rid)["enabled"])

    def test_permission_is_required(self):
        self.me.permission_keys = set()
        self.assertEqual(self.client.get("/knowledge/policy").status_code, 403)
        self.assertEqual(self.client.put("/knowledge/categories", json={"name": "x"}).status_code, 403)

    def test_test_endpoint_reports_matching_rules(self):
        j = self.client.post("/knowledge/rules/test", json={"text": "Her salary is high, call 01712345678"}).json()
        self.assertIn("Salary and compensation", j["matches"])
        self.assertIn("Phone numbers", j["matches"])
        self.assertEqual(self.client.post("/knowledge/rules/test", json={"text": "Our refund window is 7 days."}).json()["matches"], [])


class WebPolicyApiTests(_KbApp):
    def setUp(self):
        super().setUp()
        self._saved_cfg = {k: APP_CONFIG.get(k) for k in ("knowledge", "chat")}
        self.disk = {}                       # stands in for config/app.json: the real file is never touched
        self._p = mock.patch("core.config.update_app_config", side_effect=lambda m: (m(self.disk), self.disk)[1])
        self._p.start()

    def tearDown(self):
        self._p.stop()
        for k, v in self._saved_cfg.items():
            if v is None:
                APP_CONFIG.pop(k, None)
            else:
                APP_CONFIG[k] = v
        super().tearDown()

    def test_policy_payload_has_the_budget_table(self):
        w = self.client.get("/knowledge/policy").json()["web"]
        self.assertEqual(w["tiers"], ["low", "medium", "high", "deep"])
        self.assertEqual(w["budget"]["partial"]["deep"], 12)
        self.assertEqual(w["budget"]["open"]["deep"], 20)

    def test_save_updates_config_and_live_settings(self):
        r = self.client.put("/knowledge/web-policy", json={
            "gap_fill": False, "full_cos": 0.8, "budget": {"partial": {"high": 9}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.disk["knowledge"]["web_gap_fill"])
        self.assertEqual(self.disk["knowledge"]["full_cos"], 0.8)
        self.assertEqual(self.disk["chat"]["web_budget"]["partial"]["high"], 9)
        self.assertEqual(r.json()["web"]["budget"]["partial"]["high"], 9)
        from core import kb_coverage
        self.assertFalse(kb_coverage.gap_fill_enabled())

    def test_bad_values_are_refused_without_saving(self):
        for body in ({"full_cos": 0.2}, {"budget": {"nope": {"low": 1}}}, {"budget": {"partial": {"low": 500}}},
                     {"budget": {"partial": {"turbo": 1}}}, {"budget": {"partial": {"low": "x"}}}):
            self.assertEqual(self.client.put("/knowledge/web-policy", json=body).status_code, 400, body)
        self.assertEqual(self.disk, {})

    def test_permission_is_required(self):
        self.me.permission_keys = set()
        self.assertEqual(self.client.put("/knowledge/web-policy", json={"gap_fill": True}).status_code, 403)


class ChunkRuleTests(_KbApp):
    def setUp(self):
        super().setUp()
        self._cfg = APP_CONFIG.get("knowledge")
        APP_CONFIG["knowledge"] = {"cloud_policy": "local_only"}

    def tearDown(self):
        if self._cfg is None:
            APP_CONFIG.pop("knowledge", None)
        else:
            APP_CONFIG["knowledge"] = self._cfg
        super().tearDown()

    def test_default_rules_hold_back_sensitive_text_only(self):
        for text in ("The monthly salary band is 50k", "NID: 1234567890123", "account no 1234567890123",
                     "write to hr@example.com", "mobile +8801712345678", "card 4111 1111 1111 1111"):
            self.assertTrue(kr.sensitive_reason(text), text)
        for text in ("Refunds are processed within 7 days.", "Opening hours 9-5", "order 12345"):
            self.assertIsNone(kr.sensitive_reason(text), text)

    def test_hits_need_local_checks_source_and_chunk_text(self):
        sid = auth_db.create_knowledge_source("p", "text")
        auth_db.set_source_category(sid, "Public")
        safe = {"source_id": sid, "text": "Refunds take 7 days."}
        leaky = {"source_id": sid, "text": "Refunds take 7 days. Contact ceo@example.com"}
        self.assertFalse(ka.hits_need_local([safe]))
        self.assertTrue(ka.hits_need_local([safe, leaky]))
        self.assertEqual(ka.cloud_safe_hits([safe, leaky]), [safe])
        self.assertTrue(ka.hits_need_local([{"source_id": sid + 1, "text": "ok"}]))   # source not cleared

    def test_disabling_a_rule_applies_immediately(self):
        rid = next(r["id"] for r in auth_db.list_knowledge_rules() if r["name"] == "Email addresses")
        self.assertTrue(kr.sensitive_reason("a@b.com"))
        auth_db.set_knowledge_rule_enabled(rid, False)
        self.assertIsNone(kr.sensitive_reason("a@b.com"))

    def test_admin_keyword_rule(self):
        auth_db.add_knowledge_rule("Falcon", "keywords", "project falcon, falcon")
        self.assertEqual(kr.sensitive_reason("status of Project Falcon"), "Falcon")
        self.assertIsNone(kr.sensitive_reason("falconry is a sport"))     # whole words only

    def test_allow_policy_skips_everything(self):
        APP_CONFIG["knowledge"] = {"cloud_policy": "allow"}
        self.assertFalse(ka.hits_need_local([{"source_id": 99, "text": "salary 5000"}]))


if __name__ == "__main__":
    unittest.main()
