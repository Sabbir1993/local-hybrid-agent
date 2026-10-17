"""tests/test_runner.py - the code runner service (tools/runner) and the app's side of it (core/sandbox).

The service runs here in a thread with --allow-unsandboxed (bubblewrap is Linux only); what a real sandbox adds is checked by the
runner's own self-test on the VM. Everything about pairing, pinning, limits and vetting is exercised for real."""
import asyncio
import base64
import datetime
import hashlib
import importlib.util
import ipaddress
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.agent_tools  # noqa: F401, E402
from core.sandbox import admin, client, config, service, tool  # noqa: E402

spec = importlib.util.spec_from_file_location("runner_server", ROOT / "tools" / "runner" / "runner_server.py")
rs = importlib.util.module_from_spec(spec)
sys.modules["runner_server"] = rs
spec.loader.exec_module(rs)


def run(coro):
    return asyncio.run(coro)


def make_cert(d: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "runner")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                                  serialization.NoEncryption()))
    return d / "cert.pem", d / "key.pem"


class Live(unittest.TestCase):
    """A real runner on 127.0.0.1 with a self-signed certificate."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.old = dict(rs.STATE)
        rs.STATE.update(dir=d, allow_unsandboxed=True, sandbox="none")
        cert, key = make_cert(d)
        cls.httpd = rs.make_server("127.0.0.1", 0, cert, key)
        cls.port = cls.httpd.server_address[1]
        cls.url = f"https://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        rs.STATE.clear()
        rs.STATE.update(cls.old)
        cls.tmp.cleanup()

    def setUp(self):
        for n in ("pair.json", "token.sha256"):
            (rs.STATE["dir"] / n).unlink(missing_ok=True)

    def req(self, method, path, body=None, token=None, pin=None):
        return run(client.request(self.url, method, path, body, token=token, pin=pin, timeout=60))

    def paired(self):
        code = rs.new_pair_code()
        fp = self.req("GET", "/health")["fingerprint"]
        r = self.req("POST", "/pair", {"proof": client.pairing_proof(code, fp)})
        self.assertEqual(r["status"], 200, r)
        return r["json"]["token"], fp


class PairingTests(Live):
    def test_health_is_open_and_names_the_service(self):
        r = self.req("GET", "/health")
        self.assertEqual((r["status"], r["json"]["service"], r["json"]["paired"]), (200, "a770-runner", False))
        self.assertEqual(r["fingerprint"], rs.cert_fingerprint(rs.STATE["cert"]))
        self.assertEqual(sorted(k for k in r["json"] if k in ("running", "max_jobs")), ["max_jobs", "running"])
        self.assertIsInstance(r["json"]["max_jobs"], int)

    def test_pairing_with_the_code_gives_a_token_once(self):
        code = rs.new_pair_code()
        fp = self.req("GET", "/health")["fingerprint"]
        r = self.req("POST", "/pair", {"proof": client.pairing_proof(code, fp)})
        self.assertEqual(r["status"], 200)
        self.assertTrue(r["json"]["token"])
        again = self.req("POST", "/pair", {"proof": client.pairing_proof(code, fp)})
        self.assertEqual(again["status"], 403)                       # the code is spent
        self.assertTrue(self.req("GET", "/health")["json"]["paired"])

    def test_a_proof_for_another_certificate_is_refused(self):
        code = rs.new_pair_code()
        r = self.req("POST", "/pair", {"proof": client.pairing_proof(code, "0" * 64)})   # what a man in the middle would send
        self.assertEqual(r["status"], 403)

    def test_five_wrong_tries_burn_the_code(self):
        code = rs.new_pair_code()
        fp = self.req("GET", "/health")["fingerprint"]
        for _ in range(5):
            self.assertEqual(self.req("POST", "/pair", {"proof": "bad"})["status"], 403)
        self.assertEqual(self.req("POST", "/pair", {"proof": client.pairing_proof(code, fp)})["status"], 403)

    def test_an_expired_code_is_refused(self):
        code = rs.new_pair_code()
        fp = self.req("GET", "/health")["fingerprint"]
        with mock.patch.object(rs.time, "time", return_value=rs.time.time() + rs.CODE_TTL_S + 5):
            self.assertEqual(self.req("POST", "/pair", {"proof": client.pairing_proof(code, fp)})["status"], 403)

    def test_everything_but_health_and_pair_needs_the_token(self):
        for path in ("/run", "/selftest"):
            self.assertEqual(self.req("POST", path, {"code": "print(1)"})["status"], 401)
            self.assertEqual(self.req("POST", path, {"code": "print(1)"}, token="nope")["status"], 401)

    def test_a_changed_certificate_is_caught_by_the_pin(self):
        with self.assertRaises(client.RunnerError):
            self.req("GET", "/health", pin="f" * 64)
        self.assertEqual(self.req("GET", "/health", pin=rs.cert_fingerprint(rs.STATE["cert"]))["status"], 200)


class JobTests(Live):
    def go(self, code, inputs=None, **kw):
        token, fp = self.paired()
        return self.req("POST", "/run", {"code": code, "inputs": inputs or [], **kw}, token=token, pin=fp)["json"]

    def test_stdout_and_a_written_file_come_back(self):
        r = self.go("print('hi')\nopen('out.txt','w').write('data')\n")
        self.assertTrue(r["ok"])
        self.assertEqual(r["stdout"].strip(), "hi")
        self.assertEqual([(f["name"], base64.b64decode(f["b64"])) for f in r["files"]], [("out.txt", b"data")])

    def test_input_files_are_readable_and_bad_names_are_refused(self):
        b64 = base64.b64encode(b"a,b\n1,2\n").decode()
        code = ("import os\np = '/in/data.csv' if os.path.exists('/in/data.csv') else '../in/data.csv'\n"
                "print(open(p).read().strip().replace(chr(10), ' '))\n")
        r = self.go(code, [{"name": "data.csv", "b64": b64}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["stdout"].strip(), "a,b 1,2")
        r = self.go("print(1)", [{"name": "../evil.py", "b64": b64}])
        self.assertFalse(r["ok"])
        self.assertIn("unusable name", r["error"])

    def test_a_failing_program_reports_its_error(self):
        r = self.go("raise ValueError('boom')\n")
        self.assertFalse(r["ok"])
        self.assertIn("ValueError: boom", r["stderr"])

    def test_an_endless_loop_is_killed(self):
        r = self.go("while True:\n    pass\n", timeout_s=5)
        self.assertTrue(r["timed_out"])
        self.assertFalse(r["ok"])

    def test_the_job_environment_has_no_secrets(self):
        r = self.go("import os\nprint(sorted(os.environ))\n")
        self.assertNotIn("TOKEN", r["stdout"].upper().replace("SYSTEMROOT", ""))

    def test_the_folder_is_wiped_afterwards(self):
        self.go("open('keep.txt','w').write('x')\n")
        import os
        import tempfile as tf
        self.assertEqual([p for p in os.listdir(tf.gettempdir()) if p.startswith("job_")], [])

    def test_too_many_inputs_and_empty_code_are_refused(self):
        self.assertFalse(self.go("print(1)", [{"name": f"f{i}.txt", "b64": ""} for i in range(25)])["ok"])
        self.assertFalse(self.go("   ")["ok"])

    def test_no_sandbox_means_no_code(self):
        token, fp = self.paired()
        rs.STATE["allow_unsandboxed"] = False
        try:
            r = self.req("POST", "/run", {"code": "print(1)"}, token=token, pin=fp)["json"]
        finally:
            rs.STATE["allow_unsandboxed"] = True
        self.assertFalse(r["ok"])
        self.assertIn("no working sandbox", r["error"])


class AdminFlowTests(Live):
    """The admin page's calls against the live runner, with the keychain and app.json replaced by dicts."""

    def setUp(self):
        super().setUp()
        self.tokens, self.conf = {}, {}
        patches = [
            mock.patch.object(config, "cfg", lambda: self.conf),
            mock.patch.object(config, "runner_url", lambda: str(self.conf.get("runner_url") or "")),
            mock.patch.object(config, "set_token", lambda url, t: self.tokens.__setitem__(url, t)),
            mock.patch.object(config, "get_token", lambda url=None: self.tokens.get(url or str(self.conf.get("runner_url") or ""))),
            mock.patch.object(config, "delete_token", lambda url=None: self.tokens.pop(url or str(self.conf.get("runner_url")), None)),
            mock.patch.object(config, "save", lambda **f: self.conf.update(f)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        admin._health_cache.update(at=0.0, url="", ok=False)

    def test_pair_then_status_then_forget(self):
        code = rs.new_pair_code()
        with mock.patch.object(admin, "selftest", mock.AsyncMock(return_value={"ok": True, "checks": []})):
            out = run(admin.pair(self.url, code))
        self.assertTrue(out["paired"] and out["enabled"])
        self.assertEqual(self.conf["fingerprint"], rs.cert_fingerprint(rs.STATE["cert"]))
        self.assertTrue(self.tokens[self.url])
        st = run(admin.status())
        self.assertTrue(st["configured"] and st["reachable"])
        self.assertFalse(st["ready"])                                # this runner has no bubblewrap: the app will not use it
        run_ready = run(admin.ready())
        self.assertFalse(run_ready)
        admin.forget()
        self.assertFalse(config.configured())
        self.assertEqual(self.tokens, {})

    def test_a_wrong_code_does_not_pair(self):
        rs.new_pair_code()
        with self.assertRaises(client.RunnerError):
            run(admin.pair(self.url, "AAAAA-AAAAA"))
        self.assertEqual(self.tokens, {})

    def test_enabling_needs_a_passed_selftest(self):
        with self.assertRaises(client.RunnerError):
            admin.set_enabled(True)

    def test_the_real_selftest_runs_against_the_live_runner(self):
        code = rs.new_pair_code()
        with mock.patch.object(admin, "selftest", mock.AsyncMock(return_value={"ok": False})):
            run(admin.pair(self.url, code))
        out = run(admin.selftest())
        names = {c["name"] for c in out["checks"]}
        self.assertEqual(names, {"sandbox", "no_network", "no_secrets", "writes_a_file", "libraries"})
        self.assertTrue(next(c for c in out["checks"] if c["name"] == "writes_a_file")["ok"])


class ConcurrencyTests(unittest.TestCase):
    """How many jobs run at once: bounded by --max-jobs, the rest wait for a slot and then get 'busy'."""
    SLEEP = "import time\ntime.sleep(3)\nprint('done')\n"

    def setUp(self):
        self.old_state = dict(rs.STATE)
        self.old_jobs = dict(rs._JOBS)
        rs.STATE.update(allow_unsandboxed=True, sandbox="none")

    def tearDown(self):
        rs.STATE.clear()
        rs.STATE.update(self.old_state)
        rs._JOBS.clear()
        rs._JOBS.update(self.old_jobs)

    def _burst(self, n):
        results, peak = [None] * n, [0]

        def one(i):
            results[i] = rs.run_job(self.SLEEP, [], 5)
        threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            peak[0] = max(peak[0], rs._JOBS["running"])
            threading.Event().wait(0.05)
        for t in threads:
            t.join()
        return results, peak[0]

    def test_at_most_max_jobs_run_together_and_extra_requests_wait(self):
        rs.set_max_jobs(2, wait_s=20)
        results, peak = self._burst(3)
        self.assertEqual(peak, 2)
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual(rs._JOBS["running"], 0)

    def test_a_request_that_waits_too_long_gets_busy(self):
        rs.set_max_jobs(1, wait_s=1)
        results, peak = self._burst(2)
        self.assertEqual(peak, 1)
        self.assertEqual(sorted(bool(r.get("busy")) for r in results), [False, True])
        self.assertEqual(rs._JOBS["running"], 0, "a refused request must not leave a slot taken")

    def test_the_setting_is_clamped(self):
        rs.set_max_jobs(0)
        self.assertEqual(rs._JOBS["max"], 1)
        rs.set_max_jobs(500)
        self.assertEqual(rs._JOBS["max"], 32)


class AddressTests(unittest.TestCase):
    def test_only_https_host_and_port(self):
        for bad in ("http://10.0.0.5:8470", "https://user:pw@10.0.0.5", "https://10.0.0.5/run", "ftp://x", "", "https://"):
            with self.assertRaises(client.RunnerError, msg=bad):
                client.parse_url(bad)
        self.assertEqual(client.parse_url("https://10.0.0.5:9000"), ("10.0.0.5", 9000))
        self.assertEqual(client.parse_url("https://runner.internal"), ("runner.internal", 8470))

    def test_public_and_metadata_addresses_are_refused(self):
        for host in ("8.8.8.8", "169.254.169.254", "0.0.0.0"):
            with self.assertRaises(client.RunnerError, msg=host):
                client.check_private(host)
        for host in ("127.0.0.1", "10.1.2.3", "192.168.0.7", "172.16.5.5"):
            client.check_private(host)
        client.check_private("8.8.8.8", allow_public=True)

    def test_the_proof_binds_code_and_certificate(self):
        a = client.pairing_proof("abcde-fghjk", "11" * 32)
        self.assertEqual(a, client.pairing_proof("ABCDE FGHJK", "11" * 32))      # case and punctuation do not matter
        self.assertNotEqual(a, client.pairing_proof("abcde-fghjk", "22" * 32))


class VettingTests(unittest.TestCase):
    """service.run with a fake runner answer: what is accepted back, and what is never sent."""

    def setUp(self):
        self.saved = []
        self.audit = []
        base = [
            mock.patch.object(admin, "ready", mock.AsyncMock(return_value=True)),
            mock.patch.object(config, "runner_url", lambda: "https://10.0.0.5:8470"),
            mock.patch.object(config, "get_token", lambda url=None: "[RUNNER_TOKEN]"),
            mock.patch.object(config, "fingerprint", lambda: "ab" * 32),
            mock.patch.object(service, "_save", lambda uid, name, data: (self.saved.append((name, data)), name)[1]),
            mock.patch("core.doc_store.audit", lambda *a, **k: self.audit.append(a)),
        ]
        for p in base:
            p.start()
            self.addCleanup(p.stop)
        service._fails.clear()
        service._runs.clear()

    def answer(self, **res):
        body = {"ok": True, "exit": 0, "stdout": "done", "stderr": "", "files": [], "ms": 12, **res}
        return mock.patch.object(client, "request", mock.AsyncMock(return_value={"status": 200, "json": body, "fingerprint": "x"}))

    @staticmethod
    def f(name, data=b"x"):
        return {"name": name, "b64": base64.b64encode(data).decode()}

    def test_allowed_files_are_saved_and_linked(self):
        with self.answer(files=[self.f("chart.png", b"\x89PNG"), self.f("data.csv", b"a,b\n1,2\n")]):
            out = run(service.run(1, "print(1)", []))
        self.assertEqual([n for n, _ in self.saved], ["chart.png", "data.csv"])
        self.assertIn("[DOWNLOAD: chart.png]", out["text"])

    def test_other_file_types_are_not_saved(self):
        with self.answer(files=[self.f("run.py"), self.f("tool.exe"), self.f("page.html"), self.f("pic.svg"), self.f("ok.txt")]):
            out = run(service.run(1, "print(1)", []))
        self.assertEqual([n for n, _ in self.saved], ["ok.txt"])
        self.assertIn("file type not allowed", out["text"])

    def test_a_card_number_in_an_output_is_never_saved(self):
        pan = "41111111" + "11111111"                        # the published Visa test number, built so no source line holds it
        with self.answer(files=[self.f("leak.csv", f"card\n{pan}\n".encode()), self.f("fine.csv", b"a\n1\n")]):
            out = run(service.run(1, "print(1)", []))
        self.assertEqual([n for n, _ in self.saved], ["fine.csv"])
        self.assertIn("contains a card number", out["text"])

    def test_output_text_is_masked(self):
        pan = "41111111" + "11111111"
        with self.answer(stdout=f"card {pan}"):
            out = run(service.run(1, "print(1)", []))
        self.assertNotIn(pan, out["text"])

    def test_code_with_a_card_number_is_not_sent(self):
        pan = "41111111" + "11111111"
        with self.answer() as m, self.assertRaises(service.SandboxRefused):
            run(service.run(1, f"x = '{pan}'", []))
        m.assert_not_called()

    def test_three_failures_stop_the_retries(self):
        with self.answer(ok=False, exit=1, stderr="Traceback ... boom"):
            for _ in range(3):
                out = run(service.run(1, "raise SystemExit(1)", []))
                self.assertIn("boom", out["text"])
            with self.assertRaises(service.SandboxRefused):
                run(service.run(1, "print(1)", []))
        with self.answer():
            run(service.run(2, "print(1)", []))                         # another user is not affected

    def test_an_unavailable_runner_fails_closed(self):
        with mock.patch.object(admin, "ready", mock.AsyncMock(return_value=False)), self.answer() as m:
            with self.assertRaises(service.SandboxRefused):
                run(service.run(1, "print(1)", []))
        m.assert_not_called()

    def test_the_audit_log_holds_a_hash_not_the_code(self):
        with self.answer():
            run(service.run(1, "print('secret plan')", []))
        detail = next(a for a in self.audit if a[1] == "sandbox.run")[3]
        self.assertEqual(detail["code_sha256"], hashlib.sha256(b"print('secret plan')").hexdigest())
        self.assertNotIn("secret plan", str(self.audit))

    def test_runs_are_rate_limited_per_user(self):
        with self.answer():
            for _ in range(service.RATE_PER_10_MIN):
                run(service.run(1, "print(1)", []))
            with self.assertRaises(service.SandboxRefused):
                run(service.run(1, "print(1)", []))


class ToolTests(unittest.TestCase):
    def test_the_tool_is_wanted_only_for_custom_code(self):
        self.assertTrue(tool.sandbox_wanted("write a python script that plots the sales"))
        self.assertTrue(tool.sandbox_wanted("make a histogram of the ages"))
        self.assertTrue(tool.sandbox_wanted("merge these two files", has_data_attachment=True))
        self.assertFalse(tool.sandbox_wanted("merge these two files"))
        self.assertFalse(tool.sandbox_wanted("create a PowerPoint about onboarding"))
        self.assertFalse(tool.sandbox_wanted("what is the capital of France"))

    def test_a_refusal_comes_back_as_an_error_line_not_a_traceback(self):
        with mock.patch.object(admin, "ready", mock.AsyncMock(return_value=False)):
            out = run(tool.tool_run_sandbox_python({"code": "print(1)"}, 1))
        self.assertTrue(out.startswith("error: "))
        self.assertNotIn("Traceback", out)

    def test_the_schema_asks_for_code(self):
        fn = tool.RUN_SANDBOX_SCHEMA["function"]
        self.assertEqual(fn["name"], "run_sandbox_python")
        self.assertEqual(fn["parameters"]["required"], ["code"])


if __name__ == "__main__":
    unittest.main()


class WiringTests(unittest.TestCase):
    """Source-level checks that the pieces are connected the way the design says."""

    def test_chat_offers_the_tool_only_when_the_runner_answers(self):
        src = (ROOT / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn("sandbox_wanted(own_q, has_data_attachment) and await _sandbox.available()", src)
        self.assertIn('t_name == "run_sandbox_python" and sandbox_on', src)

    def test_every_admin_route_needs_users_manage_with_step_up(self):
        src = (ROOT / "routes" / "sandbox.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("@router."), src.count('require_verified("users.manage")'))

    def test_the_app_never_runs_code_itself(self):
        import re
        for p in (ROOT / "core" / "sandbox").glob("*.py"):
            text = p.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\b(subprocess|os\.system|exec\(|eval\(|__import__\(\"subprocess)", text), p.name)

    def test_the_runner_script_is_stdlib_only(self):
        import ast
        tree = ast.parse((ROOT / "tools" / "runner" / "runner_server.py").read_text(encoding="utf-8"))
        names = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertTrue(names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names))
