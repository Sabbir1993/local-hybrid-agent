"""Images / videos / speech to text: lanes (core/lanes.py), engines (core/media.py),
whisper launch (core/small_model.py), routes (routes/media.py, routes/lanes.py)."""

import asyncio
import base64
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import cloud, lanes, media, profiles, small_model  # noqa: E402
from test_lanes import LaneTestBase  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
TEST_PAN = "4111 1111 1111 1111"      # a standard test card number, not a real one


def _wav(n=1600):
    return (b"RIFF" + struct.pack("<I", 36 + n * 2) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data" + struct.pack("<I", n * 2)
            + bytes(n * 2))


def run(coro):
    return asyncio.run(coro)


class MediaBase(LaneTestBase):
    def setUp(self):
        super().setUp()
        self.cfg_store = {}
        self.user = mock.Mock(id=1, username="u1", role_names=["user"])
        self.more = [
            mock.patch("core.config.update_app_config", lambda f, path=None: f(self.cfg_store)),
            mock.patch.dict(small_model.APP_CONFIG, {"media": {"allow_cloud_audio": False,
                                                               "limits": {"image_per_day": 50, "video_per_day": 5},
                                                               "max_audio_mb": 25}}),
            mock.patch.object(media, "_POLL_S", 0.0),
            mock.patch("core.audit.audit_log", lambda *a, **k: None),
            mock.patch.object(media, "used_today", lambda uid, kind: 0),
        ]
        for p in self.more:
            p.start()
        self.saved = []
        self._save = mock.patch.object(media, "save_file", self._fake_save)
        self._save.start()
        # a temp models dir with the real layout:
        # Models/Qwen/main.gguf, Models/orchestrator/{helper,mmproj,whisper},
        # Models/orchestrator/image-models/{diffusion, encoder, vae}
        self.mtmp = tempfile.TemporaryDirectory()
        root = Path(self.mtmp.name)
        self.root = root
        for rel in ("Qwen/main.gguf", "orchestrator/Spark-4B.gguf", "orchestrator/mmproj-f16.gguf",
                    "orchestrator/voice-models/ggml-large-v3-turbo-q5_0.bin",
                    "orchestrator/image-models/z_image_turbo-Q4_K_M.gguf",
                    "orchestrator/image-models/Qwen3-4B-Q8_0.gguf",
                    "orchestrator/image-models/ae.safetensors",
                    "orchestrator/video-models/wan2.2-5b.gguf"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x00" * 2048)
        self.mp = [mock.patch.object(profiles, "MODELS_DIR", root),
                   mock.patch.dict(profiles.MEDIA_DIRS_CFG, {}, clear=True),
                   mock.patch.dict(small_model.APP_CONFIG, {"models_dir": str(root)})]
        for p in self.mp:
            p.start()

    def tearDown(self):
        for p in reversed(self.mp):
            p.stop()
        self.mtmp.cleanup()
        self._save.stop()
        for p in reversed(self.more):
            p.stop()
        super().tearDown()

    def _fake_save(self, uid, data, prompt, want):
        kind = media.sniff(data)
        if kind is None:
            raise media.MediaError("The reply wasn't an image file.")
        self.saved.append(data)
        return {"path": f"generated/x{len(self.saved)}{kind[1]}", "name": f"x{kind[1]}", "mime": kind[0],
                "size": len(data)}

    def add_sd(self, name="pc-images", **extra):
        """A local image model (stable-diffusion.cpp) with its files in image-models/."""
        port = extra.pop("port", 8097)
        data = {"kind": "image_gen", "engine": "sdcpp", "port": port, "gpu": 1,
                "diffusion_model": "orchestrator/image-models/z_image_turbo-Q4_K_M.gguf",
                "llm": "orchestrator/image-models/Qwen3-4B-Q8_0.gguf",
                "vae": "orchestrator/image-models/ae.safetensors", **extra}
        lanes.save_local_lane(name, data)
        return small_model.small_models.instances[name]

    def serve(self, inst, handler):
        """Mark the sd-server as loaded and answer its requests with `handler`."""
        inst.process = mock.Mock(poll=lambda: None)
        inst.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{inst.port}",
                                        transport=httpx.MockTransport(handler))

    @staticmethod
    def sd_png(req):
        if req.method == "POST":
            return httpx.Response(202, json={"id": "j1"})
        return httpx.Response(200, json={"status": "completed", "result": {"images": [
            {"b64_json": base64.b64encode(PNG).decode()}]}})

    def sd_installed(self):
        return mock.patch.object(small_model, "find_sd_server", lambda: Path("C:/sd/sd-server.exe"))


class MediaRoutingTests(MediaBase):
    def test_media_jobs_are_off_until_a_model_is_added(self):
        for job in ("image_gen", "video_gen", "transcribe"):
            self.assertIsNone(lanes.role_map(1)[job])
            self.assertEqual(lanes.targets(job, 1), [])
        st = media.status(1)
        self.assertFalse(st["image"]["ready"])
        with self.assertRaises(media.MediaError) as cm:
            run(media.generate("image", self.user, "a cat"))
        self.assertIn("isn't set up", str(cm.exception))

    def test_local_image_lane_is_routed(self):
        self.add_sd()
        lanes.set_role_map(1, {"image_gen": "pc-images"})
        route = lanes.targets("image_gen", 1)
        self.assertEqual([t.lane for t in route], ["pc-images"])   # never the text main model
        with self.sd_installed():
            self.assertTrue(media.status(1)["image"]["ready"])

    def test_only_the_local_engine_is_accepted(self):
        # the old "own service" (ComfyUI) engine is gone
        with self.assertRaises(ValueError):
            lanes.save_local_lane("svc", {"kind": "image_gen", "engine": "endpoint",
                                          "url": "http://127.0.0.1:8188/generate"})
        with self.assertRaises(ValueError):      # no model file picked
            lanes.save_local_lane("svc", {"kind": "image_gen", "url": "http://127.0.0.1:8188/generate"})
        # a leftover config entry becomes an sd.cpp lane with no files, not a web call
        cfg = {"kind": "image_gen", "engine": "endpoint", "url": "http://127.0.0.1:8188/generate"}
        self.assertEqual(small_model.lane_engine_of("old", cfg), "sdcpp")
        self.assertIsInstance(small_model.make_instance("old", cfg), small_model.SdCppInstance)
        self.assertFalse(hasattr(small_model, "EndpointLane"))
        self.assertFalse(hasattr(media, "endpoint_generate"))

    def test_kind_mismatch_is_rejected(self):
        self.add_sd()
        with self.assertRaises(ValueError):
            lanes.set_role_map(1, {"video_gen": "pc-images"})
        with self.assertRaises(ValueError):
            lanes.set_role_map(1, {"summarize": "pc-images"})
        with self.assertRaises(ValueError):
            lanes.set_role_map(1, {"image_gen": "executor"})

    def test_backup_must_be_same_kind(self):
        self.add_sd()
        with self.assertRaises(ValueError) as cm:
            self.add_sd("pc-images2", port=8098, fallback="executor")
        self.assertIn("backup", str(cm.exception))
        self.add_sd("pc-images2", port=8098, fallback="pc-images")

    def test_cloud_speech_blocked_until_admin_allows(self):
        key = self.add_cloud(model="whisper-1")
        with self.assertRaises(ValueError):
            lanes.save_user_lane(1, "cloud-stt", {"kind": "stt", "cloud": key})
        small_model.APP_CONFIG["media"]["allow_cloud_audio"] = True
        lanes.save_user_lane(1, "cloud-stt", {"kind": "stt", "cloud": key})
        lanes.set_role_map(1, {"transcribe": "cloud-stt"})
        self.assertTrue(lanes.targets("transcribe", 1)[0].is_cloud)
        # switched off again: the mapping stays but nothing goes to the cloud
        small_model.APP_CONFIG["media"]["allow_cloud_audio"] = False
        self.assertEqual([t for t in lanes.targets("transcribe", 1) if t.is_cloud], [])
        self.assertIsNotNone(lanes.validate_mapping("transcribe", "cloud-stt", 1))

class LocalImageTests(MediaBase):
    def setUp(self):
        super().setUp()
        self.inst = self.add_sd()
        lanes.set_role_map(1, {"image_gen": "pc-images"})

    def test_card_number_is_blocked_before_sending(self):
        calls = []
        self.serve(self.inst, lambda req: calls.append(req) or self.sd_png(req))
        with self.sd_installed(), self.assertRaises(media.MediaError) as cm:
            run(media.generate("image", self.user, f"receipt for card {TEST_PAN}"))
        self.assertIn("card", str(cm.exception).lower())
        self.assertEqual(calls, [])

    def test_markdown_links_the_saved_file(self):
        self.serve(self.inst, self.sd_png)
        with self.sd_installed():
            res = run(media.generate("image", self.user, 'a "quoted" cat', {"aspect": "wide"}))
        self.assertIn("![a \"quoted\" cat](/agent/raw?path=generated", res["markdown"])
        self.assertNotIn("[DOWNLOAD:", res["markdown"])     # one preview; the viewer downloads
        self.assertFalse(res["cloud"])
        self.assertEqual(self.saved, [PNG])

    def test_falls_back_to_the_local_model(self):
        key = self.add_cloud(model="gpt-image-1")
        # the user's cloud lane goes first, with the shared local model as its backup
        lanes.save_user_lane(1, "cloud-img", {"kind": "image_gen", "cloud": key, "fallback": "pc-images"})
        lanes.set_role_map(1, {"image_gen": "cloud-img"})
        cloud_calls = []

        def cloud_h(req):
            cloud_calls.append(json.loads(req.content))
            return httpx.Response(500, json={"error": "down"})
        cc = httpx.AsyncClient(transport=httpx.MockTransport(cloud_h))
        self.serve(self.inst, self.sd_png)
        with mock.patch.object(cloud, "_client_for", lambda cm: cc), self.sd_installed():
            res = run(media.generate("image", self.user, "x"))
        self.assertEqual(cloud_calls[0]["model"], "gpt-image-1")
        self.assertEqual(res["lane"], "pc-images")
        self.assertEqual(res["where"], "this PC")


class CloudAdapterTests(MediaBase):
    def _lane(self, kind, model, base="https://api.example/v1"):
        cloud.save_provider(1, "p", {"base_url": base, "api_key": "[PLACEHOLDER_KEY]", "models": [{"id": model}]})
        lanes.save_user_lane(1, "cm1", {"kind": kind, "cloud": f"p/{model}"})
        lanes.set_role_map(1, {{"image_gen": "image_gen", "video_gen": "video_gen", "stt": "transcribe"}[kind]: "cm1"})

    def _client(self, handler):
        c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return mock.patch.object(cloud, "_client_for", lambda cm: c)

    def test_openai_image(self):
        self._lane("image_gen", "gpt-image-1")
        seen = []

        def h(req):
            seen.append((str(req.url), json.loads(req.content), req.headers.get("authorization")))
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        with self._client(h):
            res = run(media.generate("image", self.user, "a boat", {"aspect": "tall"}))
        url, body, auth = seen[0]
        self.assertEqual(url, "https://api.example/v1/images/generations")
        self.assertEqual(body["size"], "1024x1536")
        self.assertEqual(auth, "Bearer [PLACEHOLDER_KEY]")
        self.assertTrue(res["cloud"])

    def test_openai_video_polls_to_completion(self):
        self._lane("video_gen", "sora-2")
        polls = {"n": 0}

        def h(req):
            p = req.url.path
            if req.method == "POST" and p == "/v1/videos":
                self.assertIn(b"sora-2", req.content)      # form fields
                return httpx.Response(200, json={"id": "vid_1", "status": "queued"})
            if p == "/v1/videos/vid_1":
                polls["n"] += 1
                return httpx.Response(200, json={"status": "completed" if polls["n"] > 1 else "in_progress",
                                                 "progress": 50})
            if p == "/v1/videos/vid_1/content":
                return httpx.Response(200, content=MP4)
            return httpx.Response(404)
        progress = []

        async def prog(text, pct):
            progress.append((text, pct))
        with self._client(h):
            res = run(media.generate("video", self.user, "waves", {"seconds": 4}, prog))
        self.assertEqual(res["files"][0]["mime"], "video/mp4")
        self.assertIn("[VIDEO: generated/", res["markdown"])
        self.assertTrue(any(p == 50 for _, p in progress))

    def test_openai_video_failure(self):
        self._lane("video_gen", "sora-2")

        def h(req):
            if req.method == "POST":
                return httpx.Response(200, json={"id": "v"})
            return httpx.Response(200, json={"status": "failed", "error": {"message": "policy"}})
        with self._client(h):
            with self.assertRaises(media.MediaError) as cm:
                run(media.generate("video", self.user, "waves"))
        self.assertIn("policy", str(cm.exception))

    def test_google_imagen(self):
        self._lane("image_gen", "imagen-4.0-generate-001", base="https://generativelanguage.googleapis.com/v1beta/openai")
        seen = []

        def h(req):
            seen.append((str(req.url), req.headers.get("x-goog-api-key"), req.headers.get("authorization")))
            return httpx.Response(200, json={"predictions": [{"bytesBase64Encoded": base64.b64encode(PNG).decode()}]})
        with self._client(h):
            run(media.generate("image", self.user, "x"))
        url, gkey, bearer = seen[0]
        self.assertTrue(url.endswith("/v1beta/models/imagen-4.0-generate-001:predict"))
        self.assertEqual(gkey, "[PLACEHOLDER_KEY]")
        self.assertIsNone(bearer)

    def test_google_veo_operation(self):
        self._lane("video_gen", "veo-3.0-generate-001", base="https://generativelanguage.googleapis.com/v1beta/openai")
        state = {"n": 0}

        def h(req):
            u = str(req.url)
            if u.endswith(":predictLongRunning"):
                return httpx.Response(200, json={"name": "models/veo/operations/op1"})
            if u.endswith("/operations/op1"):
                state["n"] += 1
                if state["n"] < 2:
                    return httpx.Response(200, json={"done": False})
                return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {
                    "generatedSamples": [{"video": {"uri": "https://generativelanguage.googleapis.com/v1beta/files/f:download"}}]}}})
            if "files/f" in u:
                return httpx.Response(200, content=MP4)
            return httpx.Response(404)
        with self._client(h):
            res = run(media.generate("video", self.user, "x"))
        self.assertEqual(res["files"][0]["mime"], "video/mp4")

    def test_cloud_prompt_card_number_never_sent(self):
        self._lane("image_gen", "gpt-image-1")
        sent = []
        with self._client(lambda req: sent.append(req) or httpx.Response(200, json={"data": []})):
            with self.assertRaises(media.MediaError):
                run(media.generate("image", self.user, f"card {TEST_PAN}"))
        self.assertEqual(sent, [])

    def test_daily_cloud_limit(self):
        self._lane("image_gen", "gpt-image-1")
        with mock.patch.object(media, "used_today", lambda uid, kind: 50), \
                self._client(lambda req: httpx.Response(200, json={"data": []})):
            with self.assertRaises(media.MediaError) as cm:
                run(media.generate("image", self.user, "x"))
        self.assertIn("Daily limit", str(cm.exception))

    def test_cloud_transcribe_multipart(self):
        small_model.APP_CONFIG["media"]["allow_cloud_audio"] = True
        self._lane("stt", "whisper-1")
        seen = []

        def h(req):
            seen.append((str(req.url), req.headers.get("content-type")))
            return httpx.Response(200, json={"text": " hello "})
        with self._client(h):
            res = run(media.transcribe(self.user, _wav(), "en"))
        self.assertEqual(res["text"], "hello")
        self.assertTrue(seen[0][0].endswith("/v1/audio/transcriptions"))
        self.assertIn("multipart/form-data", seen[0][1])


class WhisperTests(MediaBase):
    def _whisper(self, gpu=1):
        with mock.patch("core.profiles.in_helper_dir", lambda p: True), \
                mock.patch("pathlib.Path.exists", lambda self: True):
            lanes.save_local_lane("stt", {"kind": "stt", "model": "orchestrator/voice-models/ggml-large-v3-turbo-q5_0.bin",
                                          "gpu": gpu, "port": 8095, "language": "bn"})
        return small_model.small_models.instances["stt"]

    def test_gguf_rules_do_not_apply(self):
        with mock.patch("core.profiles.in_helper_dir", lambda p: True), \
                mock.patch("pathlib.Path.exists", lambda self: True):
            with self.assertRaises(ValueError):
                lanes.save_local_lane("stt", {"kind": "stt", "model": "x.gguf", "port": 8095})
            with self.assertRaises(ValueError):
                lanes.save_local_lane("stt", {"kind": "stt", "model": "x.bin", "port": 8095, "language": "../x"})

    def test_launch_command_and_env(self):
        inst = self._whisper(gpu=2)
        self.assertIsInstance(inst, small_model.WhisperInstance)
        with mock.patch.object(small_model, "find_whisper_server", lambda: Path("C:/w/whisper-server.exe")):
            cmd, env = inst._launch_cmd()
        self.assertIn("--inference-path", cmd)
        self.assertEqual(cmd[cmd.index("--port") + 1], "8095")
        self.assertEqual(cmd[cmd.index("--host") + 1], "127.0.0.1")
        self.assertEqual(env["GGML_VK_VISIBLE_DEVICES"], "2")
        self.assertNotIn("-ng", cmd)

    def test_cpu_mode(self):
        inst = self._whisper(gpu=-1)
        self.assertEqual(inst.gpu, -1)
        with mock.patch.object(small_model, "find_whisper_server", lambda: Path("C:/w/whisper-server.exe")):
            cmd, env = inst._launch_cmd()
        self.assertIn("-ng", cmd)
        import os
        self.assertEqual(env.get("GGML_VK_VISIBLE_DEVICES"), os.environ.get("GGML_VK_VISIBLE_DEVICES"))

    def test_transcribe_through_whisper(self):
        inst = self._whisper()
        lanes.set_role_map(1, {"transcribe": "stt"})
        seen = {}

        def h(req):
            seen["ct"] = req.headers.get("content-type")
            seen["body"] = req.content
            return httpx.Response(200, json={"text": "আমি ভালো আছি [BLANK_AUDIO]"})
        inst.client = httpx.AsyncClient(base_url="http://127.0.0.1:8095", transport=httpx.MockTransport(h))

        async def _up():
            return None
        with mock.patch.object(small_model, "find_whisper_server", lambda: Path("C:/w/whisper-server.exe")), \
                mock.patch("pathlib.Path.exists", lambda self: True), \
                mock.patch.object(inst, "ensure_loaded", _up):
            res = run(media.transcribe(self.user, _wav(), None))
        self.assertEqual(res["text"], "আমি ভালো আছি")
        self.assertIn(b'name="language"\r\n\r\nbn', seen["body"])   # the lane's default language
        self.assertIn("multipart/form-data", seen["ct"])

    def test_wav_checks(self):
        with self.assertRaises(media.MediaError):
            media.check_wav(b"not audio")
        small_model.APP_CONFIG["media"]["max_audio_mb"] = 1
        media.check_wav(_wav())
        with self.assertRaises(media.MediaError):
            media.check_wav(_wav(700_000))


class MediaRouteTests(MediaBase):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import deps
        from routes import lanes as lanes_routes, media as media_routes
        self.admin = False
        app = FastAPI()
        app.include_router(lanes_routes.router)
        app.include_router(media_routes.router)
        app.dependency_overrides[deps.get_current_user] = lambda: self.user
        self.rp = [mock.patch.object(lanes_routes, "user_has_permission", lambda u, k: self.admin),
                   mock.patch.object(lanes_routes, "audit_log", lambda *a, **k: None),
                   mock.patch.object(media_routes, "audit_log", lambda *a, **k: None)]
        for p in self.rp:
            p.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for p in self.rp:
            p.stop()
        super().tearDown()

    def test_non_admin_cannot_add_a_local_image_model(self):
        r = self.client.post("/control/lanes", json={
            "name": "pc-images", "backend": "local", "kind": "image_gen", "engine": "sdcpp",
            "diffusion_model": "orchestrator/image-models/z_image_turbo-Q4_K_M.gguf"})
        self.assertEqual(r.status_code, 403)

    def test_status_and_not_set_up(self):
        d = self.client.get("/media/status").json()
        self.assertFalse(d["image"]["ready"])
        r = self.client.post("/media/generate", json={"kind": "image", "prompt": "a cat"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["not_setup"])

    def test_cloud_video_needs_confirm(self):
        cloud.save_provider(1, "p", {"base_url": "https://api.example/v1", "api_key": "[PLACEHOLDER_KEY]",
                                     "models": [{"id": "sora-2"}]})
        lanes.save_user_lane(1, "vid", {"kind": "video_gen", "cloud": "p/sora-2"})
        lanes.set_role_map(1, {"video_gen": "vid"})
        r = self.client.post("/media/generate", json={"kind": "video", "prompt": "waves"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["needs_confirm"])

    def test_transcribe_size_cap_and_format(self):
        small_model.APP_CONFIG["media"]["max_audio_mb"] = 1
        big = _wav(700_000)
        r = self.client.post("/media/transcribe", files={"file": ("a.wav", big, "audio/wav")})
        self.assertEqual(r.status_code, 413)
        r = self.client.post("/media/transcribe", files={"file": ("a.wav", b"RIFFxxxxWAVE", "audio/wav")})
        self.assertIn(r.status_code, (400, 409))

    def test_media_settings_admin_only(self):
        r = self.client.post("/control/media-settings", json={"allow_cloud_audio": True})
        self.assertEqual(r.status_code, 403)
        self.admin = True
        r = self.client.post("/control/media-settings", json={"allow_cloud_audio": True, "video_per_day": 2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(small_model.APP_CONFIG["media"]["allow_cloud_audio"])
        self.assertEqual(self.cfg_store["media"]["limits"]["video_per_day"], 2)

    def test_job_is_private_to_its_user(self):
        self.admin = True
        inst = self.add_sd()
        inst.process = mock.Mock(poll=lambda: None)          # loaded
        lanes.set_role_map(1, {"image_gen": "pc-images"})

        async def fake_gen(kind, user, prompt, opts, progress=None, lane=None, enforce_quota=True):
            return {"files": [], "markdown": "", "model": "Z-Image", "source": "local", "where": "this PC",
                    "ms": 1, "lane": "pc-images", "cloud": False}
        with mock.patch.object(media, "generate", fake_gen), self.sd_installed():
            r = self.client.post("/media/generate", json={"kind": "image", "prompt": "a cat"})
            self.assertEqual(r.status_code, 200, r.text)
            jid = r.json()["job_id"]
            body = self.client.get(f"/media/jobs/{jid}").text
            self.assertIn("event: done", body)
            other = mock.Mock(id=2)
            self.assertIsNone(media.get_job(jid, other.id))


if __name__ == "__main__":
    unittest.main()
