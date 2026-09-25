"""Lane registry + job routing (core/lanes.py), answer check (core/verifier.py),
legacy plaintext key purge (core/cloud.import_legacy_file)."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import cloud, credentials, lanes, verifier  # noqa: E402
from core import small_model  # noqa: E402


class _Inst:
    """Stand-in for a SmallModelInstance: never spawns llama-server."""

    def __init__(self, name, kind="chat", available=True, up=False):
        self.role, self.kind, self._avail, self._up = name, kind, available, up
        self.model_path = Path(f"{name}.gguf")
        self.client = _Client(f"local:{name}")
        self.last_used = 0
        self.port, self.load_error = 0, None

    @property
    def available(self):
        return self._avail

    def is_up(self):
        return self._up

    async def ensure_loaded(self):
        self._up = True


class _Resp:
    def __init__(self, text):
        self._text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


class _Client:
    def __init__(self, name, reply="ok", fail=False):
        self.name, self.reply, self.fail, self.calls = name, reply, fail, []

    async def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        if self.fail:
            raise RuntimeError(f"{self.name} down")
        return _Resp(self.reply if not callable(self.reply) else self.reply(json))


class LaneTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app_json = root / "app.json"
        self.app_json.write_text(json.dumps({"provider": {}, "cloud": {}}), encoding="utf-8")
        self.vault = {}
        self.sm_cfg = {
            "executor": {"model": "e.gguf", "port": 8091},
            "vision": {"model": "v.gguf", "mmproj": "mm.gguf", "port": 8092},
            "embedder": {"model": "n.gguf", "port": 8093},
        }
        self.insts = {"executor": _Inst("executor"), "vision": _Inst("vision", "vision"),
                      "embedder": _Inst("embedder", "embed")}
        self.patches = [
            mock.patch.object(cloud, "PROVIDERS_DIR", root / "providers"),
            mock.patch.object(cloud, "CONFIG_FILE", self.app_json),
            mock.patch.object(credentials, "set_token", lambda k, v: self.vault.__setitem__(k, v)),
            mock.patch.object(credentials, "get_token", lambda k: self.vault.get(k)),
            mock.patch.object(credentials, "has_token", lambda k: bool(self.vault.get(k))),
            mock.patch.object(credentials, "delete_token", lambda k: self.vault.pop(k, None)),
            mock.patch.object(cloud, "check_provider_url", lambda u: u),
            mock.patch.dict(small_model.APP_CONFIG, {"small_models": self.sm_cfg}),
            mock.patch.object(small_model.small_models, "instances", self.insts),
            mock.patch.object(lanes.Target, "_main_up", False, create=True),
        ]
        for p in self.patches:
            p.start()
        self.main_client = _Client("main")
        self._main = mock.patch("core.state.state")
        st = self._main.start()
        st.client = self.main_client
        st.process = mock.Mock(poll=lambda: None)
        st.profile = {}
        cloud.reload()

    def tearDown(self):
        self._main.stop()
        for p in reversed(self.patches):
            p.stop()
        cloud.reload()
        self.tmp.cleanup()

    def add_cloud(self, uid=1, prov="p", model="m"):
        cloud.save_provider(uid, prov, {"base_url": "https://api.example/v1", "api_key": "[PLACEHOLDER_KEY]",
                                        "models": [{"id": model}]})
        return f"{prov}/{model}"


class RoutingTests(LaneTestBase):
    def test_defaults_match_todays_lanes(self):
        rm = lanes.role_map(1)
        self.assertEqual(rm["summarize"], "executor")
        self.assertEqual(rm["agent.reason"], "main")
        self.assertEqual(rm["embed"], "embedder")
        self.assertEqual(rm["vision"], "vision")
        route = [t.lane for t in lanes.targets("summarize", 1)]
        self.assertEqual(route, ["executor", "main"])
        self.assertFalse(any(t.is_cloud for t in lanes.targets("summarize", 1)))

    def test_custom_cloud_lane_used_when_mapped(self):
        key = self.add_cloud()
        lanes.save_user_lane(1, "reviewer-cloud", {"kind": "chat", "cloud": key, "label": "Reviewer"})
        lanes.set_role_map(1, {"summarize": "reviewer-cloud"})
        route = lanes.targets("summarize", 1)
        self.assertTrue(route[0].is_cloud)
        self.assertEqual(route[0].lane, "reviewer-cloud")
        self.assertEqual([t.lane for t in route[1:]], ["main", "executor"])
        # another user never sees it
        self.assertNotIn("reviewer-cloud", lanes.registry(2))
        self.assertEqual(lanes.role_map(2)["summarize"], "executor")

    def test_kind_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            lanes.set_role_map(1, {"embed": "executor"})
        with self.assertRaises(ValueError):
            lanes.set_role_map(1, {"vision": "executor"})
        self.assertIsNone(lanes.validate_mapping("summarize", "vision", 1))  # image models can chat

    def test_local_only_jobs_refuse_cloud(self):
        key = self.add_cloud()
        lanes.save_user_lane(1, "c1", {"kind": "chat", "cloud": key})
        with self.assertRaises(ValueError):
            lanes.set_role_map(1, {"input_guard": "c1"})
        # even when the executor itself is cloud-bound, policy checks stay local
        cloud.set_lanes(1, {"executor": key, "routing_mode": "custom"})
        self.assertFalse(any(t.is_cloud for t in lanes.targets("input_guard", 1)))
        self.assertTrue(lanes.targets("commit_msg", 1)[0].is_cloud)

    def test_force_local_skips_cloud(self):
        key = self.add_cloud()
        lanes.save_user_lane(1, "c1", {"kind": "chat", "cloud": key})
        lanes.set_role_map(1, {"summarize": "c1"})
        route = lanes.targets("summarize", 1, force_local=True)
        self.assertTrue(route)
        self.assertFalse(any(t.is_cloud for t in route))

    def test_fallback_cycle_guarded(self):
        key = self.add_cloud()
        lanes.save_user_lane(1, "a1", {"kind": "chat", "cloud": key})
        lanes.save_user_lane(1, "b1", {"kind": "chat", "cloud": key, "fallback": "a1"})
        with self.assertRaises(ValueError):
            lanes.save_user_lane(1, "a1", {"fallback": "b1"})
        with self.assertRaises(ValueError):
            lanes.save_user_lane(1, "a1", {"fallback": "a1"})

    def test_builtin_lane_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            lanes.delete_local_lane("executor")

    def test_delete_moves_jobs(self):
        key = self.add_cloud()
        lanes.save_user_lane(1, "c1", {"kind": "chat", "cloud": key})
        lanes.set_role_map(1, {"summarize": "c1", "commit_msg": "c1"})
        lanes.delete_user_lane(1, "c1", move_jobs_to="main")
        rm = lanes.role_map(1)
        self.assertEqual(rm["summarize"], "main")
        self.assertEqual(rm["commit_msg"], "main")

    def test_post_chat_walks_fallback_chain(self):
        self.insts["executor"].client.fail = True
        data, used = asyncio.run(lanes.post_chat("summarize", {"messages": []}, 1))
        self.assertEqual(used.lane, "main")
        self.assertEqual(lanes.message_text(data), "ok")

    def test_bad_lane_names(self):
        key = self.add_cloud()
        for bad in ("Main", "x", "1abc", "a b", "executor"):
            with self.assertRaises(ValueError, msg=bad):
                lanes.save_user_lane(1, bad, {"kind": "chat", "cloud": key})

    def test_cloud_lane_needs_a_real_cloud_model(self):
        with self.assertRaises(ValueError):
            lanes.save_user_lane(1, "c1", {"kind": "chat", "cloud": "nope/none"})
        key = self.add_cloud()
        with self.assertRaises(ValueError):
            lanes.save_user_lane(1, "c2", {"kind": "embed", "cloud": key})

    def test_role_map_file_has_no_keys(self):
        key = self.add_cloud()
        lanes.save_user_lane(1, "c1", {"kind": "chat", "cloud": key})
        on_disk = (Path(self.tmp.name) / "providers" / "user_1.json").read_text(encoding="utf-8")
        self.assertNotIn("[PLACEHOLDER_KEY]", on_disk)


class VerifierTests(LaneTestBase):
    def _run(self, gen):
        async def go():
            return [x async for x in gen]
        return asyncio.run(go())

    def test_off_makes_no_call(self):
        self.assertEqual(verifier.effective_mode("chat", "x" * 500, 1, None), "off")
        self.assertEqual(verifier.effective_mode("chat", "short", 1, "gate"), "off")   # min_length
        self.assertEqual(verifier.effective_mode("chat", "x" * 500, 1, "badge"), "badge")

    def test_pass_passes_through(self):
        self.main_client.reply = '{"verdict": "PASS", "issues": [], "confidence": 0.9}'
        out = self._run(verifier.check_and_fix("q", "answer", [], self.main_client, "gate", 1))
        self.assertEqual(out[-1][0], "verify_result")
        self.assertEqual(out[-1][1]["verdict"], "pass")
        self.assertEqual(len(out), 1)

    def test_gate_fail_revises_once(self):
        replies = iter(['{"verdict": "FAIL", "issues": [{"severity": "major", "text": "wrong year"}]}',
                        "fixed answer",
                        '{"verdict": "PASS", "issues": []}'])
        self.main_client.reply = lambda payload: next(replies)
        out = self._run(verifier.check_and_fix("q", "draft", [], self.main_client, "gate", 1))
        self.assertEqual(out[0], ("revised", "fixed answer"))
        res = out[-1][1]
        self.assertEqual(res["verdict"], "pass")
        self.assertEqual(res["fixed"], 1)
        self.assertEqual(res["original"], "draft")

    def test_badge_never_revises(self):
        self.main_client.reply = '{"verdict": "FAIL", "issues": [{"severity": "major", "text": "x"}]}'
        out = self._run(verifier.check_and_fix("q", "draft", [], self.main_client, "badge", 1))
        self.assertEqual([e for e, _ in out], ["verify_result"])
        self.assertEqual(out[0][1]["verdict"], "fail")

    def test_unparseable_is_unverified(self):
        self.main_client.reply = "looks fine to me"
        res = asyncio.run(verifier.verify_answer("q", "draft", "", 1))
        self.assertEqual(res["verdict"], "unverified")

    def test_checker_down_is_unverified(self):
        self.main_client.fail = True
        self.insts["executor"].client.fail = True
        res = asyncio.run(verifier.verify_answer("q", "draft", "", 1))
        self.assertEqual(res["verdict"], "unverified")

    def test_card_number_always_flagged(self):
        self.main_client.reply = '{"verdict": "PASS", "issues": []}'
        res = asyncio.run(verifier.verify_answer("q", "card 4111 1111 1111 1111 ok", "", 1))
        self.assertEqual(res["verdict"], "fail")
        self.assertIn("card number", res["issues"][0]["text"])


class LegacyKeyFileTests(LaneTestBase):
    def test_migrated_file_keys_moved_and_file_removed(self):
        pdir = Path(self.tmp.name) / "providers"
        pdir.mkdir()
        (pdir / "user_1.json").write_text(json.dumps({"provider": {
            "a": {"options": {"baseURL": "https://a", "apiKeyRef": "keyring"}},
            "b": {"options": {"baseURL": "https://b", "apiKeyRef": "keyring"}}}}), encoding="utf-8")
        self.vault[cloud._key_id(1, "a")] = "[PLACEHOLDER_A_CURRENT]"
        legacy = Path(self.tmp.name) / "providers.json.migrated"
        legacy.write_text(json.dumps({"provider": {
            "a": {"options": {"apiKey": "[PLACEHOLDER_A_OLD]"}},
            "b": {"options": {"apiKey": "[PLACEHOLDER_B]"}},
            "gone": {"options": {"apiKey": "[PLACEHOLDER_GONE]"}}}}), encoding="utf-8")
        r = cloud.import_legacy_file(legacy, 1)
        self.assertEqual(r["imported"], 1)
        self.assertFalse(legacy.exists())
        self.assertEqual(self.vault[cloud._key_id(1, "a")], "[PLACEHOLDER_A_CURRENT]")   # not overwritten
        self.assertEqual(self.vault[cloud._key_id(1, "b")], "[PLACEHOLDER_B]")
        self.assertNotIn(cloud._key_id(1, "gone"), self.vault)                        # deleted provider stays gone
        self.assertNotIn("PLACEHOLDER", (pdir / "user_1.json").read_text(encoding="utf-8"))

    def test_keychain_failure_keeps_file(self):
        pdir = Path(self.tmp.name) / "providers"
        pdir.mkdir()
        (pdir / "user_1.json").write_text(json.dumps({"provider": {"b": {"options": {}}}}), encoding="utf-8")
        legacy = Path(self.tmp.name) / "providers.json.migrated"
        legacy.write_text(json.dumps({"provider": {"b": {"options": {"apiKey": "[PLACEHOLDER_B]"}}}}),
                          encoding="utf-8")

        def boom(k, v):
            raise RuntimeError("no keychain")
        with mock.patch.object(credentials, "set_token", boom):
            with self.assertRaises(RuntimeError):
                cloud.import_legacy_file(legacy, 1)
        self.assertTrue(legacy.exists())


class LaneRouteTests(LaneTestBase):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import deps
        from routes import lanes as lanes_routes
        self.admin = False
        app = FastAPI()
        app.include_router(lanes_routes.router)
        app.dependency_overrides[deps.get_current_user] = lambda: mock.Mock(id=1, is_super_admin=False)
        self.more = [mock.patch.object(lanes_routes, "user_has_permission", lambda u, k: self.admin),
                     mock.patch.object(lanes_routes, "audit_log", lambda *a, **k: None)]
        for p in self.more:
            p.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for p in self.more:
            p.stop()
        super().tearDown()

    def test_view_is_plain_language(self):
        d = self.client.get("/control/lanes").json()
        names = {l["name"]: l for l in d["lanes"]}
        self.assertEqual(names["executor"]["label"], "Fast helper")
        self.assertIn("status", names["executor"])
        self.assertFalse(d["can_edit_local"])
        self.assertEqual({j["job"] for j in d["jobs"]}, set(lanes.JOBS))

    def test_non_admin_cannot_add_local_model(self):
        r = self.client.post("/control/lanes", json={"name": "coder", "backend": "local", "model": "x.gguf"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("admin", r.json()["error"])

    def test_user_adds_cloud_model_and_maps_jobs(self):
        key = self.add_cloud()
        r = self.client.post("/control/lanes", json={"name": "cloudy", "backend": "cloud", "kind": "chat",
                                                     "cloud": key, "label": "Cloudy", "jobs": ["summarize"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["role_map"]["summarize"], "cloudy")
        bad = self.client.post("/control/lanes/roles", json={"map": {"embed": "cloudy"}})
        self.assertEqual(bad.status_code, 400)
        r = self.client.delete("/control/lanes", params={"name": "cloudy", "move_to": "main"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["role_map"]["summarize"], "main")

    def test_only_admin_sets_default_for_everyone(self):
        r = self.client.post("/control/lanes/roles", json={"map": {"summarize": "main"}, "as_default": True})
        self.assertEqual(r.status_code, 403)

    def test_local_port_conflict_and_bad_gpu(self):
        self.admin = True
        with mock.patch("core.profiles.in_helper_dir", lambda p: True),              mock.patch("pathlib.Path.exists", lambda self: True),              mock.patch("core.config.update_app_config", lambda f, path=None: None),              mock.patch.object(small_model.small_models, "reconfigure", lambda n, c: None):
            r = self.client.post("/control/lanes", json={"name": "coder", "backend": "local",
                                                         "model": "c.gguf", "port": 8091})
            self.assertEqual(r.status_code, 400)
            self.assertIn("already used", r.json()["error"])
            r = self.client.post("/control/lanes", json={"name": "coder", "backend": "local",
                                                         "model": "c.gguf", "port": 8099, "gpu": 42})
            self.assertEqual(r.status_code, 400)
            self.assertIn("GPU", r.json()["error"])

    def test_verification_settings_saved_per_user(self):
        r = self.client.post("/control/verification", json={"mode": "gate"})
        self.assertEqual(r.json()["verification"]["mode"], "gate")
        self.assertEqual(verifier.settings(2)["mode"], "off")
        self.assertEqual(self.client.post("/control/verification", json={"mode": "loud"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
