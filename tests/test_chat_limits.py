"""tests/test_chat_limits.py - the Chat limits settings (core/chat_limits.py, /control/chat_limits) and the write cap per lane.

Run: python -m pytest tests/test_chat_limits.py -q -p no:cacheprovider
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import chat_limits, deck_research, deps, request_context as rc
from core.agent_tools import limits
from core.auth import Principal
from routes.capabilities.endpoints import settings as caps

ROOT = Path(__file__).resolve().parents[1]


def principal(perms):
    return Principal(id=1, username="u1", display_name="u", is_super_admin=False,
                     must_change_password=False, role_names=["user"], permission_keys=set(perms))


class ChatLimitsRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "app.json"
        shutil.copy(ROOT / "config" / "app.json", self.cfg_path)
        self.audit = []
        self.who = {"perms": {"chat.use", "settings.orchestration.configure"}}
        self._patches = [
            mock.patch.object(caps, "CONFIG_FILE", self.cfg_path),
            mock.patch.dict("core.small_model.APP_CONFIG", {"chat": {}}),
            mock.patch("routes.capabilities.endpoints.settings.audit_log", lambda *a, **k: self.audit.append(k)),
            mock.patch("core.deps.audit_log", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        app = FastAPI()
        app.include_router(caps.router)
        app.dependency_overrides[deps.get_current_user] = lambda: principal(self.who["perms"])
        self.client = TestClient(app)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def saved(self):
        return json.loads(self.cfg_path.read_text(encoding="utf-8"))["chat"]

    def test_get_returns_every_key_with_default_and_range(self):
        j = self.client.get("/control/chat_limits").json()
        for k, (default, lo, hi, help_) in chat_limits.CHAT_LIMITS.items():
            self.assertEqual(j["defaults"][k], default, k)
            self.assertEqual(j["ranges"][k], [lo, hi], k)
            self.assertEqual(j["values"][k], default, k)          # empty live config -> defaults
            self.assertTrue(help_ and j["help"][k], k)
        self.assertEqual(set(j["levels"]), set(chat_limits.LEVELS))
        self.assertEqual(j["levels"]["medium"], chat_limits.DEFAULT_LEVELS["medium"])
        self.assertIn("overrides the level's Token budget", j["turn_token_note"])
        self.assertIn("thinking counts against it", j["turn_token_note"])

    def test_post_saves_valid_values_persists_and_applies_live(self):
        r = self.client.post("/control/chat_limits", json={
            "doc_max_rounds": 30, "auto_continue": 5, "deck_repeat_penalty": 1.1,
            "thinking_budgets": {"high": {"token_budget": 24000, "tool_calls": 20}},
            "web_budget": {"open": {"deep": 50}}})
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j["values"]["doc_max_rounds"], 30)
        self.assertEqual(j["levels"]["high"]["token_budget"], 24000)
        self.assertEqual(j["levels"]["high"]["max_rounds"], 20)       # a cell that was not sent keeps its value
        self.assertEqual(j["web_budget"]["open"]["deep"], 50)
        s = self.saved()
        self.assertEqual((s["doc_max_rounds"], s["auto_continue"], s["deck_repeat_penalty"]), (30, 5, 1.1))
        self.assertEqual(s["thinking_budgets"]["high"]["token_budget"], 24000)
        self.assertEqual(s["web_budget"]["open"]["deep"], 50)
        self.assertEqual(s["web_soft_chars"], 32000, "keys that were not sent are left alone")
        self.assertEqual(chat_limits.chat_limit("doc_max_rounds"), 30, "applied without a restart")
        self.assertEqual(chat_limits.level_row("high")["token_budget"], 24000)

    def test_out_of_range_is_clamped_and_unknown_keys_ignored(self):
        r = self.client.post("/control/chat_limits", json={
            "doc_max_rounds": 9999, "auto_continue": -4, "not_a_limit": 5,
            "thinking_budgets": {"low": {"token_budget": 1, "tool_calls": 500, "bogus": 3}, "ultra": {"tool_calls": 1}}})
        self.assertEqual(r.status_code, 200, r.text)
        v, lv = r.json()["values"], r.json()["levels"]
        self.assertEqual(v["doc_max_rounds"], 100)
        self.assertEqual(v["auto_continue"], 0)
        self.assertEqual(lv["low"]["token_budget"], 500)
        self.assertEqual(lv["low"]["tool_calls"], 100)
        s = self.saved()
        self.assertNotIn("not_a_limit", s)
        self.assertNotIn("ultra", s["thinking_budgets"])
        self.assertNotIn("bogus", s["thinking_budgets"]["low"])

    def test_a_non_number_is_refused_and_nothing_is_written(self):
        before = self.cfg_path.read_text(encoding="utf-8")
        self.assertEqual(self.client.post("/control/chat_limits", json={"doc_max_rounds": "lots"}).status_code, 400)
        self.assertEqual(self.client.post("/control/chat_limits", json={"auto_continue": True}).status_code, 400)
        self.assertEqual(self.client.post("/control/chat_limits", json={"only_unknown": 1}).status_code, 400)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.audit, [])

    def test_needs_the_orchestration_permission(self):
        self.who["perms"] = {"chat.use"}
        self.assertEqual(self.client.get("/control/chat_limits").status_code, 403)
        before = self.cfg_path.read_text(encoding="utf-8")
        self.assertEqual(self.client.post("/control/chat_limits", json={"doc_max_rounds": 5}).status_code, 403)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), before)

    def test_a_change_is_audit_logged(self):
        self.client.post("/control/chat_limits", json={"doc_max_rounds": 31, "thinking_budgets": {"deep": {"max_rounds": 50}}})
        self.assertEqual(len(self.audit), 1)
        a = self.audit[0]
        self.assertEqual(a["action"], "chat.limits")
        self.assertEqual(a["permission_key"], "settings.orchestration.configure")
        self.assertEqual(a["detail"]["doc_max_rounds"], 31)
        self.assertEqual(a["detail"]["thinking_budgets.deep.max_rounds"], 50)

    def test_the_old_web_settings_place_still_reports_the_matrix(self):
        from core.small_model import APP_CONFIG
        APP_CONFIG["chat"] = {"thinking_budgets": {"medium": {"tool_calls": 9999, "token_budget": 12000, "max_rounds": 12}}}
        self.assertEqual(chat_limits.level_matrix()["medium"]["tool_calls"], 100)
        self.assertEqual(set(chat_limits.level_matrix()), set(chat_limits.LEVELS))


class ChatReaders(unittest.TestCase):
    def test_the_reader_clamps_a_bad_config_value(self):
        cfg = {"chat": {"doc_max_rounds": 5000, "auto_continue": "x", "runaway_retries": -3, "build_temperature": 9,
                        "thinking_budgets": {"low": {"tool_calls": "many", "token_budget": 10, "max_rounds": 1000}}}}
        self.assertEqual(chat_limits.chat_limit("doc_max_rounds", cfg), 100)
        self.assertEqual(chat_limits.chat_limit("auto_continue", cfg), 3)          # not a number -> default
        self.assertEqual(chat_limits.chat_limit("runaway_retries", cfg), 0)
        self.assertEqual(chat_limits.chat_limit("build_temperature", cfg), 2.0)
        self.assertEqual(chat_limits.chat_limit("project_max_rounds", cfg), 40)    # missing -> default
        row = chat_limits.level_row("low", cfg)
        self.assertEqual(row, {"tool_calls": 4, "token_budget": 500, "max_rounds": 100})

    def test_the_chat_code_goes_through_it(self):
        src = (ROOT / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        for needle in ('chat_limits.level_row(', 'chat_limits.chat_limit("doc_max_rounds")', 'chat_limits.chat_limit("auto_continue")',
                       'chat_limits.chat_limit("runaway_retries")', 'chat_limits.chat_limit("repeat_call_limit")',
                       'chat_limits.chat_limit("project_max_rounds")', 'chat_limits.chat_limit("run_timeout_s")'):
            self.assertIn(needle, src)
        self.assertNotIn('default_matrix', src)

    def test_deck_research_reads_clamped_values(self):
        cfg = {"chat": {"deck_max_tokens": 10**9, "deck_research_calls": 500, "web_soft_chars": 10}}
        self.assertEqual(deck_research.build_limits(cfg, "build")["max_tokens"], 65536)
        self.assertEqual(deck_research.calls(cfg), 40)
        self.assertEqual(deck_research.web_cap("", cfg), (6, 500))


class WriteCapPerLane(unittest.TestCase):
    CFG = {"agent": {"write_file_max_tokens": 12000, "chunk_target_tokens": 8000,
                     "executor_max_tokens": 16000 // 2, "main_max_tokens": 32000}}

    def cap(self, lane):
        tok = rc.set_current_lane(lane)
        try:
            with mock.patch.dict("core.small_model.APP_CONFIG", self.CFG):
                return limits.write_cap_tokens()
        finally:
            rc.reset_current_lane(tok)

    def test_in_a_chat_turn_the_cap_follows_the_setting_and_the_main_lane(self):
        self.assertEqual(self.cap("main"), 12000)                  # min(12000, 2 * min(8000, 32000 // 2))
        cfg = {"agent": {**self.CFG["agent"], "main_max_tokens": 10000}}
        tok = rc.set_current_lane("main")
        try:
            with mock.patch.dict("core.small_model.APP_CONFIG", cfg):
                self.assertEqual(limits.write_cap_tokens(), 10000)  # the main lane carries at most 2 * (10000 // 2)
        finally:
            rc.reset_current_lane(tok)

    def test_in_an_executor_step_it_stays_limited_by_the_executor_cap(self):
        self.assertEqual(self.cap("executor"), 8000)               # 2 * (8000 // 2)

    def test_outside_a_run_it_is_the_executor_cap(self):
        with mock.patch.dict("core.small_model.APP_CONFIG", self.CFG):
            self.assertEqual(limits.write_cap_tokens(), 8000)

    def test_the_stream_oversize_check_uses_the_running_lane(self):
        from routes.common import sse_stream
        args = "x" * int(9000 * limits.CHARS_PER_TOKEN)             # ~9000 tokens: past the executor cap, under the main one
        for lane, over in (("main", False), ("executor", True)):
            tok = rc.set_current_lane(lane)
            try:
                with mock.patch.dict("core.small_model.APP_CONFIG", self.CFG):
                    self.assertEqual(sse_stream._oversize_write("write_file", "a.py", args), over, lane)
            finally:
                rc.reset_current_lane(tok)

    def test_the_chat_route_runs_its_turn_on_the_main_lane(self):
        src = (ROOT / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn('set_current_lane("main")', src)
        self.assertIn("reset_current_lane(lane_token)", src)

    def test_the_settings_view_reports_the_effective_write_caps(self):
        with mock.patch.dict("core.small_model.APP_CONFIG", self.CFG):
            w = caps._limits_view()["effective"]["write"]
        self.assertEqual(w["setting"], 12000)
        self.assertEqual(w["main"]["write_cap"], 12000)
        self.assertEqual(w["main"]["limited_by"], "setting")
        self.assertEqual(w["executor"]["write_cap"], 8000)
        self.assertEqual(w["executor"]["limited_by"], "the executor lane output cap")


if __name__ == "__main__":
    unittest.main()
