"""Usage-based router tuning: read core.route_log telemetry and PROPOSE changes
to the APP_CONFIG["router"] rules. Nothing is applied here -- an admin reviews
each suggestion (with its evidence) in Settings -> Router and applies or
dismisses it via /control/router/suggestions/{id}/... (audited as change control).

Rules (each needs MIN_SAMPLES before it may fire):
  1. executor almost always escalates for a query category -> start that
     category on main (the wasted executor step is pure latency + GPU time)
  2. a start_on_main category turns out to be simple (short runs, few thumbs
     down) -> hand it back to the executor to free the main lane
  3. router confidence_threshold: raise when router-picked tools fail or get
     thumbs down, lower (more CPU routing) when they are reliably right
  4. repeat_streak_limit: lower to 1 when escalating after a repeat streak
     rarely rescues the run anyway
"""

import asyncio
import sys
import time

from . import route_log
from .db import _usage_db
from .router_policy import rcfg

MIN_SAMPLES = 30
WINDOW_DAYS = 30
THRESH_STEP = 0.05
THRESH_MIN, THRESH_MAX = 0.6, 0.95
TUNE_INTERVAL_S = 24 * 3600


def _q(sql: str, params: tuple = ()):
    return _usage_db.execute(sql, params).fetchall()


def _category_rules(cfg: dict, since: float) -> list:
    out = []
    on_main = list(cfg.get("start_on_main_categories") or [])
    for cat, n, esc, avg_dt in _q(
            "SELECT category, COUNT(*), SUM(escalated), AVG(duration_s) FROM route_events "
            "WHERE ts >= ? AND step = 0 AND lane = 'executor' AND tool_name IS NULL "
            "AND reason = 'executor_default' GROUP BY category", (since,)):
        if not cat or cat == "greeting" or cat in on_main or n < MIN_SAMPLES:
            continue
        rate = (esc or 0) / n
        if rate >= 0.6:
            reasons = {r: c for r, c in _q(
                "SELECT escalate_reason, COUNT(*) FROM route_events WHERE ts >= ? AND step = 0 "
                "AND lane = 'executor' AND category = ? AND escalated = 1 GROUP BY escalate_reason", (since, cat))}
            out.append(("start_on_main_categories", on_main, sorted(on_main + [cat]), {
                "rule": "executor_escalates", "category": cat, "samples": n,
                "escalation_rate": round(rate, 3), "escalate_reasons": reasons,
                "avg_wasted_executor_s": round(avg_dt or 0, 2),
                "why": f"The executor's first step was re-run on main {rate:.0%} of the time for "
                       f"'{cat}' requests; starting them on main skips that wasted step."}))
    for cat in on_main:
        row = _q("SELECT COUNT(*), AVG(steps), SUM(rating = -1), SUM(rating IS NOT NULL) FROM route_runs "
                 "WHERE ts >= ? AND category = ? AND outcome IN ('answered', 'synthesized')", (since, cat))[0]
        n, avg_steps, down, rated = row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0
        down_rate = (down / rated) if rated else 0.0
        if n >= MIN_SAMPLES and avg_steps <= 1.5 and down_rate <= 0.1:
            out.append(("start_on_main_categories", on_main, sorted(c for c in on_main if c != cat), {
                "rule": "main_first_is_simple", "category": cat, "samples": n,
                "avg_steps": round(avg_steps, 2), "thumbs_down_rate": round(down_rate, 3),
                "why": f"'{cat}' runs that start on main finish in ~{avg_steps:.1f} steps with few "
                       "thumbs down; try the executor again to free the main lane."}))
    return out


def _threshold_rule(cfg: dict, since: float) -> list:
    from .small_model import APP_CONFIG
    cur = float((APP_CONFIG.get("router") or {}).get("confidence_threshold", 0.7))
    hits = _q("SELECT e.run_id, e.tool_ok, r.rating FROM route_events e LEFT JOIN route_runs r ON r.run_id = e.run_id "
              "WHERE e.ts >= ? AND e.step = 0 AND e.lane = 'router' AND e.tool_name IS NOT NULL", (since,))
    if len(hits) < MIN_SAMPLES:
        return []
    ok_rate = sum(1 for h in hits if h[1] == 1) / len(hits)
    rated = [h for h in hits if h[2] is not None]
    down_rate = (sum(1 for h in rated if h[2] == -1) / len(rated)) if rated else 0.0
    ev = {"rule": "router_threshold", "router_hits": len(hits), "tool_ok_rate": round(ok_rate, 3),
          "thumbs_down_rate": round(down_rate, 3), "rated": len(rated)}
    if (ok_rate < 0.8 or down_rate > 0.25) and cur < THRESH_MAX:
        new = round(min(THRESH_MAX, cur + THRESH_STEP), 2)
        ev["why"] = (f"Router-picked tools succeeded {ok_rate:.0%} of the time "
                     f"({down_rate:.0%} thumbs down); require more confidence before routing.")
        return [("confidence_threshold", cur, new, ev)]
    passes = _q("SELECT COUNT(*) FROM route_events WHERE ts >= ? AND step = 0 AND reason = 'router_pass'", (since,))[0][0]
    if ok_rate >= 0.97 and down_rate <= 0.05 and passes >= MIN_SAMPLES and cur > THRESH_MIN:
        new = round(max(THRESH_MIN, cur - THRESH_STEP), 2)
        ev.update(router_passes=passes, why=(f"Router-picked tools succeeded {ok_rate:.0%} of the time; "
                                            "lower the threshold so more simple lookups skip the LLM."))
        return [("confidence_threshold", cur, new, ev)]
    return []


def _streak_rule(cfg: dict, since: float) -> list:
    cur = int(cfg.get("repeat_streak_limit") or 2)
    if cur <= 1:
        return []
    rows = _q("SELECT DISTINCT e.run_id, r.outcome FROM route_events e JOIN route_runs r ON r.run_id = e.run_id "
              "WHERE e.ts >= ? AND e.reason = 'repeat_streak'", (since,))
    if len(rows) < MIN_SAMPLES:
        return []
    failed = sum(1 for _rid, o in rows if o in ("max_steps", "loop", "error"))
    rate = failed / len(rows)
    if rate >= 0.5:
        return [("repeat_streak_limit", cur, 1, {
            "rule": "repeat_streak", "runs": len(rows), "still_failed_rate": round(rate, 3),
            "why": f"{rate:.0%} of runs that escalated after repeating tool calls still failed; "
                   "escalate on the first repeat instead."})]
    return []


def run_tuner(days: int = WINDOW_DAYS) -> list:
    """Compute suggestions and store the new ones as pending. Returns the ids touched."""
    cfg = rcfg()
    since = time.time() - days * 86400
    ids = []
    for key, current, proposed, evidence in (_category_rules(cfg, since) + _threshold_rule(cfg, since)
                                             + _streak_rule(cfg, since)):
        evidence["window_days"] = days
        sid = route_log.add_suggestion(key, current, proposed, evidence)
        if sid:
            ids.append(sid)
    route_log.purge()
    return ids


async def tuner_background_task() -> None:
    """Daily suggestion pass (never applies anything)."""
    await asyncio.sleep(120)
    while True:
        try:
            # a handful of indexed aggregate queries: run on the loop thread, which
            # owns every other write to the shared usage.db connection
            ids = run_tuner()
            if ids:
                print(f"[router_tuner] {len(ids)} routing suggestion(s) pending admin review", file=sys.stderr)
        except Exception as e:
            print(f"[router_tuner] tuning pass failed: {e}", file=sys.stderr)
        await asyncio.sleep(TUNE_INTERVAL_S)
