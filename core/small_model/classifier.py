"""Laya as a small fixed-choice classifier for routing decisions (System 1).

The step-0 tool shortcut in router.py asks Laya to pick one of ~48 tools; measured, that
never produced a call. Here it answers two questions with 3 options each, which is what a
sequence classifier is good at:

  request_profile(query) -> tools | create | chat
      does this request need the agent to DO something, produce content, or just talk?
  reply_state(reply)     -> announced | other
      did the reply only announce the next step (no tool call follows), or does it give a result/question?

Rules stay the source of truth and the fallback: every function returns None when the
classifier is off, not loaded yet, slow (hard timeout), low-confidence, or failing, and the
caller then uses the rule-based decision. So the classifier can refine routing but can never
block or delay it beyond the timeout.

Config (config/app.json -> router.classifier):
    enabled     false   master switch
    mode        "shadow"  shadow = compute and log only; active = the answer changes routing
    active_questions ["request_profile"]  which questions may act in active mode (reply_state
                    did not pass the held-out eval, so it is only ever logged)
    timeout_s   2.0     per call; a slower call is abandoned (returns None)
    thresholds  {request_profile: 0.6, reply_state: 0.7}   minimum probability per question
Thresholds come from scripts/eval_classifier.py: the checkpoint reports its confidences as
uncalibrated, so they are chosen on labelled data, not trusted at face value.
"""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Optional

from .config import APP_CONFIG

DEFAULT_CFG = {
    "enabled": False,
    "mode": "shadow",
    # questions that may change routing when mode is "active"; the others are only logged.
    # A question belongs here only after scripts/eval_classifier.py passes for it.
    "active_questions": ["request_profile"],
    "timeout_s": 2.0,
    "thresholds": {"request_profile": 0.6, "reply_state": 0.7},
}

QUESTIONS = {
    "request_profile": {
        "instructions": "What does the user's message ask the assistant to do?",
        "criteria": {
            "tools": ("run a command, open or read a file, test or inspect an app or website, search, "
                      "install, or change something on the computer"),
            "create": "write new code, text, a document or a story from scratch",
            "chat": "answer a general question, explain a concept, small talk, a greeting or thanks",
        },
    },
    "reply_state": {
        "instructions": "What is this assistant reply doing?",
        "criteria": {
            "announced": ("says it will do something next, like let me, I'll, first I will, "
                          "and gives no result yet"),
            "other": "gives a result, an answer or findings, or asks the user a question",
        },
    },
}

# Wording chosen on tests/data/classifier_eval.jsonl (scripts/eval_classifier.py): the choice
# descriptions decide what the checkpoint calls each option, so change them only with the eval.

_lock = threading.Lock()            # the underlying model is not built for concurrent predict()
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="laya-clf")
_router = None
_router_failed_at = 0.0
_RETRY_AFTER_S = 120.0              # a failed load is retried, never latched for the process


def config() -> dict:
    cfg = dict(DEFAULT_CFG)
    user = ((APP_CONFIG.get("router") or {}).get("classifier")) or {}
    cfg.update({k: v for k, v in user.items() if k != "thresholds"})
    cfg["thresholds"] = {**DEFAULT_CFG["thresholds"], **(user.get("thresholds") or {})}
    return cfg


def enabled() -> bool:
    return bool(config().get("enabled"))


def active(question: Optional[str] = None) -> bool:
    """True when this question's answers may change routing (not just be logged)."""
    c = config()
    if not (c.get("enabled") and c.get("mode") == "active"):
        return False
    return question is None or question in (c.get("active_questions") or [])


def _get_router():
    """The shared, lazily built Laya router (CPU). None if it cannot be built right now."""
    global _router, _router_failed_at
    import time
    if _router is not None:
        return _router
    if _router_failed_at and time.time() - _router_failed_at < _RETRY_AFTER_S:
        return None
    try:
        import laya
        laya_cfg = (APP_CONFIG.get("router") or {}).get("laya") or {}
        if hasattr(laya, "Router"):
            _router = laya.Router(preload=bool(laya_cfg.get("preload", False)), device="cpu")
        else:
            _router = laya.load(laya_cfg.get("checkpoint", "convaiinnovations/laya"),
                                subfolder=laya_cfg.get("subfolder"), device="cpu")
        return _router
    except Exception as e:
        _router_failed_at = time.time()
        print(f"[classifier] laya unavailable ({e}); retrying in {int(_RETRY_AFTER_S)}s", file=sys.stderr)
        return None


def _predict(question: str, text: str) -> Optional[tuple]:
    """(label, confidence) or None. Runs under the lock, on the classifier thread."""
    with _lock:
        r = _get_router()
        if r is None:
            return None
        spec = {"answer": {"type": "choice", **QUESTIONS[question]}}
        state = {"query": text, "body": text}
        pred = r.predict(state, spec) if hasattr(r, "predict") else r(state, spec)
        ans = (pred.get("answers") or {}).get("answer") or {}
        label = ans.get("choice") or ans.get("answer")
        # `confidence` in Laya's reply is a separate, much lower score (0.009 for a clear-cut
        # answer); the probability of the chosen option is answer_confidence / probabilities[label]
        probs = ans.get("probabilities") or {}
        conf = ans.get("answer_confidence")
        if conf is None:
            conf = probs.get(label, 0.0)
        conf = float(conf)
        if label not in QUESTIONS[question]["criteria"]:
            return None
        return label, conf


def classify(question: str, text: str, *, timeout_s: Optional[float] = None) -> Optional[dict]:
    """{"label", "confidence", "confident"} or None (off / slow / failing / nothing to classify).

    `confident` is the label's confidence against this question's threshold; callers act on
    the label only when it is True."""
    cfg = config()
    if not cfg.get("enabled") or not (text or "").strip() or question not in QUESTIONS:
        return None
    limit = float(timeout_s if timeout_s is not None else cfg.get("timeout_s", 2.0))
    text = text.strip()[:1500]
    try:
        res = _pool.submit(_predict, question, text).result(timeout=limit)
    except FutureTimeout:
        return None                 # too slow right now (cold load, busy CPU): use the rules
    except Exception as e:
        print(f"[classifier] {question} failed: {e}", file=sys.stderr)
        return None
    if res is None:
        return None
    label, conf = res
    thr = float((cfg.get("thresholds") or {}).get(question, 0.8))
    return {"label": label, "confidence": round(conf, 4), "confident": conf >= thr}


# ---------------------------------------------------------------- the two questions

_PROFILE_TO_CATEGORY = {"tools": "action", "create": "creation", "chat": "question"}


def request_profile(query: str) -> Optional[dict]:
    """Category the classifier reads from the request ("action" / "creation" / "question")."""
    r = classify("request_profile", query)
    if r:
        r["category"] = _PROFILE_TO_CATEGORY.get(r["label"])
    return r


def reply_state(reply: str) -> Optional[dict]:
    return classify("reply_state", reply)


def warm() -> None:
    """Build the model off the request path (called from a background thread at start-up)."""
    if not enabled():
        return
    try:
        with _lock:
            _get_router()
        classify("request_profile", "warm up", timeout_s=30)
    except Exception as e:
        print(f"[classifier] warm-up failed: {e}", file=sys.stderr)


def log_code(profile: Optional[dict], reply: Optional[dict] = None) -> Optional[str]:
    """Compact code for route_events.clf: 'p:tools@0.93|r:asked@0.88'. Labels and confidence only."""
    parts = []
    if profile:
        parts.append(f"p:{profile['label']}@{profile['confidence']:.2f}")
    if reply:
        parts.append(f"r:{reply['label']}@{reply['confidence']:.2f}")
    return "|".join(parts) or None
