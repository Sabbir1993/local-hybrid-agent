"""Helper models only from Models/orchestrator; image models on this PC through
stable-diffusion.cpp (core/small_model.SdCppInstance, core/media.sdcpp_generate),
loaded by hand only (routes/lanes.py /load), and offered to plain chat only when local.

Run: python -m unittest tests.test_sdcpp -v
"""

import asyncio
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import lanes, media, media_tools, profiles, small_model, vram  # noqa: E402
from test_media import MediaBase, PNG, run  # noqa: E402

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64


# the temp models dir (self.root) and add_sd() live in test_media.MediaBase
ModelsDirBase = MediaBase


class OrchestratorOnlyTests(ModelsDirBase):
    def test_file_list_is_orchestrator_only(self):
        f = profiles.discover_helper_files()
        names = [m["name"] for m in f["models"]]
        self.assertEqual(names, ["Spark-4B.gguf"])                 # not the main model, not image models
        self.assertEqual([m["name"] for m in f["mmproj"]], ["mmproj-f16.gguf"])
        self.assertEqual([m["name"] for m in f["whisper"]], ["ggml-large-v3-turbo-q5_0.bin"])
        self.assertEqual(sorted(m["name"] for m in f["image"]),
                         ["Qwen3-4B-Q8_0.gguf", "ae.safetensors", "z_image_turbo-Q4_K_M.gguf"])
        self.assertEqual([m["name"] for m in f["video"]], ["wan2.2-5b.gguf"])
        self.assertEqual(f["models"][0]["path"], "orchestrator/Spark-4B.gguf")

    def test_media_folders_from_app_json(self):
        # app.json "media_dirs" moves a folder (absolute, or relative to models_dir)
        voice = self.root / "my-voices"
        voice.mkdir()
        (voice / "ggml-base.bin").write_bytes(b"\x00" * 16)
        with mock.patch.dict(profiles.MEDIA_DIRS_CFG, {"stt": str(voice), "image_gen": "orchestrator/pics"}):
            self.assertEqual(profiles.media_root("stt"), voice)
            self.assertEqual(profiles.media_root("image_gen"), self.root / "orchestrator/pics")
            self.assertEqual(profiles.media_root("video_gen"), self.root / "orchestrator/video-models")
            f = profiles.discover_helper_files()
            self.assertEqual([m["name"] for m in f["whisper"]], ["ggml-base.bin"])
            cur = lanes.save_local_lane("stt", {"kind": "stt", "port": 8095, "model": "my-voices/ggml-base.bin"})
            self.assertEqual(cur["model"], "my-voices/ggml-base.bin")
            with self.assertRaises(ValueError):       # the default folder no longer counts
                lanes.save_local_lane("stt", {"kind": "stt", "port": 8095,
                                              "model": "orchestrator/voice-models/ggml-large-v3-turbo-q5_0.bin"})
        with mock.patch.object(profiles, "CONFIG_FILE", self.root / "app.json"):
            (self.root / "app.json").write_text('{"media_dirs": {"voice_models": "V:/x", "bogus": "y", "image_models": 5}}')
            self.assertEqual(profiles._load_media_dirs_config(), {"stt": "V:/x"})

    def test_helper_lane_must_be_in_orchestrator(self):
        with self.assertRaises(ValueError) as e:
            lanes.save_local_lane("coder", {"kind": "chat", "model": "Qwen/main.gguf", "port": 8099})
        self.assertIn("orchestrator", str(e.exception))
        with self.assertRaises(ValueError):          # image models aren't text helpers
            lanes.save_local_lane("coder", {"kind": "chat", "port": 8099,
                                            "model": "orchestrator/image-models/z_image_turbo-Q4_K_M.gguf"})
        cur = lanes.save_local_lane("coder", {"kind": "chat", "model": "orchestrator/Spark-4B.gguf", "port": 8099})
        self.assertEqual(cur["model"], "orchestrator/Spark-4B.gguf")

    def test_whisper_model_must_be_in_orchestrator(self):
        (self.root / "whisper").mkdir()
        (self.root / "whisper" / "ggml-small.bin").write_bytes(b"\x00")
        with self.assertRaises(ValueError):
            lanes.save_local_lane("stt", {"kind": "stt", "model": "whisper/ggml-small.bin", "port": 8095})
        (self.root / "orchestrator" / "ggml-old.bin").write_bytes(b"\x00")
        with self.assertRaises(ValueError) as e:     # only voice-models, not the orchestrator root
            lanes.save_local_lane("stt", {"kind": "stt", "model": "orchestrator/ggml-old.bin", "port": 8095})
        self.assertIn("voice-models", str(e.exception))
        cur = lanes.save_local_lane("stt", {"kind": "stt", "port": 8095,
                                            "model": "orchestrator/voice-models/ggml-large-v3-turbo-q5_0.bin"})
        self.assertEqual(cur["engine"], "whisper")
        with self.assertRaises(ValueError):          # and a voice model isn't a text helper
            lanes.save_local_lane("coder", {"kind": "chat", "port": 8099,
                                            "model": "orchestrator/voice-models/ggml-large-v3-turbo-q5_0.bin"})


class SdCppLaneTests(ModelsDirBase):
    def test_save_validates_folder_and_builds_instance(self):
        with self.assertRaises(ValueError) as e:
            self.add_sd(diffusion_model="orchestrator/Spark-4B.gguf")
        self.assertIn("image-models", str(e.exception))
        with self.assertRaises(ValueError):      # video models are for video lanes
            self.add_sd(diffusion_model="orchestrator/video-models/wan2.2-5b.gguf")
        with self.assertRaises(ValueError):
            self.add_sd(sampler="euler; rm -rf")
        inst = self.add_sd(steps=8, cfg_scale=1.0, sampler="euler")
        self.assertIsInstance(inst, small_model.SdCppInstance)
        cfg = small_model.APP_CONFIG["small_models"]["pc-images"]
        self.assertEqual(cfg["engine"], "sdcpp")
        self.assertEqual(small_model.lane_engine_of("pc-images", cfg), "sdcpp")
        self.assertNotIn("url", cfg)
        self.assertEqual(lanes.registry()["pc-images"]["engine"], "sdcpp")

    def test_launch_command_env_offload_cpu(self):
        inst = self.add_sd(offload_to_cpu=True)
        with mock.patch.object(small_model, "find_sd_server", lambda: Path("C:/sd/sd-server.exe")):
            cmd, env = inst._launch_cmd()
        self.assertEqual(cmd[0], str(Path("C:/sd/sd-server.exe")))
        for flag in ("--diffusion-model", "--llm", "--vae", "--offload-to-cpu", "--diffusion-fa"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--listen-ip") + 1], "127.0.0.1")
        self.assertEqual(cmd[cmd.index("--listen-port") + 1], "8097")
        self.assertEqual(env["GGML_VK_VISIBLE_DEVICES"], "1")
        cpu = self.add_sd(name="cpu-images", port=8098, gpu=-1)
        with mock.patch.object(small_model, "find_sd_server", lambda: Path("C:/sd/sd-server.exe")):
            _, env = cpu._launch_cmd()
        self.assertEqual(env["GGML_VK_VISIBLE_DEVICES"], "")
        self.assertEqual(cpu.est_bytes(), 0)

    def test_never_loads_by_itself_and_never_idles_out(self):
        inst = self.add_sd()
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(small_model, "find_sd_server", lambda: Path("C:/sd/sd-server.exe")):
            with self.assertRaises(RuntimeError):
                run(inst.ensure_loaded())
            lanes.set_role_map(1, {"image_gen": "pc-images"})
            with self.assertRaises(media.NotLoadedError) as e:
                run(media.generate("image", self.user, "a red bus"))
            self.assertEqual(e.exception.lane, "pc-images")
            popen.assert_not_called()
        inst.process = mock.Mock(poll=lambda: None)
        inst.last_used = 0
        self.assertFalse(run(inst.unload_if_idle()))
        self.assertEqual(inst.state(), "loaded")

    def test_preflight_refuses_when_it_wont_fit(self):
        inst = self.add_sd()
        with mock.patch.object(inst, "est_bytes", lambda: 13 * 1024 ** 3), \
                mock.patch.object(vram, "query_devices", lambda *a, **k: [{"index": 1, "free_b": 9 * 1024 ** 3}]):
            with self.assertRaises(vram.PreflightError) as e:
                async def go():
                    await inst._preflight(asyncio.get_running_loop())
                run(go())
        self.assertIn("Keep weights in RAM", str(e.exception))

    def test_status_says_needs_load(self):
        self.add_sd()
        lanes.set_role_map(1, {"image_gen": "pc-images"})
        with mock.patch.object(small_model, "find_sd_server", lambda: Path("C:/sd/sd-server.exe")):
            st = media.status(1)["image"]
            self.assertTrue(st["ready"])
            self.assertTrue(st["needs_load"])
            self.assertEqual(st["state"], "not_loaded")
            row = next(r for r in lanes.public_view(1, True)["lanes"] if r["name"] == "pc-images")
        self.assertIn("press Load", row["status"]["text"])


class SdCppExitReasonTests(ModelsDirBase):
    def test_missing_vae_is_explained(self):
        inst = self.add_sd()
        inst._log_tail.extend([
            "[ERROR  ] model_manager.cpp:761  - VAE tensor 'first_stage_model.encoder.middle.2.residual.6.weight' "
            "not in model metadata",
            "[ERROR  ] diffusion_engine.cpp:1264 - model metadata validation failed"])
        self.assertIn("VAE", inst._exit_reason())
        from routes import lanes as lanes_routes
        msg = lanes_routes._plain_error(RuntimeError(f"[pc-images] failed to start (exited code 1 - {inst._exit_reason()})"))
        self.assertTrue(msg.startswith("The model server stopped: the VAE file"), msg)

    def test_other_errors_show_the_last_error_line(self):
        inst = self.add_sd()
        inst._log_tail.extend(["loading...", "[ERROR  ] something odd happened", "bye"])
        self.assertEqual(inst._exit_reason(), "[ERROR ] something odd happened")
        inst._log_tail.clear()
        self.assertIsNone(inst._exit_reason())


class SdCppAdapterTests(ModelsDirBase):
    def setUp(self):
        super().setUp()
        self.inst = self.add_sd(steps=8, cfg_scale=1.0, sampler="euler")
        self.inst.process = mock.Mock(poll=lambda: None)      # "loaded"
        self.seen = []

    def _serve(self, handler):
        self.inst.client = httpx.AsyncClient(base_url="http://127.0.0.1:8097",
                                             transport=httpx.MockTransport(handler))

    def test_submit_poll_completed(self):
        polls = iter([{"status": "queued", "queue_position": 1}, {"status": "generating"},
                      {"status": "completed", "result": {"images": [{"index": 0, "b64_json": base64.b64encode(PNG).decode()}]}}])

        def h(req):
            self.seen.append((req.method, req.url.path, req.content))
            if req.method == "POST":
                return httpx.Response(202, json={"id": "job_1", "status": "queued", "poll_url": "/sdcpp/v1/jobs/job_1"})
            return httpx.Response(200, json=next(polls))
        self._serve(h)
        texts = []

        async def prog(t, pct):
            texts.append(t)
        blobs = run(media.sdcpp_generate(self.inst, "image", "a red bus", {"width": 1000, "height": 700}, prog, poll_s=0))
        self.assertEqual(blobs, [PNG])
        import json as _j
        body = _j.loads(self.seen[0][2])
        self.assertEqual(self.seen[0][1], "/sdcpp/v1/img_gen")
        self.assertEqual((body["width"], body["height"]), (992, 672))          # multiples of 32
        self.assertEqual(body["sample_params"]["sample_steps"], 8)
        self.assertEqual(body["sample_params"]["guidance"]["txt_cfg"], 1.0)
        self.assertEqual(body["seed"], -1)
        self.assertIn("Waiting in line (1 ahead)", texts)
        self.assertEqual(self.inst.busy, 0)

    def test_failed_and_busy(self):
        def failed(req):
            if req.method == "POST":
                return httpx.Response(202, json={"id": "j2"})
            return httpx.Response(200, json={"status": "failed", "error": {"message": "out of memory"}})
        self._serve(failed)
        with self.assertRaises(media.MediaError) as e:
            run(media.sdcpp_generate(self.inst, "image", "x", {}, poll_s=0))
        self.assertIn("out of memory", str(e.exception))
        self._serve(lambda req: httpx.Response(429, json={"error": "queue full"}))
        with self.assertRaises(media.MediaError) as e:
            run(media.sdcpp_generate(self.inst, "image", "x", {}, poll_s=0))
        self.assertIn("busy", str(e.exception))

    def test_poll_url_cannot_leave_the_local_server(self):
        def h(req):
            self.seen.append(str(req.url))
            if req.method == "POST":
                return httpx.Response(202, json={"id": "j3", "poll_url": "http://evil.example/steal"})
            return httpx.Response(200, json={"status": "completed",
                                             "result": {"images": [{"b64_json": base64.b64encode(PNG).decode()}]}})
        self._serve(h)
        run(media.sdcpp_generate(self.inst, "image", "x", {}, poll_s=0))
        self.assertTrue(all(u.startswith("http://127.0.0.1:8097/") for u in self.seen))

    def test_cancel_cancels_on_the_server(self):
        def h(req):
            self.seen.append((req.method, req.url.path))
            if req.method == "POST" and req.url.path.endswith("/img_gen"):
                return httpx.Response(202, json={"id": "j4"})
            if req.method == "POST":
                return httpx.Response(200, json={"status": "cancelled"})
            return httpx.Response(200, json={"status": "generating"})
        self._serve(h)

        async def go():
            t = asyncio.create_task(media.sdcpp_generate(self.inst, "image", "x", {}, poll_s=0.01))
            await asyncio.sleep(0.1)
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t
            await asyncio.sleep(0.05)
        run(go())
        self.assertIn(("POST", "/sdcpp/v1/jobs/j4/cancel"), self.seen)
        self.assertEqual(self.inst.busy, 0)

    def test_step_progress_from_the_console_bar(self):
        # sd.cpp's redrawn bar: used for progress, kept out of the log
        self.assertTrue(self.inst._on_log_line("  |=========>          | 12/20 - 5.12s/it\x1b[K"))
        i, n, spi, _ = self.inst.step
        self.assertEqual((i, n, spi), (12, 20, 5.12))
        self.assertTrue(self.inst._on_log_line("  |==>   | 3/20 - 2.00it/s"))
        self.assertAlmostEqual(self.inst.step[2], 0.5)
        self.assertFalse(self.inst._on_log_line("[INFO ] loading llm from 'x.gguf'"))
        text, pct = media._step_progress((12, 20, 5.0, 10.0), 5.0, {20}, "image")
        self.assertEqual(text, "Step 12 of 20 · 5.0 s/step · about 40 s left")
        self.assertEqual(pct, 57)
        self.assertEqual(media._step_progress((20, 20, 5.0, 10.0), 5.0, {20}, "image")[0], "Finishing the image")
        self.assertEqual(media._step_progress((3, 16, 1.0, 10.0), 5.0, {20}, "image")[0], "Finishing the image")  # VAE tiles
        self.assertEqual(media._step_progress((12, 20, 5.0, 1.0), 5.0, {20}, "image")[1], 3)   # an older job's bar
        self.assertEqual(media._sampling_steps({"sample_params": {"sample_steps": 20},
                                                "init_image": "x", "strength": 0.5}), {20, 10, 11})

    def test_generating_reports_steps(self):
        polls = iter([{"status": "generating"}, {"status": "generating"},
                      {"status": "completed", "result": {"images": [{"b64_json": base64.b64encode(PNG).decode()}]}}])

        def h(req):
            if req.method == "POST":
                return httpx.Response(202, json={"id": "j9"})
            d = next(polls)
            if d["status"] == "generating":
                self.inst._on_log_line("  |====>    | 4/8 - 3.00s/it")
            return httpx.Response(200, json=d)
        self._serve(h)
        seen = []

        async def prog(t, pct):
            seen.append((t, pct))
        run(media.sdcpp_generate(self.inst, "image", "x", {}, prog, poll_s=0))
        self.assertIn(("Step 4 of 8 · 3.0 s/step · about 12 s left", 49), seen)

    def test_size_choice(self):
        # same pixel count per size; the shape changes; multiples of 32
        self.assertEqual(media.size_for("small", "square"), (512, 512))
        self.assertEqual(media.size_for("large", "square"), (1024, 1024))
        self.assertEqual(media.size_for("large", "wide"), (1248, 832))
        self.assertEqual(media.size_for("xlarge", "tall"), (1024, 1536))
        o = media.clean_opts("image", {"aspect": "wide", "size": "medium"})
        self.assertEqual((o["width"], o["height"], o["size"]), (928, 608, "medium"))
        body = media.sdcpp_body(self.inst, "image", "x", o)
        self.assertEqual((body["width"], body["height"]), (928, 608))
        # no size: the old sizes; an unknown size is dropped; video ignores it
        self.assertEqual(media.clean_opts("image", {"aspect": "wide"})["width"], 1536)
        self.assertNotIn("size", media.clean_opts("image", {"aspect": "wide", "size": "huge"}))
        self.assertEqual(media.clean_opts("video", {"aspect": "wide", "size": "small"})["width"], 1280)

    def test_model_default_size(self):
        inst = self.add_sd(default_size="small")
        self.assertEqual(inst.cfg["default_size"], "small")
        body = media.sdcpp_body(inst, "image", "x", media.clean_opts("image", {"aspect": "wide"}))
        self.assertEqual((body["width"], body["height"]), (608, 416))      # the model's size, wide shape
        body = media.sdcpp_body(inst, "image", "x", media.clean_opts("image", {"aspect": "wide", "size": "large"}))
        self.assertEqual((body["width"], body["height"]), (1248, 832))     # the chat's pick wins
        with self.assertRaises(ValueError):
            self.add_sd(default_size="huge")
        self.assertEqual(self.add_sd(default_size="").cfg.get("default_size"), None)

    def test_stopping_the_chat_cancels_the_image(self):
        # the chat's generate_image tool waits with run_to_end; stopping the chat cancels the job
        started = asyncio.Event()

        async def slow(*a, **k):
            started.set()
            await asyncio.sleep(30)

        async def go():
            with mock.patch.object(media, "generate", slow):
                job = media.start_job(self.user, "image", "x", {})
                waiter = asyncio.create_task(media.run_to_end(job))
                await started.wait()
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                await asyncio.sleep(0.05)
                return job
        job = run(go())
        self.assertTrue(job.done)
        self.assertTrue(job.events[-1][1].get("cancelled"))

    def test_chat_tool_streams_progress_then_markdown(self):
        async def fake(kind, user, prompt, opts, progress=None, lane=None):
            await progress("Step 3 of 8", 37)
            return {"files": [], "markdown": "![a bus](/agent/raw?path=generated/a.png)", "model": "Z",
                    "where": "this PC", "source": "local", "lane": "pc-images", "ms": 1}

        async def go():
            with mock.patch.object(media, "generate", fake), \
                    mock.patch.object(media_tools, "_user", lambda: self.user):
                return [e async for e in media_tools.generate_events("image", {"prompt": "a bus"})]
        evs = run(go())
        self.assertEqual(evs[0][0], "progress")
        self.assertEqual((evs[0][1]["text"], evs[0][1]["pct"]), ("Step 3 of 8", 37))
        kind, text, done = evs[-1]
        self.assertEqual(kind, "result")
        self.assertIn("![a bus](/agent/raw?path=generated/a.png)", text)
        self.assertIn("Do not write an HTML page", text)
        self.assertEqual(done["markdown"], "![a bus](/agent/raw?path=generated/a.png)")

    def test_saved_name_is_not_masked_as_a_card_number(self):
        from core import pan
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("core.agent_tools.user_common_root", lambda uid: Path(d)):
            f = self._save.temp_original(1, PNG, "a boy on a cycle", "image")
        self.assertRegex(f["name"], r"^\d{8}_\d{6}_a-boy-on-a-cycle\.png$")
        self.assertEqual(pan.mask_pans(media.markdown_for([f], "x"))[1], 0)

    def test_video_body_and_webm(self):
        def h(req):
            self.seen.append(req)
            if req.method == "POST":
                return httpx.Response(202, json={"id": "v1"})
            return httpx.Response(200, json={"status": "completed", "result": {
                "output_format": "webm", "mime_type": "video/webm", "b64_json": base64.b64encode(WEBM).decode()}})
        self._serve(h)
        blobs = run(media.sdcpp_generate(self.inst, "video", "waves", {"seconds": 2}, poll_s=0))
        self.assertEqual(blobs, [WEBM])
        import json as _j
        body = _j.loads(self.seen[0].content)
        self.assertEqual(self.seen[0].url.path, "/sdcpp/v1/vid_gen")
        self.assertEqual(body["output_format"], "webm")
        self.assertEqual((body["video_frames"] - 1) % 4, 0)

    def test_generate_uses_the_local_engine(self):
        lanes.set_role_map(1, {"image_gen": "pc-images"})
        self._serve(lambda req: httpx.Response(202, json={"id": "j5"}) if req.method == "POST" else
                    httpx.Response(200, json={"status": "completed", "result": {"images": [
                        {"b64_json": base64.b64encode(PNG).decode()}]}}))
        with mock.patch.object(small_model, "find_sd_server", lambda: Path("C:/sd/sd-server.exe")), \
                mock.patch.object(media, "_policy_check", mock.AsyncMock(return_value=None)):
            res = run(media.generate("image", self.user, "a red bus"))
        self.assertEqual(res["where"], "this PC")
        self.assertFalse(res["cloud"])


class LoadRouteAndChatTests(ModelsDirBase):
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
                   mock.patch.object(media_routes, "user_has_permission", lambda u, k: self.admin),
                   mock.patch.object(lanes_routes, "audit_log", lambda *a, **k: None),
                   mock.patch.object(media_routes, "audit_log", lambda *a, **k: None),
                   mock.patch.object(small_model, "find_sd_server", lambda: Path("C:/sd/sd-server.exe"))]
        for p in self.rp:
            p.start()
        self.client = TestClient(app)
        self.inst = self.add_sd()
        lanes.set_role_map(1, {"image_gen": "pc-images"})

    def tearDown(self):
        self.client.close()
        for p in self.rp:
            p.stop()
        super().tearDown()

    def test_files_route_admin_only(self):
        self.assertEqual(self.client.get("/control/lanes/files").status_code, 403)
        self.admin = True
        d = self.client.get("/control/lanes/files?fresh=true").json()
        self.assertEqual([m["name"] for m in d["models"]], ["Spark-4B.gguf"])

    def test_load_route(self):
        r = self.client.post("/control/lanes/pc-images/load")
        self.assertEqual(r.status_code, 403)
        self.admin = True
        r = self.client.post("/control/lanes/executor/load")
        self.assertEqual(r.status_code, 400)                  # helpers start by themselves
        loaded = []

        async def fake_load():
            loaded.append(True)
        with mock.patch.object(self.inst, "load", fake_load):
            r = self.client.post("/control/lanes/pc-images/load")
        self.assertEqual(r.status_code, 200, r.text)
        row = next(x for x in r.json()["lanes"] if x["name"] == "pc-images")
        self.assertEqual(row["state"], "loading")

    def test_only_one_image_model_loaded(self):
        self.admin = True
        other = self.add_sd("pc-images-2", port=8098, label="Second")
        self.inst.process = mock.Mock(poll=lambda: None)        # pc-images is loaded
        try:
            with mock.patch.object(other, "load", mock.AsyncMock()) as ld:
                r = self.client.post("/control/lanes/pc-images-2/load")
            self.assertEqual(r.status_code, 409, r.text)
            self.assertIn("only one image model", r.json()["error"])
            ld.assert_not_called()
            self.inst.process = None
            self.inst.loading_since = time.time()               # still loading counts too
            self.assertEqual(self.client.post("/control/lanes/pc-images-2/load").status_code, 409)
            self.inst.loading_since = None
            with mock.patch.object(other, "load", mock.AsyncMock()):
                self.assertEqual(self.client.post("/control/lanes/pc-images-2/load").status_code, 200)
        finally:
            self.inst.process = None
            self.inst.loading_since = None
            other.loading_since = None

    def test_generate_says_not_loaded(self):
        r = self.client.post("/media/generate", json={"kind": "image", "prompt": "a cat"})
        self.assertEqual(r.status_code, 409)
        nl = r.json()["not_loaded"]
        self.assertEqual(nl["lane"], "pc-images")
        self.assertFalse(nl["can_load"])

    def test_unload_refused_while_busy(self):
        self.admin = True
        self.inst.process = mock.Mock(poll=lambda: None)
        self.inst.busy = 1
        self.assertEqual(self.client.post("/control/lanes/pc-images/stop").status_code, 409)
        self.inst.busy = 0
        self.inst.process = None

    def test_chat_offers_the_tool_only_when_local(self):
        media_tools.register_media_tools()
        with mock.patch("core.media_tools.get_current_user_id", lambda: 1):
            self.assertIsNotNone(media_tools.chat_image_tool_schema())
            with mock.patch.object(media_tools, "first_is_cloud", lambda tool: (True, "OpenAI")):
                self.assertIsNone(media_tools.chat_image_tool_schema())
            lanes.set_role_map(1, {"image_gen": None})
            self.assertIsNone(media_tools.chat_image_tool_schema())


class PictureEditTests(ModelsDirBase):
    """Change a picture (init_image) / combine pictures (ref_images) - on this PC only."""

    VISION = "orchestrator/image-models/mmproj-Qwen3VL-8B-Instruct-F16.gguf"

    def setUp(self):
        super().setUp()
        (self.root / self.VISION).write_bytes(b"\x00" * 2048)
        self.sdp = mock.patch.object(small_model, "find_sd_server", lambda: Path("C:/sd/sd-server.exe"))
        self.sdp.start()
        from core import media_images
        self.pic = media_images.clean_picture(_png(800, 400))

    def tearDown(self):
        self.sdp.stop()
        super().tearDown()

    def test_vision_weights_and_caps(self):
        inst = self.add_sd()
        self.assertEqual(inst.edit_caps(), {"img2img": True, "refs": False, "max_refs": 4})
        cmd, _ = inst._launch_cmd()
        self.assertNotIn("--llm_vision", cmd)
        inst = self.add_sd(llm_vision=self.VISION, max_refs=6)
        self.assertEqual(inst.edit_caps(), {"img2img": True, "refs": True, "max_refs": 6})
        cmd, _ = inst._launch_cmd()
        self.assertTrue(cmd[cmd.index("--llm_vision") + 1].endswith("mmproj-Qwen3VL-8B-Instruct-F16.gguf"))
        self.assertIn(inst.llm_vision_path, inst.files())            # counted in the VRAM check
        with self.assertRaises(ValueError):                           # the text-helper folder isn't allowed
            self.add_sd(llm_vision="orchestrator/mmproj-f16.gguf")
        with self.assertRaises(ValueError):
            self.add_sd(max_refs=11)
        inst = self.add_sd(llm_vision="", edit_refs=True)             # e.g. FLUX Kontext
        self.assertTrue(inst.edit_caps()["refs"])

    def test_request_bodies(self):
        inst = self.add_sd()
        opts = media.clean_opts("image", {"inputs": [self.pic], "mode": "img2img", "strength": 0.4})
        body = media.sdcpp_body(inst, "image", "watercolor", opts)
        self.assertTrue(body["init_image"].startswith("data:image/png;base64,"))
        self.assertEqual(body["strength"], 0.4)
        self.assertNotIn("ref_images", body)
        self.assertEqual((body["width"] % 32, body["height"] % 32), (0, 0))
        self.assertGreater(body["width"], body["height"])            # keeps the 2:1 shape
        small = media.sdcpp_body(inst, "image", "x", media.clean_opts(
            "image", {"inputs": [self.pic], "mode": "img2img", "size": "small"}))
        self.assertEqual((small["width"], small["height"]), (704, 352))   # the 2:1 shape at ~512x512 pixels
        second = dict(self.pic, png=self.pic["png"] + b"")
        opts = media.clean_opts("image", {"inputs": [self.pic, second], "mode": "edit"})
        body = media.sdcpp_body(inst, "image", "combine", opts)
        self.assertEqual(len(body["ref_images"]), 2)
        self.assertNotIn("init_image", body)
        # img2img takes one picture; a video or no mode takes none
        self.assertEqual(len(media.clean_opts("image", {"inputs": [self.pic, second], "mode": "img2img"})["inputs"]), 1)
        self.assertNotIn("inputs", media.clean_opts("image", {"inputs": [self.pic]}))
        self.assertNotIn("inputs", media.clean_opts("video", {"inputs": [self.pic], "mode": "edit"}))

    def test_pictures_never_go_to_the_cloud(self):
        from core import cloud
        inst = self.add_sd(llm_vision=self.VISION)
        key = self.add_cloud(model="gpt-image-1")
        lanes.save_user_lane(1, "cloud-img", {"kind": "image_gen", "cloud": key, "fallback": "pc-images"})
        lanes.set_role_map(1, {"image_gen": "cloud-img"})
        cloud_calls = []
        cc = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: cloud_calls.append(r) or httpx.Response(500)))
        self.serve(inst, self.sd_png)
        with mock.patch.object(cloud, "_client_for", lambda cm: cc):
            res = run(media.generate("image", self.user, "make it blue", {"inputs": [self.pic], "mode": "edit"}))
        self.assertEqual(res["lane"], "pc-images")
        self.assertEqual(cloud_calls, [])
        # only a cloud model mapped: a plain refusal, still nothing sent
        lanes.save_user_lane(1, "cloud-img", {"kind": "image_gen", "cloud": key, "fallback": None})
        with mock.patch.object(cloud, "_client_for", lambda cm: cc), self.assertRaises(media.MediaError) as e:
            run(media.generate("image", self.user, "make it blue", {"inputs": [self.pic], "mode": "edit"}))
        self.assertIn("never sent to a cloud", str(e.exception))
        self.assertEqual(cloud_calls, [])

    def test_combine_needs_vision_weights(self):
        self.add_sd()
        lanes.set_role_map(1, {"image_gen": "pc-images"})
        with self.assertRaises(media.MediaError) as e:
            run(media.generate("image", self.user, "x", {"inputs": [self.pic], "mode": "edit"}))
        self.assertIn("vision weights", str(e.exception))
        st = media.status(1)["image"]["edit"]
        self.assertEqual((st["img2img"], st["refs"]), (True, False))


def _png(w, h):
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(b, format="PNG")
    return b.getvalue()


class PictureRouteTests(LoadRouteAndChatTests):
    def test_route_checks(self):
        b64 = base64.b64encode(_png(64, 64)).decode()
        r = self.client.post("/media/generate", json={"kind": "image", "prompt": "x", "images": [{"b64": b64}]})
        self.assertEqual(r.status_code, 400)                          # pictures need a mode
        r = self.client.post("/media/generate", json={"kind": "image", "prompt": "x", "mode": "edit",
                                                      "images": [{"b64": b64}]})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["needs_edit_model"])
        self.inst.process = mock.Mock(poll=lambda: None)
        r = self.client.post("/media/generate", json={"kind": "image", "prompt": "x", "mode": "img2img",
                                                      "images": [{"b64": base64.b64encode(b"GIF89a....").decode()}]})
        self.assertEqual(r.status_code, 400)                          # not a PNG/JPEG/WEBP
        seen = {}

        async def fake_gen(kind, user, prompt, opts, progress=None, lane=None, enforce_quota=True):
            seen["opts"] = dict(opts)        # the job drops its pictures when it ends
            return {"files": [], "markdown": "", "model": "Z", "source": "local", "where": "this PC",
                    "ms": 1, "lane": "pc-images", "cloud": False}
        logged = []
        from core import media as core_media
        with mock.patch.object(core_media, "generate", fake_gen), \
                mock.patch("core.audit.audit_log", lambda *a, **k: logged.append(k)):
            r = self.client.post("/media/generate", json={"kind": "image", "prompt": "x", "mode": "img2img",
                                                          "strength": 0.3, "images": [{"b64": b64}]})
            self.assertEqual(r.status_code, 200, r.text)
            self.client.get(f"/media/jobs/{r.json()['job_id']}")
        self.assertEqual(seen["opts"]["mode"], "img2img")
        self.assertTrue(seen["opts"]["inputs"][0]["png"].startswith(b"\x89PNG"))
        detail = logged[-1]["detail"]
        self.assertEqual((detail["inputs"], detail["mode"]), (1, "img2img"))
        self.assertNotIn("png", str(detail))
        self.inst.process = None


if __name__ == "__main__":
    unittest.main()
