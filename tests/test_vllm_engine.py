"""vLLM as the main-model engine: launch command, model checks, and the adapter that makes vLLM answer like llama-server."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from core.engines import engine_of, vllm
from core.engines import vllm_adapter
from routes.common.overflow import _ctx_from_error, _is_context_overflow


def _settings(**over):
    return {**vllm.DEFAULTS, "bin": "/opt/vllm/bin/vllm", **over}


class ModelFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def folder(self, name="Qwen3-8B-AWQ"):
        d = self.root / name
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_bytes(b"x" * 10)
        return d

    def test_hf_folder_runs_on_vllm_only(self):
        d = self.folder()
        self.assertTrue(vllm.is_hf_folder(d))
        self.assertEqual(vllm.model_format(d), "hf")
        self.assertEqual(vllm.engines_for(d), ["vllm"])
        self.assertEqual(vllm.folder_size_bytes(d), 10)

    def test_folder_without_weights_is_not_a_model(self):
        d = self.root / "notes"
        d.mkdir()
        (d / "config.json").write_text("{}")
        self.assertFalse(vllm.is_hf_folder(d))
        self.assertEqual(vllm.engines_for(d), [])

    def test_gguf_runs_on_both(self):
        f = self.root / "m.gguf"
        f.write_bytes(b"GGUF")
        self.assertEqual(vllm.engines_for(f), ["llama", "vllm"])

    def test_gguf_on_vllm_needs_a_tokenizer_and_a_single_file(self):
        f = self.root / "m.gguf"
        f.write_bytes(b"GGUF")
        self.assertIn("tokenizer", vllm.target_problem({"model_path": str(f)}))
        self.assertIsNone(vllm.target_problem({"model_path": str(f), "vllm_tokenizer": "Qwen/Qwen3-8B"}))
        for i in (1, 2):
            (self.root / f"big-0000{i}-of-00002.gguf").write_bytes(b"GGUF")
        self.assertIn("split", vllm.target_problem({"model_path": str(self.root / "big-00001-of-00002.gguf"),
                                                    "vllm_tokenizer": "x/y"}))

    def test_discovery_lists_folders(self):
        from core.profiles import discovery
        self.folder("Llama-3-8B-FP8")
        fam = self.root / "qwen"
        fam.mkdir()
        sub = fam / "Qwen3-32B-AWQ"
        sub.mkdir()
        (sub / "config.json").write_text("{}")
        (sub / "model-00001-of-00002.safetensors").write_bytes(b"x")
        (fam / "q.gguf").write_bytes(b"GGUF")
        with mock.patch.object(discovery, "models_roots", return_value=[self.root]):
            found = {(p.name, folder) for p, folder in discovery.discover_models()}
        self.assertIn(("Llama-3-8B-FP8", None), found)
        self.assertIn(("Qwen3-32B-AWQ", "qwen"), found)
        self.assertIn(("q.gguf", "qwen"), found)

    def test_folder_profile_is_vllm(self):
        from core.profiles.builder import build_dynamic_profile
        d = self.folder()
        with mock.patch("core.profiles.builder.load_model_configs", return_value={}):
            prof = build_dynamic_profile(d)
        self.assertEqual(engine_of(prof), "vllm")
        self.assertEqual(prof["model_path"], str(d))

    def test_display_name(self):
        self.assertEqual(vllm.display_name(self.folder("Qwen3-8B-AWQ")), "Qwen3-8B-AWQ")
        d = self.folder("hf")
        d.joinpath("config.json").write_text(json.dumps({"_name_or_path": "Qwen/Qwen3-14B"}))
        self.assertEqual(vllm.display_name(d), "Qwen3-14B")
        d.joinpath("config.json").write_text(json.dumps({"architectures": ["X"]}))
        self.assertEqual(vllm.display_name(d), "hf")
        d.joinpath("config.json").write_text("{not json")
        self.assertEqual(vllm.display_name(d), "hf")
        d.joinpath("config.json").write_text(json.dumps({"_name_or_path": "."}))
        self.assertEqual(vllm.display_name(d), "hf")

    def test_nested_folder_display_and_served_name(self):
        from core import profiles
        saved = (profiles.MODELS_DIR, list(profiles._extra_roots))
        profiles.MODELS_DIR = self.root
        profiles._extra_roots.clear()
        try:
            (self.root / "hf").mkdir()
            sub = self.root / "hf" / "Qwen3-32B-AWQ"
            sub.mkdir()
            (sub / "config.json").write_text("{}")
            (sub / "model.safetensors").write_bytes(b"x")
            found = profiles.discover_models()
            self.assertEqual([(f.name, folder) for f, folder in found], [("Qwen3-32B-AWQ", "hf")])
            self.assertEqual(vllm.display_name(found[0][0]), "Qwen3-32B-AWQ")
            self.assertEqual(vllm.served_name({"model_path": str(sub)}), "Qwen3-32B-AWQ")
        finally:
            profiles.MODELS_DIR = saved[0]
            profiles._extra_roots[:] = saved[1]

    def test_chat_template_from_folder(self):
        from core import reasoning
        d = self.folder()
        (d / "tokenizer_config.json").write_text(json.dumps({"chat_template": "{% if enable_thinking %}<think>{% endif %}"}))
        self.assertIn("enable_thinking", reasoning.chat_template(d))


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name) / "Qwen3-8B"
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_bytes(b"x")
        self.model = d
        self._s = mock.patch.object(vllm, "settings", return_value=_settings())
        self._s.start()
        self._f = mock.patch.object(vllm, "find_vllm", return_value="/opt/vllm/bin/vllm")
        self._f.start()
        self._g = mock.patch.dict(vllm.ACTIVE_RUNTIME, {"gpu_devices": [0, 1, 2]})
        self._g.start()

    def tearDown(self):
        self._g.stop()
        self._f.stop()
        self._s.stop()
        self.tmp.cleanup()

    def prof(self, **kw):
        return {"model_path": str(self.model), "context_size": 32768, "n_slots": 4, "engine": "vllm", **kw}

    def test_command(self):
        cmd = vllm.build_command(self.prof())
        self.assertEqual(cmd[:3], ["/opt/vllm/bin/vllm", "serve", str(self.model)])
        args = " ".join(cmd)
        for part in ("--host 127.0.0.1", "--max-model-len 32768", "--max-num-seqs 4", "--tensor-parallel-size 2",
                     "--pipeline-parallel-size 1", "--enable-prefix-caching", "--tool-call-parser hermes"):
            self.assertIn(part, args)
        self.assertNotIn("--trust-remote-code", args)

    def test_three_gpus_default_to_tensor_parallel_two(self):
        self.assertEqual(vllm.parallel_sizes(self.prof()), (2, 1))
        self.assertEqual(vllm.gpu_devices(self.prof()), [0, 1])
        self.assertEqual(vllm.gpu_devices(self.prof(tensor_parallel_size=1, pipeline_parallel_size=3)), [0, 1, 2])

    def test_env_selects_gpus_and_turns_off_usage_stats(self):
        env = vllm.build_env(self.prof())
        self.assertEqual(env["ZE_AFFINITY_MASK"], "0,1")
        self.assertEqual(env["VLLM_NO_USAGE_STATS"], "1")
        self.assertEqual(env["DO_NOT_TRACK"], "1")

    def test_options(self):
        args = " ".join(vllm.build_command(self.prof(vllm_quantization="fp8", vllm_dtype="bfloat16", vllm_tool_parser="",
                                                     vllm_reasoning_parser="qwen3", vllm_enforce_eager=True,
                                                     gpu_memory_utilization_pct=70)))
        self.assertIn("--quantization fp8", args)
        self.assertIn("--dtype bfloat16", args)
        self.assertIn("--reasoning-parser qwen3", args)
        self.assertIn("--enforce-eager", args)
        self.assertIn("--gpu-memory-utilization 0.7", args)
        self.assertNotIn("--tool-call-parser", args)

    def test_one_slot_uses_the_vllm_default_and_extra_args_are_added(self):
        cmd = vllm.build_command(self.prof(n_slots=1, vllm_extra_args="--kv-cache-dtype fp8"))
        args = " ".join(cmd)
        self.assertIn("--max-num-seqs 8", args)
        self.assertEqual(cmd[-2:], ["--kv-cache-dtype", "fp8"])

    def test_preflight_warns_about_shared_gpus(self):
        with mock.patch.object(vllm, "helper_gpus", return_value={1}):
            plan = vllm.preflight(self.prof())
        self.assertEqual(plan["status"], "tight")
        self.assertIn("GPU 1", plan["warnings"][0])


class LayerInfoTests(unittest.TestCase):
    def test_gguf_and_folder_layer_counts(self):
        from routes.control import config_helpers as ch
        with mock.patch("core.vram.gguf_parser.parse_gguf_info",
                        return_value={"n_layer": 48, "file_bytes": 49 * 400 * 1048576, "stat_ok": True, "complete": True}):
            self.assertEqual(ch._layer_info("/m/x.gguf"), {"total_layers": 49, "layer_mb": 400})
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.json").write_text(json.dumps({"num_hidden_layers": 64}))
            (Path(d) / "model.safetensors").write_bytes(b"x" * 64 * 1048576)
            self.assertEqual(ch._layer_info(d), {"total_layers": 64, "layer_mb": 1})
        self.assertEqual(ch._layer_info("/nope/missing.gguf"), {"total_layers": None, "layer_mb": None})


class AdapterTests(unittest.TestCase):
    INFO = {"engine": "vllm", "context_size": 16384, "model_path": "/m/Qwen3-8B"}

    def test_request_fields(self):
        with mock.patch.object(vllm, "settings", return_value=_settings()):
            out = vllm_adapter.adapt_request({"messages": [], "stream": True, "id_slot": 1, "cache_prompt": True,
                                              "repeat_penalty": 1.1, "max_tokens": -1, "grammar": "root ::= \"a\"",
                                              "thinking_budget_tokens": 0, "top_k": 20}, self.INFO)
        for k in ("id_slot", "cache_prompt", "repeat_penalty", "max_tokens", "grammar", "thinking_budget_tokens"):
            self.assertNotIn(k, out)
        self.assertEqual(out["repetition_penalty"], 1.1)
        self.assertEqual(out["structured_outputs"], {"grammar": "root ::= \"a\""})
        self.assertEqual(out["stream_options"], {"include_usage": True})
        self.assertEqual(out["model"], "Qwen3-8B")
        self.assertEqual(out["top_k"], 20)

    def test_timings_from_usage(self):
        t = vllm_adapter.timings_from_usage({"prompt_tokens": 1000, "completion_tokens": 50,
                                             "prompt_tokens_details": {"cached_tokens": 800}}, 0.0, 0.5, 2.5)
        self.assertEqual((t["prompt_n"], t["cache_n"], t["predicted_n"]), (200, 800, 50))
        self.assertEqual(t["predicted_per_second"], 25.0)

    def run_transport(self, info, handler, method="POST", path="/v1/chat/completions", body=None):
        sent = {}

        def inner(request):
            sent["body"] = json.loads(request.content or b"{}") if request.content else None
            return handler(request)

        async def go():
            t = vllm_adapter.EngineTransport(lambda: info, inner=httpx.MockTransport(inner))
            async with httpx.AsyncClient(transport=t, base_url="http://127.0.0.1:8090") as c:
                if method == "GET":
                    r = await c.get(path)
                    return r, [r.json()]
                async with c.stream("POST", path, json=body) as r:
                    lines = [line async for line in r.aiter_lines()]
                    return r, lines
        r, out = asyncio.run(go())
        return r, out, sent.get("body")

    def test_stream_gets_reasoning_and_timings(self):
        chunks = [{"choices": [{"delta": {"reasoning": "hmm"}}]},
                  {"choices": [{"delta": {"content": "hi"}}]},
                  {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}]
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        with mock.patch.object(vllm, "settings", return_value=_settings()):
            r, lines, sent = self.run_transport(self.INFO, lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse.encode()),
                body={"messages": [], "stream": True, "id_slot": 0})
        self.assertNotIn("id_slot", sent)
        data = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
        self.assertEqual(data[0]["choices"][0]["delta"]["reasoning_content"], "hmm")
        self.assertEqual(data[2]["timings"]["predicted_n"], 2)
        self.assertEqual(data[2]["timings"]["prompt_n"], 10)
        self.assertIn("data: [DONE]", lines)

    def test_slots_answered_locally(self):
        r, out, _ = self.run_transport(self.INFO, lambda req: httpx.Response(404), method="GET", path="/slots")
        self.assertEqual(out[0][0]["n_ctx"], 16384)

    def test_llama_passes_through(self):
        r, lines, sent = self.run_transport({"engine": "llama"}, lambda req: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"data: {\"timings\": {}}\n\n"),
            body={"messages": [], "id_slot": 1, "cache_prompt": True})
        self.assertEqual(sent["id_slot"], 1)
        self.assertTrue(sent["cache_prompt"])

    def test_vllm_context_overflow_is_recognised(self):
        body = json.dumps({"error": {"message": "This model's maximum context length is 32768 tokens. However, you "
                                                "requested 40000 tokens.", "code": 400}}).encode()
        self.assertTrue(_is_context_overflow(400, body))
        self.assertEqual(_ctx_from_error(body), 32768)


class ServingLimitTests(unittest.TestCase):
    """Admin-editable concurrency limits: persisted into nested app.json sections, clamped, applied in memory."""

    def setUp(self):
        from core import small_model
        self.cfg = small_model.APP_CONFIG
        self.saved = dict(self.cfg)
        self.cfg.pop("serving", None)
        self.cfg["engines"] = {"vllm": {"bin": "/x/vllm"}}
        self.addCleanup(lambda: (self.cfg.clear(), self.cfg.update(self.saved)))

    def apply(self, key, value):
        from routes.control.config_helpers import _apply_config_update
        disk = {}
        with mock.patch("core.config.update_app_config", side_effect=lambda m: m(disk)) as upd:
            err = _apply_config_update({"name": "m"}, key, value)
        return err, disk, upd

    def test_per_user_valid_and_clamped(self):
        for given, want in ((3, 3), (99, 16), (0, 1), ("4", 4)):
            err, disk, _ = self.apply("max_inflight_per_user", given)
            self.assertIsNone(err)
            self.assertEqual(disk, {"serving": {"max_inflight_per_user": want}})
            self.assertEqual(self.cfg["serving"]["max_inflight_per_user"], want)

    def test_vllm_seqs_valid_and_clamped_keeps_other_keys(self):
        for given, want in ((12, 12), (99, 64), (0, 1)):
            err, disk, _ = self.apply("vllm_max_num_seqs_default", given)
            self.assertIsNone(err)
            self.assertEqual(disk, {"engines": {"vllm": {"max_num_seqs": want}}})
            self.assertEqual(self.cfg["engines"]["vllm"], {"bin": "/x/vllm", "max_num_seqs": want})
            self.assertEqual(vllm.settings()["max_num_seqs"], want)

    def test_non_integer_is_an_error_and_writes_nothing(self):
        for key in ("max_inflight_per_user", "vllm_max_num_seqs_default"):
            err, disk, upd = self.apply(key, "lots")
            self.assertIn("must be an integer", err)
            self.assertEqual(disk, {})
            upd.assert_not_called()
        self.assertNotIn("serving", self.cfg)
        self.assertNotIn("max_num_seqs", self.cfg["engines"]["vllm"])


if __name__ == "__main__":
    unittest.main()
