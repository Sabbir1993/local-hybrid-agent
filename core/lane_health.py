"""Run-time health of the agent lanes, learned from what recent turns did.

Every executor turn is recorded as ok / not ok (core/step_outcome.py). A rolling
window per (lane, category) gives a success rate, and consecutive failures on a
lane open a circuit breaker. The router asks `prefer_main()` before it hands a
request to the executor: a lane that is measurably failing on this kind of request
is skipped instead of being given one more chance to end the run.

Nothing is decided from text and nothing per-request touches the database: the
window is in memory and seeded once from core.route_log. A demoted lane is
re-tried after `cooldown_s` (half-open probe); one good turn closes the breaker
and lets the rate recover, so a lane is never written off permanently.
"""

import threading
import time
from collections import defaultdict, deque
from typing import Optional

from . import step_outcome

WINDOW = 20             # turns kept per (lane, category)
MIN_SAMPLES = 10        # below this a rate is not trusted
MIN_SUCCESS = 0.7       # rate below which a category is sent to main
BREAKER_FAILURES = 4    # consecutive failed turns that open a lane's breaker
COOLDOWN_S = 300        # wait before a demoted lane is probed again

_ACTION_CATEGORIES = ("action", "creation")


def turn_ok(outcome: Optional[str], category: str, step: int, escalated: bool) -> bool:
    """Did this model turn do its job?

    A step-0 reply with no tool call fails an action/creation request but is a fine
    final answer later in a run, so only step 0 is judged on that. Transport, parse
    and loop failures fail at any step."""
    if escalated:
        return False
    if outcome in (step_outcome.TRANSPORT_ERROR, step_outcome.PARSE_FAIL, step_outcome.LOOP):
        return False
    if step == 0 and category in _ACTION_CATEGORIES and outcome in (
            step_outcome.NO_TOOL_CALL, step_outcome.EMPTY, step_outcome.FINISH):
        return False
    return True


class LaneHealth:
    def __init__(self, window: int = WINDOW, clock=time.time):
        self._window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._turns = defaultdict(lambda: deque(maxlen=self._window))   # (lane, category) -> bools
        self._streak = defaultdict(int)                                 # lane -> consecutive failures
        self._opened_at = {}                                            # lane -> when breaker opened
        self._probed_at = {}                                            # (lane, category) -> last probe

    # -------------------------------------------------------------- record
    def record(self, lane: str, category: str, ok: bool) -> None:
        with self._lock:
            self._turns[(lane, category)].append(bool(ok))
            if ok:
                self._streak[lane] = 0
                self._opened_at.pop(lane, None)
            else:
                self._streak[lane] += 1
                if self._streak[lane] >= BREAKER_FAILURES and lane not in self._opened_at:
                    self._opened_at[lane] = self._clock()

    def record_step(self, lane: str, category: str, outcome: Optional[str], step: int,
                    escalated: bool) -> None:
        self.record(lane, category, turn_ok(outcome, category, step, escalated))

    # -------------------------------------------------------------- read
    def success_rate(self, lane: str, category: str, min_samples: int = MIN_SAMPLES) -> Optional[float]:
        with self._lock:
            w = self._turns.get((lane, category))
            if not w or len(w) < min_samples:
                return None
            return sum(w) / len(w)

    def breaker_open(self, lane: str) -> bool:
        with self._lock:
            return lane in self._opened_at

    def prefer_main(self, lane: str, category: str, *, min_success: float = MIN_SUCCESS,
                    min_samples: int = MIN_SAMPLES, cooldown_s: float = COOLDOWN_S) -> str:
        """'' to use `lane`, else the reason to send this request to main.

        After `cooldown_s` one request is let through to the demoted lane as a probe
        (half-open); its outcome is recorded like any other turn."""
        now = self._clock()
        with self._lock:
            reason = ""
            opened = self._opened_at.get(lane)
            if opened is not None:
                reason = "breaker_open"
            else:
                w = self._turns.get((lane, category))
                if w and len(w) >= min_samples and sum(w) / len(w) < min_success:
                    reason = "adaptive_low_success"
            key = (lane, category)
            if not reason:
                self._probed_at.pop(key, None)     # healthy again: a later demotion starts a fresh cooldown
                return ""
            last = self._probed_at.get(key, opened)
            if last is None:
                # first time this category is demoted: the cooldown starts now
                self._probed_at[key] = now
                return reason
            if now - last >= cooldown_s:
                self._probed_at[key] = now
                if opened is not None:
                    self._opened_at[lane] = now      # keep the breaker open until a turn succeeds
                return ""
            return reason

    def snapshot(self) -> dict:
        with self._lock:
            lanes = {}
            for (lane, cat), w in self._turns.items():
                d = lanes.setdefault(lane, {"breaker_open": lane in self._opened_at,
                                            "consecutive_failures": self._streak.get(lane, 0),
                                            "categories": {}})
                d["categories"][cat] = {"turns": len(w),
                                        "success_rate": round(sum(w) / len(w), 3) if w else None}
            return lanes

    def reset(self) -> None:
        with self._lock:
            self._turns.clear()
            self._streak.clear()
            self._opened_at.clear()
            self._probed_at.clear()

    # -------------------------------------------------------------- seed
    def hydrate(self, rows) -> None:
        """Seed from (lane, category, outcome, step, escalated) rows, oldest first."""
        for lane, category, outcome, step, escalated in rows:
            self.record_step(lane, category or "other", outcome, step or 0, bool(escalated))


health = LaneHealth()
_hydrated = False


def ensure_hydrated() -> None:
    """Seed the shared tracker once from recent route_events (best effort)."""
    global _hydrated
    if _hydrated:
        return
    _hydrated = True
    try:
        from . import route_log
        health.hydrate(route_log.recent_turns())
    except Exception as e:      # health is advisory: never block a request on it
        import sys
        print(f"[lane_health] hydrate failed: {e}", file=sys.stderr)
