#!/usr/bin/env python3
"""a770 code runner - runs model-written Python for the chat app on THIS machine, never on the app server.

Standard library only, so a fresh Alpine VM needs nothing but python3 and the document libraries. It is meant to live on a
VM of its own: a private network, no internet, no secrets, nothing of the application on it. Every job runs in a throwaway
folder, as a separate process inside bubblewrap (no network, no view of this machine) with CPU / memory / process / file-size
limits, and the folder is wiped afterwards.

    python3 runner_server.py serve            start the service (https, self-signed certificate)
    python3 runner_server.py pair-code        print a one-time pairing code (valid 10 minutes) for the app's Settings page
    python3 runner_server.py fingerprint      print the certificate fingerprint

Pairing: the app sends HMAC(code, fingerprint of the certificate it was shown). A man in the middle presents a different
certificate, so the proof does not match and the runner refuses. The app then pins the fingerprint and keeps the token.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = 1
CODE_TTL_S = 600
CODE_MAX_ATTEMPTS = 5
MAX_CODE_BYTES = 200_000
MAX_INPUT_FILES = 20
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_FILES = 20
MAX_OUTPUT_FILE_BYTES = 25 * 1024 * 1024
MAX_OUTPUT_BYTES = 60 * 1024 * 1024
MAX_REQUEST_BYTES = 90 * 1024 * 1024
TEXT_CAP = 8000
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 180
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,99}$")
_LIBS = ("openpyxl", "pptx", "docx", "PIL", "lxml", "matplotlib")

STATE = {"dir": Path("/var/lib/a770-runner"), "cert": None, "allow_unsandboxed": False, "sandbox": "none"}
_job_lock = threading.Lock()
_state_lock = threading.Lock()


# ---- state: pairing code, token ---------------------------------------------------------------------------------------
def _path(name: str) -> Path:
    return STATE["dir"] / name


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def new_pair_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(10))
    code = raw[:5] + "-" + raw[5:]
    with _state_lock:
        _write_private(_path("pair.json"), json.dumps({"code": code, "expires": time.time() + CODE_TTL_S, "attempts": 0}))
    return code


def _norm_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def cert_fingerprint(cert_path: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(Path(cert_path).read_text(encoding="ascii"))
    return hashlib.sha256(der).hexdigest()


def pair(proof: str, fingerprint: str):
    """-> (token, error). The proof must be HMAC-SHA256(key=normalised code, msg=this certificate's fingerprint)."""
    with _state_lock:
        st = _read_json(_path("pair.json"))
        if not st.get("code") or time.time() > float(st.get("expires") or 0):
            return None, "no pairing code is active: run `runner_server.py pair-code` on the runner"
        if int(st.get("attempts") or 0) >= CODE_MAX_ATTEMPTS:
            return None, "too many wrong attempts: run `runner_server.py pair-code` again"
        expected = hmac.new(_norm_code(st["code"]).encode(), fingerprint.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(proof or "")):
            st["attempts"] = int(st.get("attempts") or 0) + 1
            _write_private(_path("pair.json"), json.dumps(st))
            return None, "the pairing code does not match"
        token = secrets.token_urlsafe(32)
        _write_private(_path("token.sha256"), hashlib.sha256(token.encode()).hexdigest())
        try:
            _path("pair.json").unlink()                       # one use
        except OSError:
            pass
        return token, ""


def token_ok(header: str) -> bool:
    m = re.match(r"^Bearer\s+(\S+)$", str(header or ""))
    if not m:
        return False
    try:
        want = _path("token.sha256").read_text(encoding="ascii").strip()
    except OSError:
        return False
    return bool(want) and hmac.compare_digest(want, hashlib.sha256(m.group(1).encode()).hexdigest())


# ---- sandbox ---------------------------------------------------------------------------------------------------------
def detect_sandbox() -> str:
    """'bwrap' when bubblewrap can start a no-network container here, else 'none'."""
    exe = shutil.which("bwrap")
    if not exe or not sys.platform.startswith("linux"):
        return "none"
    try:
        r = subprocess.run([exe, "--unshare-all", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "true"],
                           capture_output=True, timeout=10)
        return "bwrap" if r.returncode == 0 else "none"
    except (OSError, subprocess.SubprocessError):
        return "none"


def _limits(timeout_s: int):
    if not sys.platform.startswith("linux"):
        return None
    import resource

    def apply():
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (timeout_s + 5, timeout_s + 10))
        resource.setrlimit(resource.RLIMIT_AS, (1400 * 1024 * 1024, 1400 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NPROC, (96, 96))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_FILE_BYTES, MAX_OUTPUT_FILE_BYTES))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def _command(work: Path, inp: Path) -> list:
    py = sys.executable or "python3"
    if STATE["sandbox"] == "bwrap":
        cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
               "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib", "--ro-bind", "/bin", "/bin",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
               "--bind", str(work), "/work", "--ro-bind", str(inp), "/in", "--chdir", "/work",
               "--setenv", "HOME", "/work", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
               "--setenv", "MPLBACKEND", "Agg", "--setenv", "MPLCONFIGDIR", "/tmp"]
        if Path("/lib64").exists():
            cmd[cmd.index("--proc"):cmd.index("--proc")] = ["--ro-bind", "/lib64", "/lib64"]
        return cmd + ["/usr/bin/python3", "-I", "main.py"]
    return [py, "-I", "main.py"]            # only reachable with --allow-unsandboxed (tests)


def _job_env() -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if os.environ.get("SYSTEMROOT"):                   # Windows (tests only): Python will not start without it
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _tail(text: bytes, cap: int = TEXT_CAP) -> str:
    s = text.decode("utf-8", "replace")
    return s if len(s) <= cap else "..." + s[-cap:]


def _clean_name(name) -> str:
    n = Path(str(name or "")).name.strip()
    return n if _SAFE_NAME.match(n) and not n.startswith(".") else ""


def run_job(code: str, inputs: list, timeout_s: int) -> dict:
    if STATE["sandbox"] != "bwrap" and not STATE["allow_unsandboxed"]:
        return {"ok": False, "error": "this runner has no working sandbox (bubblewrap): it refuses to run code"}
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return {"ok": False, "error": "code is empty or too large"}
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUT_FILES:
        return {"ok": False, "error": f"at most {MAX_INPUT_FILES} input files"}
    timeout_s = max(5, min(int(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    if not _job_lock.acquire(timeout=30):
        return {"ok": False, "error": "the runner is busy: try again in a moment", "busy": True}
    root = Path(tempfile.mkdtemp(prefix="job_", dir=os.environ.get("RUNNER_TMP") or None))
    try:
        work, inp = root / "work", root / "in"
        work.mkdir()
        inp.mkdir()
        total = 0
        for item in inputs:
            raw_name = str((item or {}).get("name") or "")
            name = _clean_name(raw_name)
            if not name or name != raw_name:                    # a bare file name only: no folders, no ../
                return {"ok": False, "error": "an input file has an unusable name"}
            try:
                data = base64.b64decode(str((item or {}).get("b64") or ""), validate=True)
            except ValueError:
                return {"ok": False, "error": f"input {name} is not valid base64"}
            total += len(data)
            if total > MAX_INPUT_BYTES:
                return {"ok": False, "error": "input files are too large"}
            (inp / name).write_bytes(data)
        (work / "main.py").write_text(code, encoding="utf-8")
        before = {p.name for p in work.iterdir()}
        t0 = time.time()
        timed_out = False
        proc = subprocess.Popen(_command(work, inp), cwd=str(work), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=_job_env(),
                                preexec_fn=_limits(timeout_s))
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, 9) if hasattr(os, "killpg") else proc.kill()
            except OSError:
                proc.kill()
            out, err = proc.communicate()
        files, size = [], 0
        for p in sorted(work.iterdir()):
            if p.name in before or p.is_symlink() or not p.is_file() or not _clean_name(p.name):
                continue
            n = p.stat().st_size
            if len(files) >= MAX_OUTPUT_FILES or n > MAX_OUTPUT_FILE_BYTES or size + n > MAX_OUTPUT_BYTES:
                continue
            size += n
            files.append({"name": p.name, "b64": base64.b64encode(p.read_bytes()).decode("ascii")})
        return {"ok": proc.returncode == 0 and not timed_out, "exit": proc.returncode, "timed_out": timed_out,
                "stdout": _tail(out), "stderr": _tail(err), "files": files, "ms": int((time.time() - t0) * 1000)}
    finally:
        shutil.rmtree(root, ignore_errors=True)
        _job_lock.release()


SELFTEST_NET = ("import socket\n"
                "try:\n    socket.create_connection(('1.1.1.1', 53), 3)\n    print('NET_OPEN')\n"
                "except OSError:\n    print('NET_BLOCKED')\n")
SELFTEST_SECRETS = ("import os\n"
                    "bad = [k for k in os.environ if any(w in k.upper() for w in ('TOKEN', 'SECRET', 'KEY', 'PASSWORD'))]\n"
                    "seen = [p for p in ('/etc/shadow', '/var/lib/a770-runner', '/root') if os.path.exists(p)]\n"
                    "print('CLEAN' if not bad and not seen else 'LEAK ' + repr(bad + seen))\n")
SELFTEST_HELLO = "open('hello.txt', 'w').write('hello from the runner')\nprint('done')\n"
SELFTEST_LIBS = ("import importlib\nmiss = []\nfor m in %r:\n    try:\n        importlib.import_module(m)\n"
                 "    except Exception:\n        miss.append(m)\nprint('MISSING ' + ','.join(miss) if miss else 'ALL')\n" % (_LIBS,))


def selftest() -> dict:
    checks = []

    def go(name, code, want, critical=True):
        r = run_job(code, [], 30)
        out = (r.get("stdout") or "").strip()
        checks.append({"name": name, "ok": bool(r.get("ok")) and want(out, r), "critical": critical, "detail": (out or r.get("error") or r.get("stderr") or "")[:200]})

    checks.append({"name": "sandbox", "ok": STATE["sandbox"] == "bwrap" or STATE["allow_unsandboxed"], "critical": True,
                   "detail": STATE["sandbox"]})
    go("no_network", SELFTEST_NET, lambda o, r: o == "NET_BLOCKED")
    go("no_secrets", SELFTEST_SECRETS, lambda o, r: o == "CLEAN")
    go("writes_a_file", SELFTEST_HELLO, lambda o, r: any(f["name"] == "hello.txt" for f in r.get("files", [])))
    go("libraries", SELFTEST_LIBS, lambda o, r: o == "ALL", critical=False)
    return {"ok": all(c["ok"] for c in checks if c["critical"]), "checks": checks}


# ---- http ------------------------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "a770-runner"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):          # no request bodies, no codes in logs
        sys.stderr.write("[runner] %s %s\n" % (self.command, self.path.split("?")[0]))

    def _send(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        obj = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(obj, dict):
            raise ValueError("body must be an object")
        return obj

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._send(200, {"ok": True, "service": "a770-runner", "version": VERSION, "sandbox": STATE["sandbox"],
                             "paired": _path("token.sha256").exists()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": str(e)[:200]})
            return
        if path == "/pair":
            token, err = pair(body.get("proof"), STATE["fingerprint"])
            self._send(200 if token else 403, {"token": token} if token else {"error": err})
            return
        if not token_ok(self.headers.get("Authorization")):
            self._send(401, {"error": "not paired"})
            return
        try:
            if path == "/run":
                self._send(200, run_job(body.get("code"), body.get("inputs") or [], body.get("timeout_s")))
            elif path == "/selftest":
                self._send(200, selftest())
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:                  # a bad request must not take the service down or leak a traceback
            sys.stderr.write(f"[runner] {path} failed: {type(e).__name__}\n")
            self._send(500, {"ok": False, "error": "the runner could not process that request"})


def make_server(host: str, port: int, cert: Path, key: Path) -> ThreadingHTTPServer:
    STATE["cert"] = Path(cert)
    STATE["fingerprint"] = cert_fingerprint(cert)
    httpd = ThreadingHTTPServer((host, port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("serve", "pair-code", "fingerprint"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8470)
    ap.add_argument("--state-dir", default=str(STATE["dir"]))
    ap.add_argument("--cert")
    ap.add_argument("--key")
    ap.add_argument("--allow-unsandboxed", action="store_true", help="tests only: run jobs without bubblewrap")
    a = ap.parse_args(argv)
    STATE["dir"] = Path(a.state_dir)
    cert = Path(a.cert or _path("cert.pem"))
    key = Path(a.key or _path("key.pem"))
    if a.command == "pair-code":
        print(new_pair_code())
        return 0
    if a.command == "fingerprint":
        print(cert_fingerprint(cert))
        return 0
    STATE["allow_unsandboxed"] = bool(a.allow_unsandboxed)
    STATE["sandbox"] = detect_sandbox()
    if STATE["sandbox"] != "bwrap" and not a.allow_unsandboxed:
        print("[runner] WARNING: bubblewrap is not usable here - /run will refuse every job", file=sys.stderr)
    httpd = make_server(a.host, a.port, cert, key)
    print(f"[runner] listening on https://{a.host}:{a.port}  sandbox={STATE['sandbox']}  fingerprint={STATE['fingerprint']}")
    if not _path("token.sha256").exists():
        print(f"[runner] not paired yet. Pairing code (10 minutes): {new_pair_code()}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
