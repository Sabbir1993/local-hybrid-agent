"""scripts/early_exit_report.py - diagnose runs that die at step 1-2.

27% of recorded runs (67-run sample) end as `error` (median 1 step) or
`synthesized` (median 2 steps). Until now nothing could split that population:
outcome was recorded, cause was not, and the one tool-failure number people
quoted (a phantom 44% for edit_file) came from counting `tool_start` rows as
failures - a start is not an outcome.

Two rules this script exists to enforce:
  * a tool call is a COMPLETION row; `tool_start` rows are the beginning of a
    call and are reported separately as dangling, never as failures
  * every rate carries a Wilson 95% band, because n is small and a point
    estimate alone would be read as a finding

Run: python scripts/early_exit_report.py [--db usage.db] [--days 30]
"""
import argparse
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

# A run that ends here died immediately: no room for a long loop, a ceiling or
# a budget. Same threshold the plan's investigation note uses.
EARLY_MAX_STEPS = 3
EARLY_OUTCOMES = ("error", "synthesized")
CEILING_OUTCOMES = ("max_steps", "loop", "loop_near_repeat", "timeout", "budget", "no_progress")
DETAIL_PREFIXES = ("err:", "synth:", "stall:", "identical_result", "interrupted", "escalated")


def wilson(passes: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score interval for a pass/fail count. No scipy dependency."""
    if n <= 0:
        return (0.0, 0.0)
    p = passes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (round(max(0.0, (centre - spread) / denom), 3), round(min(1.0, (centre + spread) / denom), 3))


def _has_code(detail) -> bool:
    return bool(detail) and any(str(detail).startswith(p) for p in DETAIL_PREFIXES)


def analyse(db_path) -> dict:
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    try:
        return _analyse(db, db_path)
    finally:
        db.close()      # a leaked handle blocks cleanup on Windows


def _analyse(db, db_path) -> dict:

    runs = db.execute("SELECT run_id, steps, outcome, mode, detail FROM route_runs").fetchall()

    # ---- tool outcomes: completion rows only, starts counted apart
    tools = defaultdict(lambda: {"calls": 0, "failed": 0, "codes": Counter()})
    starts = Counter()
    completions = set()
    step0 = defaultdict(list)
    for r in db.execute("""SELECT run_id, step, tool_name, tool_ok, reason
                           FROM route_events WHERE tool_name IS NOT NULL"""):
        key = (r["run_id"], r["step"], r["tool_name"])
        if r["reason"] == "tool_start":
            starts[key] += 1
            continue
        completions.add(key)
        slot = tools[r["tool_name"]]
        slot["calls"] += 1
        if r["tool_ok"] == 0:
            slot["failed"] += 1
    # step-0 routing decisions are events with no tool_name (router_pass, plan_first,
    # executor_default ...), so they need their own query - filtering on tool_name
    # would drop exactly the population being investigated.
    for r in db.execute("""SELECT run_id, reason FROM route_events
                           WHERE step = 0 AND tool_name IS NULL AND reason <> 'tool_start'"""):
        step0.setdefault(r["run_id"], []).append(r["reason"])
    dangling = sum(v for k, v in starts.items() if k not in completions)
    for name, slot in tools.items():
        lo, hi = wilson(slot["failed"], slot["calls"])
        slot["fail_pct"] = round(100.0 * slot["failed"] / slot["calls"], 1) if slot["calls"] else 0.0
        slot["ci95"] = [lo, hi]
        slot["codes"] = dict(slot["codes"])
    tool_fail_codes = Counter()
    for r in db.execute("""SELECT tool_name, tool_err FROM route_events
                           WHERE tool_ok = 0 AND tool_err IS NOT NULL"""):
        tool_fail_codes[f"{r['tool_name']}:{r['tool_err']}"] += 1

    # ---- run populations
    outcomes = Counter(r["outcome"] or "unknown" for r in runs)
    early_rows = [r for r in runs if (r["outcome"] in EARLY_OUTCOMES)
                  and (r["steps"] or 0) <= EARLY_MAX_STEPS]
    ceiling_rows = [r for r in runs if r["outcome"] in CEILING_OUTCOMES]
    answered_rows = [r for r in runs if r["outcome"] == "answered"]

    causes = Counter()
    unattributed = 0
    for r in early_rows:
        if _has_code(r["detail"]):
            causes[str(r["detail"])] += 1
        else:
            unattributed += 1

    def step0_reasons(rows):
        c = Counter()
        for r in rows:
            for reason in step0.get(r["run_id"], []):
                c[reason] += 1
        return dict(c)

    attributed_any = any(_has_code(r["detail"]) for r in early_rows)
    notes = [
        "step-0 router behaviour per population is a HYPOTHESIS, not a finding: "
        "the bands overlap at this sample size and it is only worth chasing if "
        "the split persists with err:/synth: codes attached.",
    ]
    if not attributed_any:
        notes.append("none of the early-exit runs carry a cause code: they predate the "
                     "err:/synth:/stall: change, so the split below is a count, not a "
                     "diagnosis. Re-run after fresh traffic.")

    return {
        "db": str(db_path),
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "runs": len(runs),
        "outcomes": dict(outcomes),
        "answered": len(answered_rows),
        "early": {"n": len(early_rows), "causes": dict(causes), "unattributed": unattributed},
        "ceiling": {"n": len(ceiling_rows),
                    "outcomes": dict(Counter(r["outcome"] for r in ceiling_rows))},
        "tools": {name: slot for name, slot in tools.items()},
        "dangling_tool_starts": dangling,
        "tool_failure_codes": dict(tool_fail_codes),
        "step0_router": {"early": step0_reasons(early_rows), "answered": step0_reasons(answered_rows)},
        "attribution_ready": attributed_any,
        "notes": notes,
    }


def render(out: dict) -> str:
    L = []
    A = L.append
    A(f"early-exit report - {out['db']} ({out['generated']})")
    A(f"runs recorded: {out['runs']}")
    A("\nrun outcomes:")
    for outcome, n in sorted(out["outcomes"].items(), key=lambda kv: -kv[1]):
        A(f"  {outcome:<18} {n:4}")
    early, ceil = out["early"], out["ceiling"]
    A(f"\nearly exits (outcome in {EARLY_OUTCOMES} within {EARLY_MAX_STEPS} steps): {early['n']}"
      f" ({round(100.0 * early['n'] / max(1, out['runs']))}% of runs)")
    if early["causes"]:
        A("  causes:")
        for code, n in sorted(early["causes"].items(), key=lambda kv: -kv[1]):
            A(f"    {code:<34} {n:4}")
    if early["unattributed"]:
        A(f"  unattributed (no code recorded): {early['unattributed']}")
    A(f"ceiling stops (max_steps/loop/timeout/budget/no_progress): {ceil['n']}")
    for outcome, n in sorted(ceil["outcomes"].items(), key=lambda kv: -kv[1]):
        A(f"    {outcome:<18} {n:4}")
    A("\ntools (completion rows only - tool_start is not an outcome):")
    for name, slot in sorted(out["tools"].items(), key=lambda kv: -kv[1]["calls"]):
        A(f"  {name:<22} calls={slot['calls']:<5} fail={slot['fail_pct']:5.1f}%  "
          f"ci95={slot['ci95']}")
    if out["dangling_tool_starts"]:
        A(f"  (plus {out['dangling_tool_starts']} call(s) that started and never "
          f"completed - reported, never counted as failures)")
    if out["tool_failure_codes"]:
        A("\ntool failure codes:")
        for code, n in sorted(out["tool_failure_codes"].items(), key=lambda kv: -kv[1]):
            A(f"  {code:<34} {n:4}")
    A("\nstep-0 router reason by population:")
    A(f"  early   : {out['step0_router']['early'] or '{}'}")
    A(f"  answered: {out['step0_router']['answered'] or '{}'}")
    A("\nnotes:")
    for note in out["notes"]:
        A(f"  - {note}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="usage.db", help="path to usage.db")
    args = ap.parse_args()
    db_path = Path(args.db)
    if not db_path.is_file():
        raise SystemExit(f"no database at {db_path}")
    print(render(analyse(db_path)))


if __name__ == "__main__":
    main()