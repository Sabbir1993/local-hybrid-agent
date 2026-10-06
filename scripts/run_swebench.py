#!/usr/bin/env python3
"""
scripts/run_swebench.py - run SWE-bench Lite instances against the agent loop.

The external-benchmark counterpart to the self-authored live suite
(tests/eval_agent.py --live): SWE-bench instances are graded by a gold test
patch, not by a prompt-fitted checker, so the number is comparable across
agents (SWE-bench Lite / Terminal-Bench style; we use the SWE-bench JSONL
format). Uses the same login + SSE machinery as tests/eval_agent.py --live.

The grading core (load_instances / grade_resolved) is pure and unit-tested
(tests/test_swebench_harness.py) and runs anywhere. The run itself needs a
running server, a logged-in user, the Companion connected and a workspace
taking the instance repo - i.e. the machine that runs models.

Usage:
  python scripts/run_swebench.py instances.jsonl --live-user EVAL --live-password ... \\
      --live-workspace C:/swebench_ws --instance <instance_id> \\
      --test-cmd "python -m pytest tests -x -q"
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))


def load_instances(path: str | Path) -> list:
    """Parse a SWE-bench JSONL file into a list of instance dicts.

    Keeps the fields this harness uses and normalises them; drops junk rows
    with a stderr note so one malformed instance cannot kill the run.
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
                print(f"[swebench] line {line_no}: bad JSON ({e}); skipped",
                      file=sys.stderr)
                continue
            if not isinstance(d, dict) or not d.get("instance_id"):
                print(f"[swebench] line {line_no}: no instance_id; skipped",
                      file=sys.stderr)
                continue
            out.append(d)
    return out


def _first_line(text: str) -> str:
    return (text or "").splitlines()[0].strip()[:120]


def parse_test_report(output: str) -> dict:
    """Extract test names + pass/fail from pytest short-summary output.

    The `-q`/default summary prints one line per test:
      tests/test_x.py::test_y PASSED
      tests/test_x.py::test_z FAILED
    Returns {tests, passed, failed} - the observed green set is what
    grade_resolved wants for FAIL_TO_PASS and PASS_TO_PASS."
    """
    rx = re.compile(r"^([\w./\\-]+\.py::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s*$", re.M)
    lines = output.splitlines()
    names = set()
    passed, failed = set(), set()
    for ln in lines:
        m = rx.match(ln.strip())
        if not m:
            continue
        name, status = m.group(1), m.group(2)
        names.add(name)
        (passed if status in ("PASSED", "XPASS") else failed).add(name)
    return {"tests": sorted(names), "passed": sorted(passed), "failed": sorted(failed)}


def grade_resolved(instance: dict, observed_pass: set) -> bool:
    """SWE-bench 'resolved': every FAIL_TO_PASS in the instance is now green
    and no PASS_TO_PASS regressed, judged against the tests observed passing
    in the operator's pytest run of the gold patch.

    Returns True when the instance is resolved, False when it is not, and
    None when insufficient evidence exists to say either way (no expected
    test ran at all - the harness must not guess at a score it did not see).
    """
    want = set(instance.get("FAIL_TO_PASS") or [])
    keep = set(instance.get("PASS_TO_PASS") or [])
    if not want and not keep:
        return None
    f2p_ok = want and (observed_pass & want) >= want   # all requested now green
    if not f2p_ok:
        return False
    regressed = keep - observed_pass
    if keep and regressed:
        return False
    return True


def summarize(instances: list) -> dict:
    """Wilson-interval rollup of resolved instances (same stat as the repo's
    eval harness, single implementation reused by import)."""
    from eval_mock import wilson_ci
    n = len(instances)
    resolved = sum(1 for i in instances if i.get("_resolved"))
    lo, hi = wilson_ci(resolved, n)
    return {"instances": n, "resolved": resolved,
            "pass_rate": round(resolved / n, 3) if n else 0.0,
            "ci95": [lo, hi]}


def operator_run(args) -> int:
    """The real run: drive the agent loop over HTTP for each instance, then
    grade from observed test results. The tests themselves run on the user's
    machine (the Companion) after the gold test_patch is applied - this file
    writes the patch + problem statement into the workspace so the agent and
    the operator both see them.

    Requires: running server, logged-in role, Companion, loaded model, and a
    workspace where the instance repo can be checked out."""
    from tests import eval_agent  # noqa: E402 (reuses login + SSE plumbing)
    instances = load_instances(args.instances)
    if args.instance_id:
        instances = [i for i in instances if i["instance_id"] == args.instance_id]
    if not instances:
        print("no instances selected"); return 2

    session = eval_agent._live_login(args.base, args.live_user, args.live_password,
                                     totp=getattr(args, "live_totp", None))
    eval_agent._live_preflight_companion(args.base, session, args.live_user)
    ws_root = Path(args.live_workspace)
    observed = {}
    if getattr(args, "observed_json", None):
        try:
            observed = json.loads(Path(args.observed_json).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[swebench] could not read --observed-json: {e}", file=sys.stderr)
            return 2
    results = []
    for inst in instances:
        iid = inst["instance_id"]
        # workspace_dir lives on the user's machine; use a subfolder per instance
        ws = ws_root / iid.replace("/", "__")
        ws.mkdir(parents=True, exist_ok=True)
        # problem_statement + gold patch land in the workspace so the agent can
        # read them and the operator can apply the patch + run --test-cmd.
        (ws / "task.md").write_text(inst.get("problem_statement") or "", encoding="utf-8")
        (ws / "_swe_test.patch").write_text(inst.get("test_patch") or "", encoding="utf-8")
        rec = eval_agent.run_live_task(
            args.base, {"name": iid, "prompt": (inst.get("problem_statement") or "")[:2000]},
            "auto", 600, client=session)
        # A run that hard-failed (transport, no model) cannot be graded.
        run_passed_names = set(observed.get(iid, {}).get("passed", []) or [])
        if not rec.get("ok") and not run_passed_names:
            inst["_resolved"] = False
            inst["_evidence"] = {"problems": rec.get("problems") or [], "run_ok": rec.get("ok"),
                                 "note": "unverified: agent run failed and no test results supplied"}
            results.append(iid)
            print(f"  [UNVERIFIED] {iid}")
            continue
        ok = grade_resolved(inst, run_passed_names)
        inst["_resolved"] = bool(ok)
        inst["_evidence"] = {"problems": rec.get("problems") or [], "run_ok": rec.get("ok"),
                             "observed_passed": sorted(run_passed_names)}
        results.append(iid)
        print(f"  [{'RESOLVED' if ok else 'not resolved'}] {iid}")
    print(json.dumps(summarize(instances), indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("instances", type=Path, help="SWE-bench JSONL file")
    ap.add_argument("--instance-id", default=None, help="run one instance only")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--live-user", required=True)
    ap.add_argument("--live-password", required=True)
    ap.add_argument("--live-totp", default=None)
    ap.add_argument("--live-workspace", required=True)
    ap.add_argument("--observed-json", default=None,
                    help="JSONL-shaped {instance_id: {passed: [...]}} from the "
                         "operator's pytest run of the gold patch")
    return operator_run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
