#!/usr/bin/env python3
"""
scripts/run_swebench.py - run SWE-bench style instances against the agent loop and grade the patch.

The external-benchmark counterpart to the self-authored live suite (tests/eval_agent.py --live).
Per instance it:
  1. clones the repo (cached) and checks out `base_commit` into a fresh folder under --work-root,
  2. applies the instance's `test_patch` and confirms the FAIL_TO_PASS tests really fail there
     (an instance whose tests cannot run in this environment is reported `env_error` and the
     agent is NOT run: it would be graded on noise),
  3. points the logged-in user's eval project at that folder and drives `/agent/run` with the issue,
  4. extracts the agent's patch with `git diff`,
  5. re-applies `test_patch` on top of it, runs the test files and grades the result
     (grade_resolved: every FAIL_TO_PASS green, no PASS_TO_PASS regressed).

Honest limits:
  * Tests run with --test-cmd in THIS machine's environment, not SWE-bench's per-repo Docker images.
    Instances whose dependencies are not installed there come out `env_error`, never "resolved". The
    predictions file (predictions.jsonl) is in the official format so the same patches can be graded by
    the official harness elsewhere.
  * Only pytest style ids (`path/to/test.py::name`) are graded.
  * The agent runs with permission_mode=bypass (no approval cards), so shell commands run unasked on this
    machine inside the checkout. --allow-bypass is required, and the Companion must be on this machine.

Dataset: SWE-bench Lite is not bundled. Export it yourself to JSONL (fields: instance_id, repo,
base_commit, problem_statement, test_patch, FAIL_TO_PASS, PASS_TO_PASS), e.g. from the Hugging Face
dataset princeton-nlp/SWE-bench_Lite.

Usage:
  python scripts/run_swebench.py lite.jsonl --live-user EVAL --live-password [PASSWORD] \\
      --work-root C:/swe_ws --allow-bypass --limit 30 \\
      --test-cmd "C:/venvs/swe/Scripts/python.exe -m pytest -v -p no:cacheprovider"
"""

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

MODEL_NAME = "ssl-local-agent"
DEFAULT_TEST_CMD = f"{sys.executable} -m pytest -v -p no:cacheprovider"
_LIST_FIELDS = ("FAIL_TO_PASS", "PASS_TO_PASS")
_PATCH_EXCLUDES = [":(exclude)**/__pycache__/**", ":(exclude)*.pyc", ":(exclude).pytest_cache/**",
                   ":(exclude)**/*.egg-info/**"]


# ---------------------------------------------------------------- instances

def normalize_instance(d: dict) -> dict:
    """The Hugging Face dump stores FAIL_TO_PASS / PASS_TO_PASS as JSON-encoded strings; accept both."""
    for key in _LIST_FIELDS:
        v = d.get(key)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                v = [v] if v.strip() else []
        d[key] = list(v or [])
    return d


def load_instances(path: str | Path) -> list:
    """Parse a SWE-bench JSONL file into a list of instance dicts.

    Drops junk rows with a stderr note so one malformed instance cannot kill the run.
    """
    out = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[swebench] line {line_no}: bad JSON ({e}); skipped", file=sys.stderr)
                continue
            if not isinstance(d, dict) or not d.get("instance_id"):
                print(f"[swebench] line {line_no}: no instance_id; skipped", file=sys.stderr)
                continue
            out.append(normalize_instance(d))
    return out


def build_prompt(inst: dict, limit: int = 12000) -> str:
    """The task the agent sees: the issue text and what is expected of it. Never the tests or gold patch."""
    issue = (inst.get("problem_statement") or "").strip()
    if len(issue) > limit:
        issue = issue[:limit] + "\n[issue truncated]"
    return ("Fix the following issue in the repository in the current folder. Find the cause in the source, "
            "make the smallest change that fixes it, and do not modify existing tests. Reproduce the problem "
            "first when you can, and run the relevant tests after your change.\n\n"
            f"<issue>\n{issue}\n</issue>")


# ------------------------------------------------------------------ grading

_RESULT_RX = re.compile(
    r"^(\S+\.py::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[\s*\d+%\])?\s*$")
_SUMMARY_RX = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+\.py::\S.*?)(?:\s+-\s.*)?$")


def parse_test_report(output: str) -> dict:
    """Extract test names + pass/fail from pytest output.

    Reads both the verbose per-test lines (`tests/test_x.py::test_y PASSED  [ 50%]`) and the `-rA` summary
    lines (`PASSED tests/test_x.py::test_y`). Returns {tests, passed, failed}.
    """
    names, passed, failed = set(), set(), set()
    for ln in output.splitlines():
        ln = ln.strip()
        m = _RESULT_RX.match(ln)
        if m:
            name, status = m.group(1), m.group(2)
        else:
            m = _SUMMARY_RX.match(ln)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
        names.add(name)
        (passed if status in ("PASSED", "XPASS") else failed).add(name)
    # a test reported twice (verbose + summary) keeps its worst outcome
    passed -= failed
    return {"tests": sorted(names), "passed": sorted(passed), "failed": sorted(failed)}


def grade_resolved(instance: dict, observed_pass: set) -> Optional[bool]:
    """SWE-bench 'resolved': every FAIL_TO_PASS now green and no PASS_TO_PASS regressed.

    None when the instance lists no expected tests at all (no evidence either way).
    """
    want = set(instance.get("FAIL_TO_PASS") or [])
    keep = set(instance.get("PASS_TO_PASS") or [])
    if not want and not keep:
        return None
    if not want or not (observed_pass & want) >= want:
        return False
    if keep and (keep - observed_pass):
        return False
    return True


def summarize(instances: list) -> dict:
    """Wilson-interval rollup of resolved instances (same stat as the repo's eval harness)."""
    from eval_mock import wilson_ci
    n = len(instances)
    resolved = sum(1 for i in instances if i.get("_resolved"))
    lo, hi = wilson_ci(resolved, n)
    return {"instances": n, "resolved": resolved,
            "pass_rate": round(resolved / n, 3) if n else 0.0, "ci95": [lo, hi]}


def summarize_run(records: list) -> dict:
    """Roll up per-instance run records. Instances that could not be graded (env_error) are reported
    separately and excluded from the pass rate, so a broken environment cannot read as an agent failure."""
    from eval_mock import wilson_ci
    graded = [r for r in records if r.get("status") != "env_error"]
    n = len(graded)
    resolved = sum(1 for r in graded if r.get("resolved") is True)
    lo, hi = wilson_ci(resolved, n)

    def rate(pred) -> float:
        return round(sum(1 for r in graded if pred(r)) / n, 3) if n else 0.0

    return {"instances": len(records), "graded": n,
            "env_errors": len(records) - n,
            "resolved": resolved,
            "resolved_rate": round(resolved / n, 3) if n else 0.0, "ci95": [lo, hi],
            "patch_rate": rate(lambda r: bool(r.get("patch") or r.get("patch_len"))),
            "clean_termination_rate": rate(lambda r: r.get("done_state") == "completed"),
            "avg_tool_calls": round(sum(len(r.get("tools") or []) for r in graded) / n, 1) if n else 0.0,
            "avg_duration_s": round(sum(r.get("duration_s") or 0 for r in graded) / n, 1) if n else 0.0}


# ---------------------------------------------------------------------- git

def git(args: list, cwd: Path, timeout: int = 600, input_text: str = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, input=input_text)


def rmtree(path: Path) -> None:
    """shutil.rmtree that also removes git's read-only object files (a plain rmtree leaves them on Windows)."""
    def _writable_then_retry(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_writable_then_retry)


def _slug(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", repo)


def ensure_repo_cache(repo: str, cache_root: Path) -> Path:
    """One full clone per repo, reused by every instance. `repo` may be owner/name or a local path/URL."""
    if Path(repo).exists():
        return Path(repo)
    url = repo if "://" in repo else f"https://github.com/{repo}.git"
    dest = cache_root / _slug(repo)
    if not dest.exists():
        cache_root.mkdir(parents=True, exist_ok=True)
        r = git(["clone", "--quiet", url, str(dest)], None, timeout=3600)
        if r.returncode != 0:
            rmtree(dest)
            raise RuntimeError(f"clone {url} failed: {r.stderr.strip()[:300]}")
    return dest


def prepare_checkout(inst: dict, cache: Path, work_root: Path) -> Path:
    """Fresh working copy of the repo at base_commit, with a local git identity for the agent."""
    dest = work_root / _slug(inst["instance_id"])
    if dest.exists():
        rmtree(dest)
    r = git(["clone", "--quiet", "--shared", "--no-checkout", str(cache), str(dest)], None)
    if r.returncode != 0:
        raise RuntimeError(f"local clone failed: {r.stderr.strip()[:300]}")
    r = git(["checkout", "--quiet", inst["base_commit"]], dest)
    if r.returncode != 0:
        raise RuntimeError(f"checkout {inst['base_commit']} failed: {r.stderr.strip()[:300]}")
    git(["config", "user.name", "swe-agent"], dest)
    git(["config", "user.email", "swe-agent@localhost"], dest)
    return dest


def extract_patch(workdir: Path) -> str:
    """The agent's change as a unified diff against base_commit, new files included, build noise excluded."""
    git(["add", "-A", "--", ".", *_PATCH_EXCLUDES], workdir)
    r = git(["diff", "--cached", "--binary", "HEAD", "--", ".", *_PATCH_EXCLUDES], workdir)
    git(["reset", "--quiet"], workdir)
    return r.stdout if r.returncode == 0 else ""


def apply_patch(workdir: Path, patch: str) -> tuple:
    """(ok, error). Applies on top of the current tree; refuses on conflict rather than half-applying."""
    if not (patch or "").strip():
        return True, ""
    r = git(["apply", "--whitespace=nowarn", "-"], workdir, input_text=patch)
    return r.returncode == 0, r.stderr.strip()[:300]


# -------------------------------------------------------------------- tests

def test_files(ids: list) -> list:
    """The test files an id list lives in. Only pytest style ids can be mapped."""
    return sorted({i.split("::", 1)[0] for i in ids if "::" in i})


def split_cmd(cmd: str) -> list:
    return [p.strip('"') for p in shlex.split(cmd, posix=(os.name != "nt"))]


def run_tests(workdir: Path, test_cmd: str, ids: list, timeout: int) -> dict:
    """Run the test files that hold `ids`; return {passed:set, failed:set, ran:int, error:str|None}."""
    files = test_files(ids)
    if not files:
        return {"passed": set(), "failed": set(), "ran": 0, "error": "no pytest-style test ids to run"}
    try:
        r = subprocess.run([*split_cmd(test_cmd), *files], cwd=str(workdir), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"passed": set(), "failed": set(), "ran": 0, "error": f"tests timed out after {timeout}s"}
    except OSError as e:
        return {"passed": set(), "failed": set(), "ran": 0, "error": f"test command failed to start: {e}"}
    rep = parse_test_report((r.stdout or "") + "\n" + (r.stderr or ""))
    err = None if rep["tests"] else f"no test results parsed (exit {r.returncode}): {(r.stderr or r.stdout)[-200:]}"
    return {"passed": set(rep["passed"]), "failed": set(rep["failed"]), "ran": len(rep["tests"]), "error": err}


def preflight_env(inst: dict, workdir: Path, test_cmd: str, timeout: int) -> Optional[str]:
    """None when the instance is gradable here, else why not.

    On the untouched base commit plus test_patch the FAIL_TO_PASS tests must run and fail. If they cannot
    run (missing deps) or already pass, a later 'resolved' or 'not resolved' would be meaningless.
    """
    ok, err = apply_patch(workdir, inst.get("test_patch") or "")
    if not ok:
        return f"test_patch does not apply to base_commit: {err}"
    want = inst.get("FAIL_TO_PASS") or []
    res = run_tests(workdir, test_cmd, want, timeout)
    git(["checkout", "--quiet", "--", "."], workdir)           # drop the test_patch edits again
    git(["clean", "-fdq"], workdir)
    if res["error"]:
        return res["error"]
    if set(want) & res["passed"] and not (set(want) - res["passed"]):
        return "FAIL_TO_PASS tests already pass before the fix (environment differs from the benchmark's)"
    return None


def grade_patch(inst: dict, workdir: Path, test_cmd: str, timeout: int) -> dict:
    """Apply test_patch over the agent's work and grade. {resolved: bool|None, error, passed:int}."""
    ok, err = apply_patch(workdir, inst.get("test_patch") or "")
    if not ok:
        return {"resolved": False, "error": f"test_patch conflicts with the agent's change: {err}", "passed": 0}
    ids = (inst.get("FAIL_TO_PASS") or []) + (inst.get("PASS_TO_PASS") or [])
    res = run_tests(workdir, test_cmd, ids, timeout)
    if res["error"] and not res["passed"]:
        return {"resolved": False, "error": res["error"], "passed": 0}
    return {"resolved": grade_resolved(inst, res["passed"]), "error": None, "passed": len(res["passed"])}


# --------------------------------------------------------------- agent run

def consume_sse(lines) -> dict:
    """Fold an /agent/run SSE stream into one record. Pure, so the parsing is unit-tested."""
    rec = {"run_id": None, "tools": [], "lanes": [], "final_text": "", "done_state": None,
           "done_reason": None, "guard_hits": [], "error": None}
    event = None
    for line in lines:
        line = line.strip()
        if line.startswith("event: "):
            event = line[7:].strip()
            continue
        if not line.startswith("data: ") or not event:
            continue
        try:
            data = json.loads(line[6:])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if event == "run":
            rec["run_id"] = data.get("run_id")
        elif event == "lane":
            rec["lanes"].append(data.get("lane"))
        elif event == "tool_call":
            rec["tools"].append(data.get("name"))
        elif event == "delta":
            rec["final_text"] += data.get("text") or ""
        elif event == "guard":
            rec["guard_hits"].append(data.get("rule") or data.get("message"))
        elif event == "done":
            rec["done_state"] = data.get("state")
            rec["done_reason"] = data.get("reason") or data.get("note")
            if data.get("state") == "failed":
                rec["error"] = f"agent run failed: {rec['done_reason'] or data}"
            break
    return rec


def run_agent(base: str, client, prompt: str, mode: str, max_steps: int, timeout_s: float) -> dict:
    payload = {"messages": [{"role": "user", "content": prompt}], "mode": mode, "max_steps": max_steps,
               "temperature": 0.3, "permission_mode": "bypass"}
    t0 = time.time()
    rec = {"run_id": None, "tools": [], "lanes": [], "final_text": "", "done_state": None,
           "done_reason": None, "guard_hits": [], "error": None}
    try:
        import httpx
        with client.stream("POST", f"{base}/agent/run", json=payload,
                           timeout=httpx.Timeout(timeout_s)) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "replace")[:300]
                rec["error"] = f"HTTP {resp.status_code}: {body}"
            else:
                rec = consume_sse(resp.iter_lines())
    except Exception as e:      # transport / timeout: reported, never fatal to the batch
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["duration_s"] = round(time.time() - t0, 1)
    return rec


def point_project_at(base: str, client, project_id: int, workspace: Path) -> Optional[str]:
    """Aim the eval project at this instance's folder. Returns an error string or None."""
    r = client.patch(f"{base}/control/projects/{project_id}/workspace", json={"workspace_dir": str(workspace)})
    if r.status_code != 200:
        return f"set workspace failed: HTTP {r.status_code} {r.text[:200]}"
    r = client.post(f"{base}/control/projects/{project_id}/activate")
    if r.status_code != 200:
        return f"activate project failed: HTTP {r.status_code} {r.text[:200]}"
    return None


# ------------------------------------------------------------------ driver

def run_instance(inst: dict, args, ctx: dict) -> dict:
    iid = inst["instance_id"]
    rec = {"instance_id": iid, "status": "error", "resolved": None, "patch": "", "tools": [],
           "done_state": None, "duration_s": None, "error": None}
    t0 = time.time()
    workdir = None
    try:
        cache = ensure_repo_cache(inst["repo"], ctx["cache_root"])
        workdir = prepare_checkout(inst, cache, ctx["work_root"])
        why = preflight_env(inst, workdir, args.test_cmd, args.test_timeout)
        if why:
            rec.update(status="env_error", error=why)
            return rec
        err = point_project_at(args.base, ctx["client"], ctx["project_id"], workdir)
        if err:
            rec["error"] = err
            return rec
        run = run_agent(args.base, ctx["client"], build_prompt(inst), args.mode, args.max_steps,
                        args.run_timeout)
        rec.update(run_id=run["run_id"], tools=run["tools"], done_state=run["done_state"],
                   done_reason=run["done_reason"], agent_error=run["error"])
        rec["patch"] = extract_patch(workdir)
        graded = grade_patch(inst, workdir, args.test_cmd, args.test_timeout) if rec["patch"] else \
            {"resolved": False, "error": "agent produced no patch", "passed": 0}
        rec.update(status="graded", resolved=graded["resolved"], error=graded["error"])
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
    finally:
        rec["duration_s"] = round(time.time() - t0, 1)
        if workdir and not args.keep:
            rmtree(workdir)
    return rec


def _patch_file(out_dir: Path, iid: str) -> Path:
    return out_dir / "patches" / f"{_slug(iid)}.diff"


def write_outputs(out_dir: Path, records: list) -> None:
    """predictions.jsonl (official format) + results.json. Patches live in patches/<id>.diff so a resumed
    run, whose cached records carry no patch text, still emits every earlier prediction."""
    (out_dir / "patches").mkdir(parents=True, exist_ok=True)
    with open(out_dir / "predictions.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            pf = _patch_file(out_dir, r["instance_id"])
            if "patch" in r:
                pf.write_text(r.get("patch") or "", encoding="utf-8")
            patch = pf.read_text(encoding="utf-8") if pf.exists() else ""
            f.write(json.dumps({"instance_id": r["instance_id"], "model_name_or_path": MODEL_NAME,
                                "model_patch": patch}) + "\n")
    slim = [{k: v for k, v in r.items() if k != "patch"} | ({"patch_len": len(r["patch"])} if "patch" in r else {})
            for r in records]
    (out_dir / "results.json").write_text(
        json.dumps({"summary": summarize_run(records), "records": slim}, indent=2), encoding="utf-8")


def operator_run(args) -> int:
    from tests import eval_agent  # reuses the login + companion + project plumbing
    if not args.allow_bypass:
        print("refusing to run: the agent runs with permission_mode=bypass, so its shell commands run unasked "
              "on this machine. Re-run with --allow-bypass if the work root is disposable.")
        return 2
    instances = load_instances(args.instances)
    if args.instance_id:
        instances = [i for i in instances if i["instance_id"] == args.instance_id]
    if args.limit:
        instances = instances[: args.limit]
    if not instances:
        print("no instances selected")
        return 2

    work_root = Path(args.work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out)
    prior = {}
    results_file = out_dir / "results.json"
    if results_file.exists() and not args.redo:
        try:
            for r in json.loads(results_file.read_text(encoding="utf-8")).get("records", []):
                prior[r["instance_id"]] = r
        except (ValueError, KeyError):
            prior = {}

    session = eval_agent._live_login(args.base, args.live_user, args.live_password,
                                     totp=getattr(args, "live_totp", None))
    eval_agent._live_preflight_companion(args.base, session, args.live_user)
    info = session.get(f"{args.base}/control/companion/status").json() or {}
    host = (info.get("hostname") or "").lower()
    if host and host != socket.gethostname().lower():
        print(f"refusing to run: the Companion is on '{host}' but this script is on "
              f"'{socket.gethostname()}'. The checkout must be on the Companion's machine.")
        return 2
    project_id = eval_agent._live_ensure_project(args.base, session, args.live_project, str(work_root))
    ctx = {"client": session, "project_id": project_id, "work_root": work_root,
           "cache_root": Path(args.cache_root) if args.cache_root else work_root / "_repos"}

    records = [prior[i["instance_id"]] for i in instances if i["instance_id"] in prior]
    for r in records:
        print(f"  [cached] {r['instance_id']}")
    done_ids = {r["instance_id"] for r in records}
    for inst in instances:
        if inst["instance_id"] in done_ids:
            continue
        rec = run_instance(inst, args, ctx)
        records.append(rec)
        verdict = {True: "RESOLVED", False: "not resolved", None: "no evidence"}[rec["resolved"]] \
            if rec["status"] == "graded" else rec["status"].upper()
        print(f"  [{verdict}] {inst['instance_id']} ({rec['duration_s']}s"
              f"{', ' + rec['error'] if rec.get('error') else ''})")
        write_outputs(out_dir, records)          # incremental: a crash mid-batch keeps what finished
    write_outputs(out_dir, records)
    print(json.dumps(summarize_run(records), indent=2))
    print(f"results -> {out_dir}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("instances", type=Path, help="SWE-bench JSONL file")
    ap.add_argument("--instance-id", default=None, help="run one instance only")
    ap.add_argument("--limit", type=int, default=0, help="first N instances only")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--live-user", required=True)
    ap.add_argument("--live-password", required=True)
    ap.add_argument("--live-totp", default=None)
    ap.add_argument("--live-project", default="__swe__", help="project the Companion works in")
    ap.add_argument("--work-root", required=True, help="disposable folder for checkouts (absolute)")
    ap.add_argument("--cache-root", default=None, help="repo clone cache (default <work-root>/_repos)")
    ap.add_argument("--out", default="bench_results/swe", help="predictions.jsonl + results.json land here")
    ap.add_argument("--test-cmd", default=DEFAULT_TEST_CMD,
                    help="pytest command of an environment that has the repo's dependencies installed")
    ap.add_argument("--test-timeout", type=int, default=1800)
    ap.add_argument("--mode", default="auto", choices=["auto", "main"])
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--run-timeout", type=float, default=1800, help="seconds per agent run")
    ap.add_argument("--allow-bypass", action="store_true",
                    help="acknowledge the agent runs shell commands unasked on this machine")
    ap.add_argument("--keep", action="store_true", help="keep checkouts after grading")
    ap.add_argument("--redo", action="store_true", help="ignore earlier results in --out")
    return operator_run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
