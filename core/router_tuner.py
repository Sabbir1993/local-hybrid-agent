"""Usage-based router tuning: read core.route_log telemetry and PROPOSE changes
to the APP_CONFIG["router"] rules. By default nothing is applied here -- an admin
reviews each suggestion (with its evidence) in Settings -> Router and applies or
dismisses it via /control/router/suggestions/{id}/... (audited as change control).

Optional auto-apply (router.auto_apply, off by default): the background pass may apply
a suggestion itself, under guard rails -
  * only the three keys the rules below produce (AUTO_KEYS), re-validated;
  * only with enough history to compare (MIN_BASELINE runs before the change);
  * one change under watch at a time, so a result is never confounded;
  * the change is judged against its own before/after window (share of runs that end
    badly: error, loop, step/time limit, thumbs down). If it is clearly worse it is
    rolled back automatically, the rollback is audited, and the same change is not
    proposed again for a week.
Every automatic step is written to the audit log and to router_applied.

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
import json
import sys
import time

from . import route_log, router_config
from .audit import audit_log
from .db import _usage_db
from .router_policy import rcfg

MIN_SAMPLES = 30
WINDOW_DAYS = 30
THRESH_STEP = 0.05
THRESH_MIN, THRESH_MAX = 0.6, 0.95
TUNE_INTERVAL_S = 24 * 3600
CHECK_INTERVAL_S = 3600            # how often applied changes are judged

AUTO_KEYS = ("start_on_main_categories", "repeat_streak_limit", "confidence_threshold")
BASELINE_RUNS = 60                 # runs before the change that form its baseline
MIN_BASELINE = 20                  # below this there is nothing to compare against
MIN_AFTER = 20                     # runs after the change needed for a verdict
MAX_AFTER = 60                     # runs after the change that are counted
WORSE_BY = 0.10                    # bad-run rate rise that triggers a rollback
MIN_BAD_AFTER = 3                  # ...and at least this many bad runs (not one unlucky pair)
WATCH_DAYS = 14                    # a change with too little traffic is kept after this long


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
        if route_log.recently_rolled_back(key, proposed):
            continue        # tried lately and made things worse: do not propose it again yet
        evidence["window_days"] = days
        sid = route_log.add_suggestion(key, current, proposed, evidence)
        if sid:
            ids.append(sid)
    route_log.purge()
    return ids


def _changed_category(key: str, current, proposed):
    """The one request category a start_on_main change touches (None = affects every run)."""
    if key != "start_on_main_categories":
        return None
    diff = set(current or []) ^ set(proposed or [])
    return next(iter(diff)) if len(diff) == 1 else None


def _rate(q: dict):
    return (q["bad"] / q["runs"]) if q["runs"] else None


def auto_apply(now: float = None) -> list:
    """Apply at most one pending suggestion, if router.auto_apply is on and it is safe."""
    if not router_config.settings().get("auto_apply"):
        return []
    if route_log.list_applied("watching", limit=1):
        return []                                   # one change under watch at a time
    who = "auto"
    for sug in reversed(route_log.list_suggestions("pending")):     # oldest first
        key, proposed = sug["key"], sug["proposed"]
        if key not in AUTO_KEYS:
            continue
        if route_log.recently_rolled_back(key, proposed):
            route_log.decide_suggestion(sug["id"], "dismissed", who)
            continue
        live = router_config.settings().get(key)
        if json.dumps(live, sort_keys=True) != json.dumps(sug["current"], sort_keys=True):
            route_log.decide_suggestion(sug["id"], "stale", who)
            continue
        changes, err = router_config.validate({key: proposed})
        if err:
            continue
        category = _changed_category(key, sug["current"], proposed)
        baseline = route_log.run_quality(category, limit=BASELINE_RUNS, newest_first=True)
        if baseline["runs"] < MIN_BASELINE:
            continue                                # not enough history: leave it for a person
        old, werr = router_config.write(changes)
        if werr:
            print(f"[router_tuner] auto-apply failed: {werr}", file=sys.stderr)
            return []
        route_log.decide_suggestion(sug["id"], "applied", who)
        aid = route_log.add_applied(sug["id"], key, old.get(key), proposed, category, baseline)
        audit_log(None, action="router.tune.auto_apply", resource=key, permission_key="settings.router.configure",
                  detail={"suggestion": sug["id"], "applied": aid, "from": old, "to": changes,
                          "category": category, "baseline": baseline, "evidence": sug["evidence"]})
        print(f"[router_tuner] auto-applied {key} -> {proposed} (watching {category or 'all runs'})",
              file=sys.stderr)
        return [aid]
    return []


def rollback_applied(aid: int, who: str, reason: str = "manual", result: dict = None) -> tuple:
    """Put an applied change's old value back. -> (ok, message). Only while the setting
    still holds the value the tuner wrote (otherwise someone changed it since)."""
    row = route_log.get_applied(aid)
    if not row:
        return False, "no such applied change"
    if row["status"] not in ("watching", "kept"):
        return False, f"already {row['status']}"
    live = router_config.settings().get(row["key"])
    if json.dumps(live, sort_keys=True) != json.dumps(row["new"], sort_keys=True):
        route_log.set_applied_status(aid, "superseded", {"live": live})
        return False, "the setting was changed since; nothing to roll back"
    _old, err = router_config.write({row["key"]: row["old"]})
    if err:
        return False, err
    res = dict(result or {}, reason=reason)
    route_log.set_applied_status(aid, "rolled_back", res)
    audit_log(None, action="router.tune.rollback", resource=row["key"], permission_key="settings.router.configure",
              detail={"applied": aid, "by": who, "restored": row["old"], **res})
    print(f"[router_tuner] rolled back {row['key']} -> {row['old']} ({reason})", file=sys.stderr)
    return True, "rolled back"


def evaluate_applied(now: float = None) -> list:
    """Judge every change under watch against its baseline. -> [(id, verdict)]."""
    now = now or time.time()
    out = []
    for row in route_log.list_applied("watching", limit=20):
        before = row["baseline"] or {"runs": 0, "bad": 0}
        after = route_log.run_quality(row["category"], since=row["applied_at"], limit=MAX_AFTER)
        b_rate, a_rate = _rate(before), _rate(after)
        summary = {"before": before, "after": after,
                   "before_rate": None if b_rate is None else round(b_rate, 3),
                   "after_rate": None if a_rate is None else round(a_rate, 3)}
        if after["runs"] >= MIN_AFTER and b_rate is not None:
            if a_rate > b_rate + WORSE_BY and after["bad"] >= MIN_BAD_AFTER:
                ok, _msg = rollback_applied(row["id"], "auto", "quality_worse", summary)
                out.append((row["id"], "rolled_back" if ok else "superseded"))
            else:
                route_log.set_applied_status(row["id"], "kept", summary)
                audit_log(None, action="router.tune.keep", resource=row["key"],
                          permission_key="settings.router.configure", detail={"applied": row["id"], **summary})
                out.append((row["id"], "kept"))
        elif now - row["applied_at"] > WATCH_DAYS * 86400:
            route_log.set_applied_status(row["id"], "kept", dict(summary, note="too little traffic to judge"))
            out.append((row["id"], "kept"))
    return out


def tuner_cycle(last_tune: float, now: float = None) -> float:
    """One background tick: judge applied changes every time; suggest (and optionally
    auto-apply) once per TUNE_INTERVAL_S. Returns the new last-tune timestamp."""
    now = now or time.time()
    evaluate_applied(now)
    if now - last_tune >= TUNE_INTERVAL_S:
        ids = run_tuner()
        if ids:
            print(f"[router_tuner] {len(ids)} routing suggestion(s) pending admin review", file=sys.stderr)
        auto_apply(now)
        return now
    return last_tune


async def tuner_background_task() -> None:
    """Hourly: judge applied changes. Daily: suggestion pass (applies nothing unless
    router.auto_apply is on)."""
    await asyncio.sleep(120)
    last_tune = 0.0
    while True:
        try:
            # a handful of indexed aggregate queries: run on the loop thread, which
            # owns every other write to the shared usage.db connection
            last_tune = tuner_cycle(last_tune)
        except Exception as e:
            print(f"[router_tuner] tuning pass failed: {e}", file=sys.stderr)
        await asyncio.sleep(CHECK_INTERVAL_S)
