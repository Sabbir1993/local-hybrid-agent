"""Measure the agent's performance from usage.db (read-only), so a change can be judged on numbers.

    python scripts/perf_report.py                # last 3 days
    python scripts/perf_report.py --days 7
    python scripts/perf_report.py --since 2026-09-30 --label after   # window starting at a date

Reports what actually costs time and money: per-model request time and prompt size (with the share
served from the provider/KV cache), steps per run, how runs ended, what the executor wasted before
escalating, and tool success. Run it before and after a change (or a config edit) and compare.
"""
import argparse
import datetime as dt
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "usage.db")


def report(con, since, until, label=""):
    q = lambda sql, *a: con.execute(sql, a).fetchall()
    print(f"\n=== {label or 'window'}: {dt.datetime.fromtimestamp(since):%Y-%m-%d %H:%M} -> "
          f"{dt.datetime.fromtimestamp(until):%Y-%m-%d %H:%M}")
    print("\nPer endpoint / model  (n, avg prompt tok, avg out tok, avg s, tok/s, cached %)")
    for r in q("""SELECT endpoint, model, source, COUNT(*), ROUND(AVG(prompt_tokens)), ROUND(AVG(completion_tokens)),
                  ROUND(AVG(duration_s),1), ROUND(AVG(tps),1),
                  ROUND(100.0*SUM(prompt_cached_tokens)/MAX(1,SUM(prompt_tokens)))
                  FROM requests WHERE ts>=? AND ts<? AND status=200 GROUP BY endpoint, model, source
                  ORDER BY COUNT(*) DESC LIMIT 12""", since, until):
        print("  ", r)
    print("\nRuns by category  (n, avg steps, max steps)")
    for r in q("""SELECT category, COUNT(*), ROUND(AVG(steps),1), MAX(steps) FROM route_runs
                  WHERE ts>=? AND ts<? AND outcome IS NOT NULL GROUP BY category""", since, until):
        print("  ", r)
    print("\nRun outcomes  (outcome, n, avg steps)")
    for r in q("""SELECT outcome, COUNT(*), ROUND(AVG(steps),1) FROM route_runs
                  WHERE ts>=? AND ts<? GROUP BY outcome ORDER BY 2 DESC""", since, until):
        print("  ", r)
    print("\nStops by detail  (outcome, detail, n)")
    try:
        for r in q("""SELECT outcome, detail, COUNT(*) FROM route_runs WHERE ts>=? AND ts<? AND detail IS NOT NULL
                      GROUP BY outcome, detail ORDER BY 3 DESC LIMIT 8""", since, until):
            print("  ", r)
    except sqlite3.OperationalError:
        print("   (no detail column yet: it appears after the server restarts)")
    n, esc, avg, wasted = q("""SELECT COUNT(*), COALESCE(SUM(escalated),0), ROUND(AVG(duration_s),1),
                               ROUND(COALESCE(SUM(CASE WHEN escalated=1 THEN duration_s END),0))
                               FROM route_events WHERE ts>=? AND ts<? AND lane='executor' AND step=0
                               AND tool_name IS NULL""", since, until)[0]
    print(f"\nExecutor step 0: {n} steps, {esc} escalated, avg {avg}s, {wasted}s spent on steps that were thrown away")
    print("\nTools  (tool, calls, ok %)")
    for r in q("""SELECT tool_name, COUNT(*), ROUND(100.0*SUM(tool_ok)/COUNT(*)) FROM route_events
                  WHERE ts>=? AND ts<? AND tool_name IS NOT NULL GROUP BY tool_name ORDER BY 2 DESC LIMIT 8""",
               since, until):
        print("  ", r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--since", help="YYYY-MM-DD[ HH:MM] start (overrides --days)")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    if not os.path.exists(DB):
        sys.exit(f"no usage.db at {DB}")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    until = time.time()
    if args.since:
        fmt = "%Y-%m-%d %H:%M" if " " in args.since else "%Y-%m-%d"
        since = dt.datetime.strptime(args.since, fmt).timestamp()
    else:
        since = until - args.days * 86400
    report(con, since, until, args.label)


if __name__ == "__main__":
    main()
