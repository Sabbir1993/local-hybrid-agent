import json
import time
from typing import Optional

_monitor_state = {
    "active": {},      # id -> request info (in flight)
    "recent": [],      # last N completed/failed requests
    "seq": 0,
}
MONITOR_RECENT_MAX = 60


def monitor_begin(endpoint: str, stream: bool, body_bytes: bytes, model: Optional[str] = None) -> int:
    _monitor_state["seq"] += 1
    rid = _monitor_state["seq"]
    prompt_tok = None
    client = None
    req_model = model
    try:
        d = json.loads(body_bytes)
        if isinstance(d, dict):
            n_msgs = len(d.get("messages", [])) or None
            prompt_tok = n_msgs
            client = d.get("client") or d.get("user")
            if not req_model and d.get("model"):
                req_model = d.get("model")
    except Exception:
        pass
    _monitor_state["active"][rid] = {
        "id": rid,
        "endpoint": endpoint,
        "model": req_model,
        "stream": stream,
        "start": time.time(),
        "first_token_s": None,
        "prompt_tokens": prompt_tok,
        "client": client,
        "last_token_s": time.time(),
        "gen_tps": 0.0,
        "gen_tokens": 0,
    }
    return rid


def monitor_token(rid: int, n: int = 1) -> None:
    req = _monitor_state["active"].get(rid)
    if not req:
        return
    now = time.time()
    if req.get("first_token_s") is None:
        req["first_token_s"] = now
    req["gen_tokens"] += n

    prev_token_s = req.get("last_token_s") or now
    dt = max(0.001, now - prev_token_s)
    req["last_token_s"] = now

    gen_duration = max(0.05, now - req["first_token_s"])
    gen_rate = req["gen_tokens"] / gen_duration
    instant_tps = n / dt

    if req.get("gen_tps", 0.0) == 0.0:
        req["gen_tps"] = instant_tps
    else:
        clamped_instant = max(0.5, min(250.0, instant_tps))
        ema = req["gen_tps"] * 0.7 + clamped_instant * 0.3
        req["gen_tps"] = ema * 0.5 + gen_rate * 0.5


def monitor_end(rid: int, status: int, prompt_tokens=None, completion_tokens=None, tps=None, duration=None, prompt_tps=None, model=None, prompt_cached=None, completion_cached=None) -> None:
    req = _monitor_state["active"].pop(rid, None)
    if req is None:
        return
    req.update({
        "status": status, "prompt_tokens": prompt_tokens or req.get("prompt_tokens"),
        "completion_tokens": completion_tokens or req.get("gen_tokens"),
        "tps": tps, "duration_s": duration or (time.time() - req["start"]),
        "prompt_tps": prompt_tps,
        "model": model or req.get("model"),
        "prompt_cached": prompt_cached or req.get("prompt_cached", 0),
        "completion_cached": completion_cached or req.get("completion_cached", 0),
        "end": time.time(),
    })
    _monitor_state["recent"].append(req)
    if len(_monitor_state["recent"]) > MONITOR_RECENT_MAX:
        _monitor_state["recent"] = _monitor_state["recent"][-MONITOR_RECENT_MAX:]


class RequestMonitor:
    def __init__(self, rid: int):
        self.rid = rid
        self.tokens = 0
        self.last = time.time()

    def add(self, n: int = 1) -> None:
        self.tokens += n
        monitor_token(self.rid, n)


def parse_cache_tokens(usage: Optional[dict], timings: Optional[dict] = None) -> tuple[int, int]:
    """Extract (prompt_cached_tokens, completion_cached_tokens) from usage/timings dictionaries."""
    prompt_cached = 0
    completion_cached = 0

    if isinstance(usage, dict):
        # 1. Input / prompt cache read
        pt_details = usage.get("prompt_tokens_details") or {}
        if isinstance(pt_details, dict) and pt_details.get("cached_tokens") is not None:
            prompt_cached = int(pt_details.get("cached_tokens") or 0)
        elif usage.get("cached_tokens") is not None:
            prompt_cached = int(usage.get("cached_tokens") or 0)
        elif usage.get("tokens_cached") is not None:
            prompt_cached = int(usage.get("tokens_cached") or 0)
        elif usage.get("cache_read_input_tokens") is not None:
            prompt_cached = int(usage.get("cache_read_input_tokens") or 0)

        # 2. Output / completion cache read (speculative decoding / draft model / KV cache reuse)
        ct_details = usage.get("completion_tokens_details") or {}
        if isinstance(ct_details, dict):
            if ct_details.get("accepted_prediction_tokens") is not None:
                completion_cached = int(ct_details.get("accepted_prediction_tokens") or 0)
            elif ct_details.get("cached_tokens") is not None:
                completion_cached = int(ct_details.get("cached_tokens") or 0)
        if not completion_cached:
            if usage.get("draft_tokens_accepted") is not None:
                completion_cached = int(usage.get("draft_tokens_accepted") or 0)
            elif usage.get("accepted_prediction_tokens") is not None:
                completion_cached = int(usage.get("accepted_prediction_tokens") or 0)

    if isinstance(timings, dict):
        if not prompt_cached:
            if timings.get("tokens_cached") is not None:
                prompt_cached = int(timings.get("tokens_cached") or 0)
            elif isinstance(timings.get("prompt_tokens_details"), dict):
                prompt_cached = int(timings["prompt_tokens_details"].get("cached_tokens") or 0)
        if not completion_cached:
            if timings.get("n_accepted") is not None:
                completion_cached = int(timings.get("n_accepted") or 0)

    return prompt_cached, completion_cached


def extract_usage_from_stream(chunk_text: str, rid: int) -> Optional[dict]:
    usage = None
    for line in chunk_text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            j = json.loads(payload)
            if isinstance(j.get("usage"), dict):
                usage = j["usage"]
                pc, cc = parse_cache_tokens(usage, j.get("timings"))
                req = _monitor_state["active"].get(rid)
                if req:
                    if pc: req["prompt_cached"] = pc
                    if cc: req["completion_cached"] = cc
            ch = j.get("choices") or []
            if ch:
                delta = ch[0].get("delta") or {}
                n = 0
                if delta.get("content"):
                    n += 1
                if delta.get("reasoning_content"):
                    n += 1
                if n:
                    monitor_token(rid, n)
        except Exception:
            pass
    return usage
