import json
import time
from typing import Optional

_monitor_state = {
    "active": {},      # id -> request info (in flight)
    "recent": [],      # last N completed/failed requests
    "seq": 0,
}
MONITOR_RECENT_MAX = 60


def monitor_begin(endpoint: str, stream: bool, body_bytes: bytes) -> int:
    _monitor_state["seq"] += 1
    rid = _monitor_state["seq"]
    prompt_tok = None
    client = None
    try:
        d = json.loads(body_bytes)
        if isinstance(d, dict):
            n_msgs = len(d.get("messages", [])) or None
            prompt_tok = n_msgs
            client = d.get("client") or d.get("user")
    except Exception:
        pass
    _monitor_state["active"][rid] = {
        "id": rid,
        "endpoint": endpoint,
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


def monitor_end(rid: int, status: int, prompt_tokens=None, completion_tokens=None, tps=None, duration=None, prompt_tps=None) -> None:
    req = _monitor_state["active"].pop(rid, None)
    if req is None:
        return
    req.update({
        "status": status, "prompt_tokens": prompt_tokens or req.get("prompt_tokens"),
        "completion_tokens": completion_tokens or req.get("gen_tokens"),
        "tps": tps, "duration_s": duration or (time.time() - req["start"]),
        "prompt_tps": prompt_tps,
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
