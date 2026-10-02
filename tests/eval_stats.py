"""
tests/eval_stats.py - statistics for repeated live runs.

The live eval used to be single-shot: one run per task, `ok` boolean, no interval. A single
sample cannot distinguish "this works" from "this worked once", and it cannot tell you that a
task is flaky - which is the single most actionable thing an agent eval can tell you, because
flaky tasks are usually prompt/tool-schema problems rather than capability limits.

Three numbers, deliberately:

  pass_rate   fraction of runs that passed
  ci95        Wilson score interval. Used instead of normal-approximation because at n=5 with
              3/5 the normal interval is nonsense (it can extend below 0 and above 1), and
              because the honest interval is what stops 4/5 being reported as "80%, basically fine"
  stable      True only if every run agreed. A task at 3/5 is not a 60% task; it is a task
              whose outcome depends on sampling temperature, and it needs a different fix.

`wilson_ci` is imported from eval_mock rather than reimplemented, so the mock and live suites
cannot drift on the same statistic.

Run: python tests/eval_stats.py   (self-check)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_mock import wilson_ci  # noqa: E402  (single implementation, shared with the mock suite)


def summarize_task(task_name: str, runs: list, category: str = "misc", soft: bool = False) -> dict:
    """One task's record from its repeated runs.

    `runs` is a list of result dicts, each with at least {"ok": bool}. Errors count as
    failures: a transport error means the task did not complete, and quietly excluding those
    runs would inflate every rate in this file.
    """
    n = len(runs)
    ok = [bool(r.get("ok")) for r in runs]
    n_ok = sum(ok)
    rate = (n_ok / n) if n else 0.0
    lo, hi = wilson_ci(n_ok, n) if n else (0.0, 0.0)

    problems = []
    for r in runs:
        # Derive from `error` as well as `problems`. A transport error is a failed run, and
        # relying on the caller to have turned it into a problem string is how a timeout
        # silently becomes a "no noted problems" failure that reads like a capability limit.
        if r.get("error"):
            msg = f"transport error: {r['error']}"
            if msg not in problems:
                problems.append(msg)
        for p in (r.get("problems") or []):
            if p not in problems:
                problems.append(p)

    durations = [r.get("duration_s") for r in runs if isinstance(r.get("duration_s"), (int, float))]
    tools_seen = sorted({t for r in runs for t in (r.get("tools") or [])})
    lanes_seen = sorted({l for r in runs for l in (r.get("lanes") or [])})

    return {
        "name": task_name,
        "category": category,
        "soft": soft,
        "n": n,
        "n_pass": n_ok,
        "pass_rate": round(rate, 3),
        "ci95": [round(lo, 3), round(hi, 3)],
        "stable": n_ok in (0, n),
        "flaky": 0 < n_ok < n,
        "problems": problems[:8],
        "tools_seen": tools_seen,
        "lanes_seen": lanes_seen,
        "duration_s": {"min": min(durations), "max": max(durations),
                       "mean": round(sum(durations) / len(durations), 1)} if durations else None,
        "runs": [{"ok": r.get("ok"), "duration_s": r.get("duration_s"),
                  "tools": r.get("tools"), "lanes": r.get("lanes"),
                  "error": r.get("error")} for r in runs],
    }


def rollup(records: list) -> dict:
    """Suite-level numbers, plus the per-category and per-flaky breakdowns that make the
    result actionable rather than a single number."""
    hard = [r for r in records if not r.get("soft")]
    all_runs = sum(r["n"] for r in records)
    all_ok = sum(r["n_pass"] for r in records)
    hard_ok = sum(r["n_pass"] for r in hard)
    hard_runs = sum(r["n"] for r in hard)
    lo, hi = wilson_ci(all_ok, all_runs) if all_runs else (0.0, 0.0)
    hlo, hhi = wilson_ci(hard_ok, hard_runs) if hard_runs else (0.0, 0.0)

    by_cat = {}
    for r in records:
        c = r.get("category", "misc")
        slot = by_cat.setdefault(c, {"n": 0, "n_pass": 0, "tasks": 0, "failing": [], "flaky": []})
        slot["tasks"] += 1
        slot["n"] += r["n"]
        slot["n_pass"] += r["n_pass"]
        if r["n_pass"] == 0:
            slot["failing"].append(r["name"])
        elif r["flaky"]:
            slot["flaky"].append(f"{r['name']}({r['n_pass']}/{r['n']})")

    for c, slot in by_cat.items():
        slot["pass_rate"] = round(slot["n_pass"] / slot["n"], 3) if slot["n"] else 0.0

    flaky = sorted((r for r in records if r["flaky"]),
                   key=lambda r: (r["n_pass"] / r["n"]) if r["n"] else 0)
    failing = sorted((r for r in records if r["n_pass"] == 0), key=lambda r: r["name"])

    return {
        "tasks": len(records),
        "hard_tasks": len(hard),
        "runs": all_runs,
        "pass_rate": round(all_ok / all_runs, 3) if all_runs else 0.0,
        "ci95": [round(lo, 3), round(hi, 3)],
        "hard_pass_rate": round(hard_ok / hard_runs, 3) if hard_runs else 0.0,
        "hard_ci95": [round(hlo, 3), round(hhi, 3)],
        "stable_tasks": sum(1 for r in records if r["stable"]),
        "flaky_tasks": [r["name"] for r in flaky],
        "failing_tasks": [r["name"] for r in failing],
        "by_category": by_cat,
    }


def format_report(summary: dict, records: list, width: int = 78) -> str:
    """Human-readable summary. Deliberately prints the interval next to every rate: a bare
    percentage at n=5 invites over-reading it."""
    out = ["=" * width, " LIVE EVAL SUMMARY (real model, deterministic checkers)", "=" * width]
    c = summary["ci95"]
    hc = summary["hard_ci95"]
    out.append(f"  tasks {summary['tasks']} ({summary['hard_tasks']} gating)  "
               f"runs {summary['runs']}")
    out.append(f"  pass rate    {summary['pass_rate']:.3f}  ci95 [{c[0]:.3f}, {c[1]:.3f}]")
    out.append(f"  gating only  {summary['hard_pass_rate']:.3f}  ci95 [{hc[0]:.3f}, {hc[1]:.3f}]")
    out.append(f"  stable       {summary['stable_tasks']}/{summary['tasks']} tasks "
               f"(all runs agreed)")
    out.append("")
    out.append("  by category:")
    for cat, s in sorted(summary["by_category"].items()):
        mark = "" if not s["failing"] else "  FAILING: " + ", ".join(s["failing"])
        fl = "" if not s["flaky"] else "  flaky: " + ", ".join(s["flaky"])
        out.append(f"    {cat:12s} {s['pass_rate']:.3f}  ({s['n_pass']}/{s['n']}){mark}{fl}")
    if summary["flaky_tasks"]:
        out.append("")
        out.append("  FLAKY (outcome depends on sampling - fix the prompt or the schema,"
                   " not the model):")
        for r in sorted((x for x in records if x["flaky"]),
                        key=lambda r: r["n_pass"] / r["n"] if r["n"] else 0):
            out.append(f"    {r['name']:26s} {r['n_pass']}/{r['n']}  "
                       f"ci95[{r['ci95'][0]:.2f},{r['ci95'][1]:.2f}]")
    return "\n".join(out)


if __name__ == "__main__":
    # self-check: the three claims this module exists to make
    s = summarize_task("t", [{"ok": True}, {"ok": True}, {"ok": False}])
    assert s["pass_rate"] == 0.667, s
    assert s["flaky"] is True and s["stable"] is False
    assert s["n_pass"] == 2 and s["n"] == 3

    allpass = summarize_task("t2", [{"ok": True}] * 5)
    assert allpass["stable"] is True and allpass["flaky"] is False

    allfail = summarize_task("t3", [{"ok": False}] * 4)
    assert allfail["n_pass"] == 0 and allfail["stable"] is True

    # Wilson must stay inside [0,1] where the normal approximation would not
    lo, hi = wilson_ci(3, 5)
    assert 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0, (lo, hi)
    lo0, hi0 = wilson_ci(0, 5)
    assert lo0 == 0.0 and 0.0 < hi0 < 1.0, (lo0, hi0)
    lo1, hi1 = wilson_ci(5, 5)
    assert hi1 == 1.0 and 0.0 < lo1 < 1.0, (lo1, hi1)

    # errors count as failures rather than being dropped
    r = summarize_task("t4", [{"ok": True, "error": None}, {"ok": False, "error": "timeout"}])
    assert r["n_pass"] == 1 and r["n"] == 2, r
    assert any("transport" in p for p in r["problems"]), r["problems"]

    roll = rollup([s, allpass, allfail, r])
    assert roll["runs"] == 3 + 5 + 4 + 2
    assert set(roll["failing_tasks"]) == {"t3"}
    assert "t" in roll["flaky_tasks"] and "t4" in roll["flaky_tasks"]
    assert roll["by_category"]["misc"]["tasks"] == 4

    print(format_report(roll, [s, allpass, allfail, r]))
    print("\nself-check OK")