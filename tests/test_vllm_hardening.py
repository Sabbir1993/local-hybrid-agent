"""vLLM hardening: the launch-flag allowlist, log scrubbing, orphan workers after a crash, adapter robustness, model keys.

Audit repro scripts these come from: scratchpad audit4/c1.py (flags), c2.py (adapter), c3.py (orphan workers)."""
import asyncio
import gc
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from core import process as proc_mod
from core.engines import vllm, vllm_adapter
from routes.control import config_helpers as ch
from routes.control.log_scrub import scrub_log_text


_REAL_HELP = vllm._serve_help


def _settings(**over):
    return {**vllm.DEFAULTS, "bin": "/opt/vllm/bin/vllm", **over}


def _alive(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return raw.rsplit(")", 1)[1].split()[0] != "Z"


# ------------------------------------------------------------------ 1. launch-flag injection
BAD_FLAGS = ["--trust-remote-code", "--trust-remote-code=true", "--trust_remote_code", "--trust-remote",
             "--trust-remote-co=1", "--host=0.0.0.0", "--host 0.0.0.0", "--api-key=abc", "--allowed-local-media-path=/",
             "--middleware=os.system", "--chat-template=/etc/passwd", "--port=9999", "--served-model-name=evil",
             "--enable-log-requests", "--ssl-keyfile=/etc/ssl/private/x", "--lora-modules=a=/x", "--hf-overrides={}",
             "--tokenizer-mode=custom", "--trust-remote-code2", "--kv-cache", "--kv-cache-dtype fp8 --host 0.0.0.0",
             "-tp 4", "kv-cache-dtype fp8", "--model=/etc", "--config=/etc/x.yaml"]


class ExtraArgsAllowlistTests(unittest.TestCase):
    def test_every_audit_payload_is_refused_by_the_settings_validator(self):
        for v in BAD_FLAGS:
            prof = {"model_path": "/x"}
            err = ch._apply_config_update(prof, "vllm_extra_args", v)
            self.assertTrue(err, f"{v!r} was accepted")
            self.assertNotIn("vllm_extra_args", prof)

    def test_the_message_names_the_flag_and_lists_the_allowed_ones(self):
        err = ch._apply_config_update({}, "vllm_extra_args", "--host=0.0.0.0")
        self.assertIn("--host", err)
        self.assertIn("--kv-cache-dtype", err)

    def test_allowed_flags_are_accepted_and_written_in_canonical_form(self):
        prof = {}
        self.assertIsNone(ch._apply_config_update(prof, "vllm_extra_args", "--KV_cache_dtype=fp8  --enforce-eager --seed 7"))
        self.assertEqual(prof["vllm_extra_args"], "--kv-cache-dtype fp8 --enforce-eager --seed 7")
        self.assertEqual(vllm.model_extra_args(prof), ["--kv-cache-dtype", "fp8", "--enforce-eager", "--seed", "7"])

    def test_empty_clears_the_setting(self):
        prof = {"vllm_extra_args": "--seed 1"}
        self.assertIsNone(ch._apply_config_update(prof, "vllm_extra_args", "   "))
        self.assertEqual(prof["vllm_extra_args"], "")

    def test_values_are_checked_per_flag(self):
        bad = ["--seed -1", "--seed=abc", "--max-num-batched-tokens /etc", "--kv-cache-dtype ../x", "--dtype /abs/path",
               "--enforce-eager=false", "--generation-config /etc/passwd", "--swap-space", "--kv-cache-dtype --host",
               "--cpu-offload-gb 1e9x"]
        for v in bad:
            with self.assertRaises(ValueError, msg=v):
                vllm.parse_extra_args(v)
        self.assertEqual(vllm.parse_extra_args("--generation-config vllm"), ["--generation-config", "vllm"])
        self.assertEqual(vllm.parse_extra_args("--swap-space 4.5"), ["--swap-space", "4.5"])

    def test_json_flags_are_parsed_and_reserialised(self):
        out = vllm.parse_extra_args('--speculative-config \'{ "method": "ngram", "num_speculative_tokens": 3 }\'')
        self.assertEqual(out[0], "--speculative-config")
        self.assertEqual(out[1], '{"method":"ngram","num_speculative_tokens":3}')
        for bad in ["--speculative-config {not-json}", "--speculative-config '[1]'", "--compilation-config '{\"a\":\"/etc/x\"}'",
                    "--compilation-config '{\"a\":\"../x\"}'", "--rope-scaling '{\"$ref\":1}'"]:
            with self.assertRaises(ValueError, msg=bad):
                vllm.parse_extra_args(bad)

    def test_a_str_is_split_like_a_shell_and_a_list_is_taken_as_items(self):
        self.assertEqual(vllm.parse_extra_args(["--kv-cache-dtype", "fp8"]), ["--kv-cache-dtype", "fp8"])
        with self.assertRaises(ValueError):
            vllm.parse_extra_args(["--kv-cache-dtype fp8"])         # one item holding two tokens is not a flag name
        with self.assertRaises(ValueError):
            vllm.parse_extra_args([1, 2])
        with self.assertRaises(ValueError):
            vllm.parse_extra_args(5)


class LaunchHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        d = self.root / "Qwen3-8B"
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_bytes(b"x")
        self.model = d
        self.cfg = _settings()
        for p in (mock.patch.object(vllm, "settings", side_effect=lambda: self.cfg),
                  mock.patch.object(vllm, "find_vllm", return_value="/opt/vllm/bin/vllm"),
                  mock.patch.object(vllm, "_serve_help", return_value=""),
                  mock.patch.dict(vllm.ACTIVE_RUNTIME, {"gpu_devices": [0, 1, 2]})):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def prof(self, **kw):
        return {"model_path": str(self.model), "context_size": 32768, "n_slots": 4, "engine": "vllm", **kw}

    def test_hand_edited_model_config_cannot_bypass_the_validator(self):
        for v in ("--host=0.0.0.0", "--trust_remote_code", "--api-key=abc", ["--port", "1"], ["--middleware=m"]):
            with self.assertRaises(RuntimeError, msg=str(v)):
                vllm.build_command(self.prof(vllm_extra_args=v))

    def test_a_str_extra_args_is_not_iterated_per_character(self):
        cmd = vllm.build_command(self.prof(vllm_extra_args="--kv-cache-dtype fp8"))
        self.assertIn("--kv-cache-dtype", cmd)
        self.assertEqual(cmd[cmd.index("--kv-cache-dtype") + 1], "fp8")
        cmd = vllm.build_command(self.prof(vllm_extra_args=["--kv-cache-dtype", "fp8"]))
        self.assertEqual(cmd[cmd.index("--kv-cache-dtype") + 1], "fp8")

    def test_the_apps_own_flags_come_last_and_once(self):
        self.cfg = _settings(extra_args="--enforce-eager --host 0.0.0.0 --port 1 --served-model-name evil")
        cmd = vllm.build_command(self.prof(vllm_extra_args="--seed 3"))
        self.assertEqual(cmd[-6:], ["--host", "127.0.0.1", "--port", str(vllm.LLAMA_SERVER_PORT),
                                    "--served-model-name", "Qwen3-8B"])
        for flag in ("--host", "--port", "--served-model-name"):
            self.assertEqual(cmd.count(flag), 1)
        self.assertNotIn("0.0.0.0", cmd)
        self.assertNotIn("evil", cmd)
        self.assertIn("--enforce-eager", cmd)                      # the harmless admin flag stays
        self.assertNotIn("--trust-remote-code", cmd)
        self.assertNotIn("--api-key", cmd)

    def test_admin_extra_args_drop_the_denied_names_with_their_values(self):
        raw = ["--host", "0.0.0.0", "--api-key=k", "--ssl-keyfile", "/x", "--ssl_certfile=/y", "--middleware", "m",
               "--chat-template", "/etc/passwd", "--allowed-local-media-path=/", "--lora-modules", "a=/p", "--enable-lora",
               "--trust-remote-code", "--trust_remote_code=1", "--served-model-name", "n", "--tokenizer-mode", "custom",
               "--tokenizer", "t", "--port", "9", "--ho", "1.2.3.4", "--trust-remote", "--uds", "/tmp/s",
               "--max-num-batched-tokens", "4096"]
        with mock.patch("builtins.print"):
            out = vllm.admin_extra_args(raw)
        self.assertEqual(out, ["--max-num-batched-tokens", "4096"])
        with mock.patch("builtins.print"):
            self.assertEqual(vllm.admin_extra_args("--host 0.0.0.0 --enforce-eager"), ["--enforce-eager"])
            self.assertEqual(vllm.admin_extra_args(["a", 5]), [])
            self.assertEqual(vllm.admin_extra_args(None), [])

    def test_prompts_are_not_logged_with_whichever_switch_this_vllm_has(self):
        with mock.patch.object(vllm, "_serve_help", return_value="  --disable-log-requests  Disable logging requests"):
            self.assertIn("--disable-log-requests", vllm.build_command(self.prof()))
        with mock.patch.object(vllm, "_serve_help", return_value="--enable-log-requests, --no-enable-log-requests"):
            cmd = vllm.build_command(self.prof())
            self.assertIn("--no-enable-log-requests", cmd)
            self.assertNotIn("--enable-log-requests", cmd)
        cmd = vllm.build_command(self.prof())                       # help unknown: no guess
        self.assertFalse([c for c in cmd if "log-requests" in c])

    def test_help_is_asked_once_per_program(self):
        vllm._HELP_CACHE.clear()
        run = mock.Mock(return_value=mock.Mock(stdout="--disable-log-requests", stderr=""))
        with mock.patch.object(vllm, "_serve_help", _REAL_HELP), mock.patch("core.engines.vllm.subprocess.run", run):
            self.assertEqual(vllm.log_requests_off_flag("/nonexistent/vllm"), ["--disable-log-requests"])
            self.assertEqual(vllm.log_requests_off_flag("/nonexistent/vllm"), ["--disable-log-requests"])
        self.assertEqual(run.call_count, 1)
        vllm._HELP_CACHE.clear()
        with mock.patch.object(vllm, "_serve_help", _REAL_HELP), \
                mock.patch("core.engines.vllm.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(vllm.log_requests_off_flag("/nonexistent/vllm"), [])

    def test_privacy_env(self):
        env = vllm.build_env(self.prof())
        self.assertEqual(env["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")                # a local folder fetches nothing
        env = vllm.build_env(self.prof(model_path=str(self.root / "m.gguf"), vllm_tokenizer="Qwen/Qwen3-8B"))
        self.assertEqual(env["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertEqual(env.get("HF_HUB_OFFLINE"), os.environ.get("HF_HUB_OFFLINE"))   # the tokenizer repo needs the hub
        self.cfg = _settings(env={"HF_HUB_OFFLINE": "0"})
        self.assertEqual(vllm.build_env(self.prof())["HF_HUB_OFFLINE"], "0")   # the admin's env has the last word

    def test_safetensors_are_forced_and_pickle_only_folders_need_an_admin_setting(self):
        cmd = vllm.build_command(self.prof())
        self.assertEqual(cmd[cmd.index("--load-format") + 1], "safetensors")
        (self.model / "model.safetensors").unlink()
        (self.model / "pytorch_model.bin").write_bytes(b"x")
        with self.assertRaises(RuntimeError) as cm:
            vllm.build_command(self.prof())
        self.assertIn("allow_pickle_weights", str(cm.exception))
        self.assertIn("pickle", vllm.preflight(self.prof())["error"])
        self.cfg = _settings(allow_pickle_weights=True)
        cmd = vllm.build_command(self.prof())
        self.assertNotIn("--load-format", cmd)

    def test_names_that_become_flag_values_cannot_start_a_flag(self):
        for key in ("vllm_tool_parser", "vllm_reasoning_parser", "vllm_dtype", "vllm_quantization"):
            with self.assertRaises(RuntimeError, msg=key):
                vllm.build_command(self.prof(**{key: "--host"}))
        with self.assertRaises(RuntimeError):
            vllm.build_command(self.prof(vllm_tool_parser="hermes --trust-remote-code"))


class TokenizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "models" / "tok").mkdir(parents=True)
        (self.root / "outside").mkdir()
        p = mock.patch("core.profiles.paths.models_roots", return_value=[self.root / "models"])
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_id_and_local_folder_are_accepted(self):
        self.assertEqual(vllm.resolve_tokenizer("Qwen/Qwen3-8B"), "Qwen/Qwen3-8B")
        self.assertEqual(vllm.resolve_tokenizer("tok"), str(self.root / "models" / "tok"))
        self.assertEqual(vllm.resolve_tokenizer(str(self.root / "models" / "tok")), str(self.root / "models" / "tok"))
        self.assertEqual(vllm.resolve_tokenizer(""), "")

    def test_escapes_are_refused(self):
        os.symlink(self.root / "outside", self.root / "models" / "link")
        for v in ("../outside", "tok/../../outside", "x/../../etc", "/etc", str(self.root / "outside"), "..", "link",
                  "a\\b", "./tok", "-x", "a b", "/tmp"):
            with self.assertRaises(ValueError, msg=v):
                vllm.resolve_tokenizer(v)

    def test_settings_validator_uses_it(self):
        self.assertIsNone(ch._apply_config_update({}, "vllm_tokenizer", "tok"))
        self.assertIsNone(ch._apply_config_update({}, "vllm_tokenizer", "Qwen/Qwen3-8B"))
        for v in ("../../etc", "/etc", "--trust-remote-code"):
            self.assertTrue(ch._apply_config_update({}, "vllm_tokenizer", v), v)

    def test_build_command_passes_the_resolved_folder(self):
        m = self.root / "models" / "Q"
        m.mkdir()
        (m / "config.json").write_text("{}")
        (m / "w.safetensors").write_bytes(b"x")
        with mock.patch.object(vllm, "settings", return_value=_settings()), \
                mock.patch.object(vllm, "find_vllm", return_value="/opt/vllm/bin/vllm"), \
                mock.patch.object(vllm, "_serve_help", return_value=""), \
                mock.patch.dict(vllm.ACTIVE_RUNTIME, {"gpu_devices": [0]}):
            cmd = vllm.build_command({"model_path": str(m), "vllm_tokenizer": "tok"})
            self.assertEqual(cmd[cmd.index("--tokenizer") + 1], str(self.root / "models" / "tok"))
            with self.assertRaises(RuntimeError):
                vllm.build_command({"model_path": str(m), "vllm_tokenizer": "/etc"})
            self.assertEqual(vllm.build_env({"model_path": str(m), "vllm_tokenizer": "tok"})["HF_HUB_OFFLINE"], "1")
            self.assertEqual(vllm.build_env({"model_path": str(m), "vllm_tokenizer": "Qwen/Qwen3-8B"}).get("HF_HUB_OFFLINE"),
                             os.environ.get("HF_HUB_OFFLINE"))


# ------------------------------------------------------------------ 2. log scrubbing and the status / log routes
class ScrubTests(unittest.TestCase):
    def test_paths_tokens_and_cards_are_removed(self):
        s = scrub_log_text("Loading /home/ops/models/Qwen3/model.safetensors with HF_TOKEN=hf_abcdefghijk1234 "
                           "Authorization: Bearer eyJhbGciOi.abc.def card 4111 1111 1111 1111 C:\\Users\\bob\\m.bin")
        for leak in ("/home/ops", "hf_abcdefghijk1234", "eyJhbGciOi", "4111 1111 1111 1111", "bob"):
            self.assertNotIn(leak, s)
        self.assertIn("<path>", s)

    def test_api_key_flag_and_url_paths(self):
        self.assertIn("<redacted>", scrub_log_text("vllm serve --api-key hunter22secret --port 8090"))
        self.assertNotIn("hunter22secret", scrub_log_text("vllm serve --api-key hunter22secret"))
        self.assertEqual(scrub_log_text("POST /v1/chat/completions 200"), "POST /v1/chat/completions 200")
        self.assertEqual(scrub_log_text("GET /health 200"), "GET /health 200")


class StatusAndLogRouteTests(unittest.TestCase):
    LINES = ["load /srv/models/secret-model/w.safetensors", "token hf_abcdefghijklmnop", "pan 4111111111111111 ok"]

    def _user(self, perms):
        from core.auth import Principal
        u = mock.MagicMock(spec=Principal)
        u.id = 5
        return u, perms

    def test_status_log_tail_is_super_admin_only_and_scrubbed(self):
        from routes.control import status_endpoints as se
        with mock.patch.object(se.state, "log_tail", return_value=list(self.LINES)), \
                mock.patch.object(se.state, "last_load_error", "failed: /opt/vllm/lib/x.py hf_zzzzzzzzzz"):
            admin = mock.Mock(is_super_admin=True, id=1)
            delegate = mock.Mock(is_super_admin=False, id=2)       # holds model.local.load, which is delegable
            self.assertTrue(se._may_read_log(admin))
            self.assertFalse(se._may_read_log(delegate))
            self.assertFalse(se._may_read_log(None))
            out = asyncio.run(self._status(se, admin))
            self.assertEqual(len(out["log_tail"]), 3)
            blob = json.dumps(out["log_tail"]) + out["last_load_error"]
            for leak in ("/srv/models", "hf_abcdefghijklmnop", "4111111111111111", "/opt/vllm", "hf_zzzzzzzzzz"):
                self.assertNotIn(leak, blob)
            out = asyncio.run(self._status(se, delegate))
            self.assertEqual(out["log_tail"], [])                   # no raw lines for the delegate ...
            self.assertIn("failed:", out["last_load_error"])        # ... but the scrubbed reason is still there
            self.assertNotIn("/opt/vllm", out["last_load_error"])
            self.assertNotIn("hf_zzzzzzzzzz", out["last_load_error"])

    @staticmethod
    async def _status(se, user):
        with mock.patch.object(se, "refresh_hw_async", new=mock.AsyncMock()), \
                mock.patch.object(se, "get_hardware_engine_summary", return_value={}), \
                mock.patch.object(se.cloud, "cloud_lane", return_value=None):
            user.id = user.id
            return await se.get_system_status(user)

    def test_control_log_is_scrubbed(self):
        from routes.control import server_endpoints as sv
        with mock.patch.object(sv.state, "log_tail", return_value=list(self.LINES)), \
                mock.patch.object(sv.state, "last_load_error", "boom /home/x/y/z"):
            out = asyncio.run(sv.server_log(n=10, user=mock.Mock(is_super_admin=True)))
        blob = json.dumps(out)
        for leak in ("/srv/models", "hf_abcdefghijklmnop", "4111111111111111", "/home/x"):
            self.assertNotIn(leak, blob)
        self.assertEqual(len(out["lines"]), 3)

    def test_control_log_refuses_a_model_load_delegate(self):
        from fastapi import HTTPException
        from routes.control import server_endpoints as sv
        delegate = mock.Mock(is_super_admin=False, id=2)           # may hold model.local.load, still not allowed here
        with mock.patch.object(sv.state, "log_tail", return_value=list(self.LINES)) as lt, \
                mock.patch.object(sv, "audit_log") as audit:
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(sv.server_log(n=10, user=delegate))
        self.assertEqual(cm.exception.status_code, 403)
        lt.assert_not_called()                                      # nothing was read for the refused caller
        self.assertEqual(audit.call_args.kwargs.get("result"), "deny")

    def test_control_log_dependency_is_the_session_not_the_delegable_permission(self):
        import inspect
        from routes.control import server_endpoints as sv
        dep = inspect.signature(sv.server_log).parameters["user"].default.dependency
        self.assertIsNone(getattr(dep, "_require_permission_key", None))


# ------------------------------------------------------------------ 3. orphan workers
class OrphanWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(proc_mod, "ENGINE_PID_FILE", Path(self.tmp.name) / "main-engine.pid")
        p.start()
        self.addCleanup(p.stop)
        self.pids, self.procs = [], []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except Exception:
                pass
            p.stdout.close()
        self.tmp.cleanup()

    def leader_with_worker(self, exit_leader=True):
        """A session leader that starts a worker and (by default) exits at once: what a crashed vLLM server looks like."""
        tail = "exit 3" if exit_leader else "wait"
        p = subprocess.Popen(["sh", "-c", f"sleep 777 & echo $! ; {tail}"], stdout=subprocess.PIPE, text=True,
                             start_new_session=True)
        child = int(p.stdout.readline())
        self.pids += [child, p.pid]
        self.procs.append(p)
        return p, child

    def test_workers_of_a_crashed_leader_are_stopped(self):
        p, child = self.leader_with_worker()
        proc_mod.remember_engine_pid(p)
        p.wait()
        time.sleep(0.1)
        self.assertTrue(_alive(child))
        proc_mod.kill_process_tree(p, timeout=3)
        time.sleep(0.2)
        self.assertFalse(_alive(child))

    def test_a_live_leader_group_is_stopped_with_its_workers(self):
        p, child = self.leader_with_worker(exit_leader=False)
        proc_mod.remember_engine_pid(p)
        proc_mod.kill_process_tree(p, timeout=3)
        time.sleep(0.2)
        self.assertFalse(_alive(child))
        self.assertIsNotNone(p.poll())

    def test_the_pid_file_holds_group_start_time_and_boot(self):
        p, _ = self.leader_with_worker(exit_leader=False)
        proc_mod.remember_engine_pid(p)
        rec = json.loads(proc_mod.ENGINE_PID_FILE.read_text())
        self.assertEqual(rec["pgid"], p.pid)
        self.assertEqual(rec["start"], proc_mod._proc_start(p.pid))
        self.assertTrue(rec["boot"])

    def test_the_next_start_stops_workers_whose_leader_died(self):
        p, child = self.leader_with_worker()
        proc_mod.remember_engine_pid(p)
        p.wait()
        self.assertTrue(proc_mod.kill_orphan_engine())
        time.sleep(0.2)
        self.assertFalse(_alive(child))
        self.assertFalse(proc_mod.ENGINE_PID_FILE.exists())

    def test_a_recycled_process_id_is_not_killed(self):
        """A live group whose leader started at another time than recorded belongs to someone else."""
        p, child = self.leader_with_worker(exit_leader=False)
        proc_mod.remember_engine_pid(p)
        rec = json.loads(proc_mod.ENGINE_PID_FILE.read_text())
        rec["start"] += 1000
        proc_mod.ENGINE_PID_FILE.write_text(json.dumps(rec))
        self.assertFalse(proc_mod.kill_orphan_engine())
        self.assertTrue(_alive(child))
        self.assertTrue(_alive(p.pid))
        self.assertFalse(proc_mod.ENGINE_PID_FILE.exists())

    def test_a_new_boot_is_not_killed(self):
        p, child = self.leader_with_worker(exit_leader=False)
        proc_mod.remember_engine_pid(p)
        rec = json.loads(proc_mod.ENGINE_PID_FILE.read_text())
        rec["boot"] = "another-boot"
        proc_mod.ENGINE_PID_FILE.write_text(json.dumps(rec))
        self.assertFalse(proc_mod.kill_orphan_engine())
        self.assertTrue(_alive(child))

    def test_an_old_pid_file_only_kills_a_vllm_process(self):
        p, child = self.leader_with_worker(exit_leader=False)
        proc_mod.ENGINE_PID_FILE.write_text(str(p.pid))              # the old format: just the id
        self.assertFalse(proc_mod.kill_orphan_engine())              # its command line does not say vllm
        self.assertTrue(_alive(child))
        self.assertTrue(_alive(p.pid))

    def test_our_own_group_and_pid_one_are_never_signalled(self):
        for pgid in (1, os.getpgrp()):
            proc_mod.ENGINE_PID_FILE.write_text(json.dumps({"pgid": pgid, "start": None, "boot": ""}))
            self.assertFalse(proc_mod.kill_orphan_engine())

    def test_an_exited_plain_process_is_left_alone(self):
        """No vLLM record on the Popen (llama-server and friends): nothing is looked up or killed."""
        p, child = self.leader_with_worker()
        p.wait()
        proc_mod.kill_process_tree(p, timeout=1)
        self.assertTrue(_alive(child))


# ------------------------------------------------------------------ 5. adapter
class _Inner(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.c = chunks
        self.closed = 0

    async def __aiter__(self):
        for c in self.c:
            yield c

    async def aclose(self):
        self.closed += 1


def _sse(obj):
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


class AdapterRobustnessTests(unittest.TestCase):
    def setUp(self):
        vllm_adapter._last["inflight"] = 0

    def run_stream(self, chunks):
        async def go():
            vllm_adapter._last["inflight"] = 1
            out = b""
            async for c in vllm_adapter._SseRewrite(_Inner(chunks), time.monotonic(), 7):
                out += c
            return out
        return asyncio.run(go())

    def test_odd_choices_do_not_crash(self):
        for choices in ("oops", [None], [1, "x", None], {"a": 1}, [{"delta": "str"}], [{"delta": None}]):
            out = self.run_stream([_sse({"choices": choices})])
            self.assertIn(b"choices", out, choices)
            self.assertEqual(vllm_adapter._last["inflight"], 0)
        obj = {"choices": [None, {"delta": {"reasoning": "hm"}}, "x"]}
        vllm_adapter._rename_reasoning(obj)
        self.assertEqual(obj["choices"][1]["delta"]["reasoning_content"], "hm")
        self.assertFalse(vllm_adapter._has_output({"choices": "oops"}))
        self.assertTrue(vllm_adapter._has_output({"choices": [None, {"delta": {"content": "a"}}]}))

    def test_non_stream_reply_with_odd_choices(self):
        class Inner(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, json={"choices": "oops", "usage": {"prompt_tokens": 1}})
        tr = vllm_adapter.EngineTransport(lambda: {"engine": "vllm", "model_path": "/x"}, Inner())

        async def go():
            return await tr.handle_async_request(httpx.Request("POST", "http://x/v1/chat/completions", json={"messages": []}))
        resp = asyncio.run(go())
        self.assertEqual(resp.status_code, 200)

    def _sse_transport(self):
        inner = _Inner([_sse({"choices": [{"delta": {"content": "a"}}]})])

        class T(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=inner)
        return vllm_adapter.EngineTransport(lambda: {"engine": "vllm", "model_path": "/x"}, T()), inner

    def test_inflight_is_released_when_the_response_is_closed_unread(self):
        tr, inner = self._sse_transport()

        async def go():
            resp = await tr.handle_async_request(httpx.Request("POST", "http://x/v1/chat/completions", json={"stream": True}))
            self.assertEqual(vllm_adapter._last["inflight"], 1)
            await resp.aclose()
            await resp.aclose()                                    # twice: counted once
            return resp
        asyncio.run(go())
        self.assertEqual(vllm_adapter._last["inflight"], 0)
        self.assertEqual(inner.closed, 1)

    def test_inflight_is_released_when_the_response_is_dropped(self):
        tr, _ = self._sse_transport()

        async def go():
            resp = await tr.handle_async_request(httpx.Request("POST", "http://x/v1/chat/completions", json={"stream": True}))
            self.assertEqual(vllm_adapter._last["inflight"], 1)
            del resp
            gc.collect()
        asyncio.run(go())
        self.assertEqual(vllm_adapter._last["inflight"], 0)

    def test_inflight_never_goes_negative(self):
        s = vllm_adapter._SseRewrite(_Inner([]), time.monotonic(), None)
        asyncio.run(s.aclose())
        asyncio.run(s.aclose())
        del s
        gc.collect()
        self.assertEqual(vllm_adapter._last["inflight"], 0)


# ------------------------------------------------------------------ 5. model-config keys for folders with one name
class ModelKeyTests(unittest.TestCase):
    def setUp(self):
        import core.profiles as profiles
        from core.profiles import constants
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "a" / "Qwen3-8B").mkdir(parents=True)
        (self.root / "b" / "Qwen3-8B").mkdir(parents=True)
        (self.root / "a" / "Solo").mkdir()
        constants._dup_cache.clear()
        for p in (mock.patch.object(profiles, "MODELS_DIR", self.root / "a"),
                  mock.patch.object(profiles, "_extra_roots", [self.root / "b"])):
            p.start()
            self.addCleanup(p.stop)
        self.key = profiles._model_key

    def tearDown(self):
        self.tmp.cleanup()

    def test_colliding_folders_get_the_parent_in_their_key(self):
        a, b = self.key(self.root / "a" / "Qwen3-8B"), self.key(self.root / "b" / "Qwen3-8B")
        self.assertEqual((a, b), ("qwen3-8b@a", "qwen3-8b@b"))

    def test_other_keys_stay_the_plain_name(self):
        self.assertEqual(self.key(self.root / "a" / "Solo"), "solo")
        self.assertEqual(self.key("Qwen3-8B"), "qwen3-8b")                 # a bare name is not a place
        self.assertEqual(self.key("/models/some/file.GGUF"), "file.gguf")

    def test_separate_saved_settings(self):
        from core.profiles import builder
        cfgs = {"qwen3-8b@a": {"vllm_dtype": "bfloat16"}, "qwen3-8b@b": {"vllm_dtype": "float16"}}
        for d in ("a", "b"):
            f = self.root / d / "Qwen3-8B"
            (f / "config.json").write_text("{}")
            (f / "w.safetensors").write_bytes(b"x")
        with mock.patch.object(builder, "load_model_configs", return_value=cfgs), \
                mock.patch.object(builder, "apply_runtime", side_effect=lambda p: p):
            self.assertEqual(builder._folder_profile(self.root / "a" / "Qwen3-8B")["vllm_dtype"], "bfloat16")
            self.assertEqual(builder._folder_profile(self.root / "b" / "Qwen3-8B")["vllm_dtype"], "float16")


if __name__ == "__main__":
    unittest.main()
