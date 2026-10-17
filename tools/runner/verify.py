#!/usr/bin/env python3
"""Does the runner's sandbox really contain hostile code? Run this on the Linux host before the runner is ever enabled.

    python3 tools/runner/verify.py             run it as the runner's own unprivileged user if you can (not root)

It needs bubblewrap and python3 and nothing else: no server, no network, no pairing. Every check sends a small hostile Python
program through the runner's own run_job() and prints PASS or FAIL. Exit code 0 only when every critical check passed.
Any FAIL is a real isolation gap: do not enable the runner until it is fixed. Standard library only.
"""
import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_runner(path: Path = HERE / "runner_server.py"):
    spec = importlib.util.spec_from_file_location("runner_server", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["runner_server"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---- the hostile programs (each runs INSIDE the sandbox) ---------------------------------------------------------------
HOST_FILES = ("import os\n"
              "paths = ['/etc/shadow', '/etc/passwd', '/root', '/home', '/var', '/srv', '/opt', %r]\n"
              "print('VISIBLE ' + ','.join(p for p in paths if os.path.exists(p)) if any(os.path.exists(p) for p in paths) else 'HIDDEN')\n")
READ_ONLY = ("import os\n"
             "bad = []\n"
             "for d in ('/usr', '/bin', '/lib', '/etc', '/proc/sys'):\n"
             "    try:\n        open(d + '/verify_probe', 'w').close()\n        bad.append(d)\n    except OSError:\n        pass\n"
             "print('READONLY' if not bad else 'WRITABLE ' + ','.join(bad))\n")
SYMLINK = ("import os\n"
           "os.symlink('/usr/bin/python3', 'link.bin')\n"
           "open('real.txt', 'w').write('x')\n"
           "print('done')\n")
FORK_BOMB = ("import os, time\n"
             "pids = []\n"
             "for i in range(600):\n"
             "    try:\n        pid = os.fork()\n    except OSError:\n        break\n"
             "    if pid == 0:\n        time.sleep(30)\n        os._exit(0)\n"
             "    pids.append(pid)\n"
             "print('FORKED', len(pids))\n"
             "for p in pids:\n    try:\n        os.kill(p, 9)\n    except OSError:\n        pass\n")
LOOP = "while True:\n    pass\n"
MEMORY = "x = bytearray(3 * 1024 * 1024 * 1024)\nprint('ALLOCATED')\n"
BIG_FILE = ("chunk = bytes(1024 * 1024)\n"
            "with open('big.bin', 'wb') as f:\n    for _ in range(100):\n        f.write(chunk)\n"
            "print('WROTE')\n")
MANY_FILES = "for i in range(40):\n    open('f%d.txt' % i, 'w').write('x')\nprint('done')\n"
SLEEP = "import time\ntime.sleep(3)\nprint('done')\n"
ENV_CANARY = "import os\nprint(os.environ.get('RUNNER_VERIFY_SECRET_TOKEN', 'ABSENT'))\n"


def _job_dirs() -> set:
    base = Path(os.environ.get("RUNNER_TMP") or tempfile.gettempdir())
    try:
        return {p.name for p in base.iterdir() if p.name.startswith("job_")}
    except OSError:
        return set()


# ---- the checks --------------------------------------------------------------------------------------------------------
def build_checks(rs) -> list:
    """[(name, critical, what it proves, callable(rs) -> (ok, detail))]. Pure data, so the logic is testable without bubblewrap."""

    def job(code, timeout=15):
        return rs.run_job(code, [], timeout)

    def out(r):
        return (r.get("stdout") or "").strip()

    def no_network(rs):
        r = job(rs.SELFTEST_NET, 20)
        return r.get("ok") and out(r) == "NET_BLOCKED", out(r) or r.get("error", "")

    def env_clean(rs):
        os.environ["RUNNER_VERIFY_SECRET_TOKEN"] = "canary-not-a-secret"
        try:
            r = job(ENV_CANARY)
        finally:
            os.environ.pop("RUNNER_VERIFY_SECRET_TOKEN", None)
        return r.get("ok") and out(r) == "ABSENT", out(r) or r.get("error", "")

    def host_files_hidden(rs):
        r = job(HOST_FILES % str(rs.STATE["dir"]))
        return r.get("ok") and out(r) == "HIDDEN", out(r) or r.get("error", "")

    def system_read_only(rs):
        r = job(READ_ONLY)
        return r.get("ok") and out(r) == "READONLY", out(r) or r.get("error", "")

    def symlink_not_returned(rs):
        r = job(SYMLINK)
        names = [f["name"] for f in r.get("files", [])]
        return r.get("ok") and names == ["real.txt"], "returned " + ",".join(names)

    def fork_bomb(rs):
        t0 = time.time()
        r = job(FORK_BOMB, 20)
        text = out(r)
        n = int(text.split()[-1]) if text.startswith("FORKED") and text.split()[-1].isdigit() else -1
        return (n >= 0 and n < 200 and time.time() - t0 < 30), (text or r.get("error") or r.get("stderr", ""))[:120]

    def endless_loop(rs):
        t0 = time.time()
        r = job(LOOP, 5)
        return bool(r.get("timed_out")) and time.time() - t0 < 25, f"timed_out={r.get('timed_out')} in {time.time() - t0:.0f}s"

    def memory_bomb(rs):
        r = job(MEMORY, 30)
        return (not r.get("ok")) and "ALLOCATED" not in out(r), (r.get("stderr") or out(r) or "")[-100:].replace("\n", " ")

    def oversize_file(rs):
        r = job(BIG_FILE, 30)
        big = [f for f in r.get("files", []) if len(f["b64"]) * 3 // 4 - f["b64"].count("=") > rs.MAX_OUTPUT_FILE_BYTES]
        return not big and not (r.get("ok") and out(r) == "WROTE" and r.get("files")), f"ok={r.get('ok')} files={len(r.get('files', []))}"

    def output_caps(rs):
        r = job(MANY_FILES)
        n = len(r.get("files", []))
        return r.get("ok") and n <= rs.MAX_OUTPUT_FILES, f"{n} files returned (cap {rs.MAX_OUTPUT_FILES})"

    def folder_wiped(rs):
        before = _job_dirs()
        job("open('x.txt', 'w').write('x')\nprint('done')\n")
        left = _job_dirs() - before
        return not left, "left behind: " + ",".join(sorted(left)) if left else "clean"

    def concurrency(rs):
        old = dict(rs._JOBS)
        rs.set_max_jobs(1, wait_s=1)
        results = [None, None]
        try:
            ts = [threading.Thread(target=lambda i=i: results.__setitem__(i, rs.run_job(SLEEP, [], 10))) for i in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        finally:
            rs._JOBS.update(old)
            rs.set_max_jobs(old["max"], old["wait_s"])
        busy = sorted(bool((r or {}).get("busy")) for r in results)
        return busy == [False, True], f"busy flags {busy}"

    def libraries(rs):
        r = job(rs.SELFTEST_LIBS, 30)
        return r.get("ok") and out(r) == "ALL", out(r) or r.get("error", "")

    return [
        ("no_network", True, "a job cannot open a network connection", no_network),
        ("no_secret_env", True, "the runner's environment is not passed to a job", env_clean),
        ("host_files_hidden", True, "/etc, /root, /home, /var and the runner state folder do not exist for a job", host_files_hidden),
        ("system_read_only", True, "a job cannot write to /usr, /bin, /lib, /etc", system_read_only),
        ("symlink_not_returned", True, "a symlink in the output folder is not sent back", symlink_not_returned),
        ("fork_bomb_contained", True, "process creation is capped", fork_bomb),
        ("endless_loop_killed", True, "a job is killed at its time limit", endless_loop),
        ("memory_bomb_stopped", True, "a job cannot allocate beyond its memory cap", memory_bomb),
        ("oversize_file_stopped", True, "a job cannot return a file above the size cap", oversize_file),
        ("output_file_count_capped", True, "at most the allowed number of files come back", output_caps),
        ("job_folder_wiped", True, "nothing is left behind after a job", folder_wiped),
        ("concurrency_capped", True, "with max-jobs 1 a second request gets busy", concurrency),
        ("libraries_present", False, "the document libraries are installed", libraries),
    ]


def run_checks(rs, checks=None) -> list:
    results = []
    for name, critical, what, fn in (checks if checks is not None else build_checks(rs)):
        try:
            ok, detail = fn(rs)
            ok = bool(ok)
        except Exception as e:                 # a check that crashes is a failed check, never a pass
            ok, detail = False, f"{type(e).__name__}: {str(e)[:100]}"
        results.append({"name": name, "critical": critical, "what": what, "ok": ok, "detail": str(detail)[:140]})
    return results


def render(results) -> str:
    width = max(len(r["name"]) for r in results)
    lines = []
    for r in results:
        tag = "PASS" if r["ok"] else ("FAIL" if r["critical"] else "WARN")
        lines.append(f"{tag}  {r['name']:<{width}}  {r['what']}" + ("" if r["ok"] else f"\n      -> {r['detail']}"))
    failed = [r for r in results if r["critical"] and not r["ok"]]
    lines.append("")
    lines.append("RESULT: every critical check passed" if not failed else f"RESULT: {len(failed)} critical check(s) FAILED - do not enable this runner")
    return "\n".join(lines)


def exit_code(results) -> int:
    return 0 if all(r["ok"] for r in results if r["critical"]) else 1


def main() -> int:
    if not sys.platform.startswith("linux"):
        print("verify.py needs Linux (bubblewrap is Linux only). On Windows use WSL2; see docs/RUNNER.md.")
        return 2
    rs = load_runner()
    rs.STATE["sandbox"] = rs.detect_sandbox()
    if rs.STATE["sandbox"] != "bwrap":
        print("bubblewrap is not usable here, so nothing can be verified. Install it (apt install bubblewrap) and make sure\n"
              "unprivileged user namespaces are allowed, then run this again.")
        return 2
    if os.geteuid() == 0:
        print("note: running as root; run this as the runner's unprivileged user for a faithful result (root ignores some limits).\n")
    rs.STATE["dir"] = Path(tempfile.mkdtemp(prefix="verify_state_"))
    results = run_checks(rs)
    print(render(results))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
