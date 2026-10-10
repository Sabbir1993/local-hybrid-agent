"""Progress-aware loop detection for the agent loop.

The old rule counted calls per tool NAME in a sliding window, so a long debugging
session of different shell commands (each returning new information) looked identical to
a model hammering one call. Here a run is stuck only when its calls stop producing
anything new:

  identical_result  the same call (tool + args) returned the same result N times
  error_streak      the last N calls all failed
  cycle             the same sequence of (call, result) pairs repeats (A B A B A B)

Different arguments that return new information are progress and never count. The response
is graduated instead of an immediate stop:

  1st trigger   nudge     tell the model what is repeating and to change approach
  2nd trigger   escalate  the next few steps run on the main model
  3rd trigger   stop      end the run (the caller writes a summary of what was found)

After a trigger only the calls made since then are judged (and not before `grace_calls` new
ones), so the model gets a real chance to react to the nudge: a model that changes course keeps
going, one that repeats itself is escalated and then stopped.

Pure logic, no I/O: routes/agent/run.py records each executed call and asks for a verdict.
Limits live in config/app.json -> agent.loop and fall back to DEFAULTS.
"""

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

DEFAULTS = {
    "enabled": True,
    "identical_result": 3,      # same call, same result, this many times
    "error_streak": 6,          # this many failed calls in a row
    "cycle_repeats": 2,         # a 2- or 3-call pattern of (call, result) pairs repeated this many times
    "grace_calls": 3,           # calls to wait after a trigger before judging again
    "escalate_steps": 3,        # steps forced onto main after the second trigger
    "graduated": True,          # False: stop at the first trigger (only when "stop" is on)
    "stop": False,              # True: end the run at the third trigger. Off by default: a run that
                                # keeps repeating is warned and moved to the main model, and only the
                                # step and time budgets end it - a review that legitimately re-reads
                                # things must not be cut off
}

OK, NUDGE, ESCALATE, STOP = "ok", "nudge", "escalate", "stop"


@dataclass
class Verdict:
    level: str = OK
    rule: str = ""
    tool: str = ""
    count: int = 0

    @property
    def detail(self) -> str:
        """Compact code for logs: 'identical_result:run_python:3'."""
        return f"{self.rule}:{self.tool}:{self.count}" if self.rule else ""


def _sig(args) -> str:
    try:
        return json.dumps(args, sort_keys=True, default=str)[:600]
    except (TypeError, ValueError):
        return str(args)[:600]


def _rhash(result) -> str:
    """Hash of a tool result with whitespace collapsed (a re-run that only reflows output is the
    same result). Only the head is hashed: the tail of very long output is often a timestamp."""
    text = re.sub(r"\s+", " ", str(result if result is not None else "")).strip()[:4000]
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def config(raw: Optional[dict] = None) -> dict:
    cfg = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k in DEFAULTS and v is not None:
            cfg[k] = v
    return cfg


class LoopGuard:
    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = config(cfg)
        self.calls = []            # (name, args_sig, result_hash, ok)
        self.triggers = 0
        self._start = 0            # calls before this index were already judged (see check)

    # ------------------------------------------------------------------ input
    def record(self, name: str, args, result, ok: bool) -> None:
        self.calls.append((str(name), _sig(args), _rhash(result), bool(ok)))

    # ------------------------------------------------------------------ rules
    def _detect(self) -> Optional[tuple]:
        # only what happened since the last trigger: a model that changed course after a nudge
        # must not be judged again on the calls it was already warned about
        calls, cfg = self.calls[self._start:], self.cfg
        if not calls:
            return None
        # the same call returning the same result again and again: nothing new is being learned
        n_ident = int(cfg["identical_result"])
        if n_ident > 0:
            top, cnt = Counter((c[0], c[1], c[2]) for c in calls).most_common(1)[0]
            if cnt >= n_ident:
                return "identical_result", top[0], cnt
        # a run of failures
        n_err = int(cfg["error_streak"])
        if n_err > 0 and len(calls) >= n_err and not any(c[3] for c in calls[-n_err:]):
            return "error_streak", calls[-1][0], n_err
        # the same short sequence of (call, result) pairs, repeated
        reps = int(cfg["cycle_repeats"])
        if reps > 1:
            keys = [(c[0], c[1], c[2]) for c in calls]
            for period in (2, 3):
                need = period * reps
                if len(keys) >= need:
                    tail = keys[-need:]
                    unit = tail[:period]
                    # a pattern of one repeated call is the identical_result rule's job
                    if len(set(unit)) > 1 and all(tail[i] == unit[i % period] for i in range(need)):
                        return "cycle", unit[0][0], reps
        return None

    # ------------------------------------------------------------------ output
    def check(self) -> Verdict:
        """Judge the run so far. Call after each step's tools have been recorded."""
        if not self.cfg["enabled"]:
            return Verdict()
        if self.triggers and len(self.calls) - self._start < int(self.cfg["grace_calls"]):
            return Verdict()
        found = self._detect()
        if not found:
            return Verdict()
        rule, tool, count = found
        self.triggers += 1
        self._start = len(self.calls)
        if self.cfg["stop"] and (not self.cfg["graduated"] or self.triggers >= 3):
            level = STOP
        else:
            level = NUDGE if self.triggers == 1 else ESCALATE
        return Verdict(level, rule, tool, count)


def describe(rule: str, tool: str, count: int) -> str:
    """Plain-language reason, for the nudge, the stop summary and the UI banner."""
    if rule == "identical_result":
        return f"`{tool}` was called {count} times with the same arguments and gave the same result each time"
    if rule == "error_streak":
        return f"the last {count} tool calls all failed"
    if rule == "cycle":
        return f"the run kept going back and forth between the same calls (starting with `{tool}`), {count} times over"
    if rule == "no_action":
        return f"the last {count} steps were only planning and looking things up, with nothing written or run"
    return "the run stopped making progress"


def nudge_message(v: Verdict) -> str:
    return (f"[stuck] {describe(v.rule, v.tool, v.count).capitalize()}, so nothing new is being learned. "
            "Do not repeat those calls. Change approach: use a different tool or different inputs, "
            "look at the actual error, or summarise what you have found so far. If you are missing "
            "something only the user can provide, say exactly what it is and stop.")
