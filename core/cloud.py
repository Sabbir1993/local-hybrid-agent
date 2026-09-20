"""
core/cloud.py - Cloud (OpenAI-compatible) provider registry and lane routing.

Cloud providers are managed per-user (each signed-in user configures their own
API keys/models/lane bindings -- there is no shared team-wide cloud config, by
design: "keep cloud in the user's hand"). Two sources are merged per user:

  config/app.json         -> "provider": {...}, "cloud": {...}
                              (tracked by git: non-secret defaults + copy-paste
                              portability; a shared *starting point*, not a
                              shared runtime config)
  config/providers/user_<id>.json
                           -> same shape, written by that user's own UI
                              actions; untracked so keys never get committed,
                              and never readable by any other user.

A lane (main / executor / vision) is served by the cloud when its binding
resolves to a configured model; otherwise the lane stays local (llama-server,
which *is* shared hardware -- unlike cloud credentials, one GPU rig has one
loaded model for everyone).  `lane_kind()` / `lane_client()` are the single
place that decision is made.

Whose config is "current" when a function is called without an explicit
user_id: request-scoped call sites (routes/cloud.py, routes/control.py,
routes/chat.py, routes/agent.py) always pass user.id explicitly. A few deeper,
harder-to-thread call sites (git commit-message generation, vision describe,
the raw reverse proxy) fall back to core.request_context.get_current_user_id()
-- the same per-request global already used for per-user memory-search
isolation -- so they still resolve to whoever's request is in flight instead
of a hardcoded shared file.
"""

import json
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

import httpx

from .config import (
    CLOUD_LANES,
    CLOUD_PROBE_TIMEOUT_S,
    CLOUD_TIMEOUT_S,
    CONFIG_FILE,
    PROVIDERS_DIR,
)
from .request_context import get_current_user_id

# Keys llama.cpp accepts but OpenAI-compatible clouds reject or handle oddly
_LLAMA_ONLY_KEYS = ("repeat_penalty", "grammar", "min_p", "top_k", "typical_p",
                    "xtc_probability", "xtc_threshold", "n_keep", "cache_prompt")

_CACHE: dict = {}   # resolved user_id (int) or "_shared" -> merged {provider, cloud} dict
_CLIENTS: dict = {}
_WARNED: set = set()


def _resolve_user(user_id: Optional[int]) -> Optional[int]:
    return user_id if user_id is not None else get_current_user_id()


def _provider_file(user_id: int) -> Path:
    return PROVIDERS_DIR / f"user_{user_id}.json"


def _read_json(p: Path) -> dict:
    try:
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception as e:
        print(f"[cloud] {p.name} unreadable: {e}")
    return {}


def _merge_sections(base_cfg: dict, override_cfg: dict) -> dict:
    out = {"provider": {}, "cloud": {}}
    for src in (base_cfg, override_cfg):
        prov = src.get("provider")
        if isinstance(prov, dict):
            for name, cfg in prov.items():
                if not isinstance(cfg, dict):
                    continue
                merged = out["provider"].setdefault(name, {})
                merged.update(deepcopy(cfg))
                if isinstance(cfg.get("options"), dict):
                    merged["options"] = {**merged.get("options", {}),
                                         **deepcopy(cfg["options"])}
                if isinstance(cfg.get("models"), dict):
                    m = merged.setdefault("models", {})
                    for mid, mcfg in cfg["models"].items():
                        m[mid] = deepcopy(mcfg) if isinstance(mcfg, dict) else {}
        cl = src.get("cloud")
        if isinstance(cl, dict):
            out["cloud"].update(deepcopy(cl))
    return out


def _merged(user_id: Optional[int] = None) -> dict:
    uid = _resolve_user(user_id)
    cache_key = uid if uid is not None else "_shared"
    if cache_key not in _CACHE:
        override = _read_json(_provider_file(uid)) if uid is not None else {}
        _CACHE[cache_key] = _merge_sections(_read_json(CONFIG_FILE), override)
    return _CACHE[cache_key]


def reload(user_id: Optional[int] = None) -> None:
    """Drop cached config after a user's providers file / config/app.json changed
    on disk. No user_id clears every cached user (startup / config/app.json edits)."""
    global _CACHE
    if user_id is None:
        _CACHE = {}
    else:
        _CACHE.pop(user_id, None)
    _WARNED.clear()
    _CLIENTS.clear()


class CloudModel:
    """One provider/model pair that can serve a lane."""

    def __init__(self, provider: str, provider_cfg: dict, model_id: str, model_cfg: dict):
        self.provider = provider
        self.provider_cfg = provider_cfg or {}
        self.model_id = model_id
        self.model_cfg = model_cfg or {}
        opts = self.provider_cfg.get("options") or {}
        self.base_url = str(opts.get("baseURL") or opts.get("base_url") or "").strip()
        self.api_key = str(opts.get("apiKey") or opts.get("api_key") or "").strip()
        self.chat_path = str(opts.get("chat_path") or "").strip()
        self.stream_options = bool(opts.get("stream_options", False))
        eb = opts.get("extra_body")
        self.extra_body = eb if isinstance(eb, dict) else {}
        # Per-provider extra HTTP headers (OpenRouter requires HTTP-Referer /
        # X-Title; other providers may need X-API-Version, etc.)
        eh = opts.get("extra_headers")
        self.extra_headers = {str(k): str(v) for k, v in eh.items()} if isinstance(eh, dict) else {}
        try:
            self.timeout_s = float(opts.get("timeout_s") or CLOUD_TIMEOUT_S)
        except (TypeError, ValueError):
            self.timeout_s = CLOUD_TIMEOUT_S

    @property
    def provider_name(self) -> str:
        return str(self.provider_cfg.get("name") or self.provider)

    @property
    def display(self) -> str:
        return str(self.model_cfg.get("name") or self.model_id)

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model_id}"

    @property
    def ctx(self) -> int:
        try:
            return int(self.model_cfg.get("ctx") or self.provider_cfg.get("ctx") or 32768)
        except (TypeError, ValueError):
            return 32768

    def endpoint(self) -> str:
        """Absolute chat-completions URL for this provider.

        Handles the two common baseURL shapes:
          https://openrouter.ai/api/v1  -> .../api/v1/chat/completions
          https://api.groq.com/openai   -> .../openai/v1/chat/completions
        """
        base = self.base_url.rstrip("/")
        if self.chat_path:
            return base + "/" + self.chat_path.lstrip("/")
        if not base:
            return ""
        if re.search(r"/v\d+$", base):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def info(self, lane: Optional[str] = None) -> dict:
        return {
            "lane": lane,
            "model": self.model_id,
            "display": f"☁️ {self.display}",
            "device": f"{self.provider_name} (cloud)",
            "role": "Cloud lane",
            "source": "cloud",
            "provider": self.provider,
            "provider_name": self.provider_name,
            "key": self.key,
        }

    def label(self) -> str:
        return f"{self.display} ({self.provider_name})"

    def __repr__(self) -> str:
        return f"<CloudModel {self.key} @ {self.base_url}>"


def providers(user_id: Optional[int] = None) -> dict:
    return _merged(user_id)["provider"]


def cloud_bindings(user_id: Optional[int] = None) -> dict:
    cl = _merged(user_id)["cloud"]
    out = {lane: (str(cl.get(lane) or "").strip() or None) for lane in CLOUD_LANES}
    out["fallback_local"] = bool(cl.get("fallback_local", True))
    mode = str(cl.get("routing_mode") or "auto").strip().lower()
    out["routing_mode"] = mode if mode in ("auto", "custom") else "auto"
    return out


def cloud_models(user_id: Optional[int] = None) -> list:
    out = []
    for name, cfg in providers(user_id).items():
        if not isinstance(cfg, dict):
            continue
        models = cfg.get("models")
        if not isinstance(models, dict):
            continue
        for mid, mcfg in models.items():
            cm = CloudModel(name, cfg, str(mid), mcfg)
            if cm.endpoint():
                out.append(cm)
    return out


def get_cloud(key: Optional[str], user_id: Optional[int] = None) -> Optional[CloudModel]:
    """Resolve "<provider>/<model-id>" (provider names may contain spaces)
    among the given user's own configured providers. user_id defaults to the
    current request's user (core.request_context) when not given explicitly."""
    if not key:
        return None
    key = str(key).strip()
    if key.startswith("cloud:"):
        key = key[6:]
    for cm in cloud_models(user_id):
        if cm.key == key:
            return cm
    return None


def cloud_lane(lane: str, user_id: Optional[int] = None) -> Optional[CloudModel]:
    b = cloud_bindings(user_id)
    # Auto routing: executor/vision follow whatever the main lane is bound to
    # (cloud model -> same cloud model; local -> local). Custom lets each lane
    # be bound independently, as before.
    if lane in ("executor", "vision") and b.get("routing_mode", "auto") == "auto":
        key = b.get("main")
    else:
        key = b.get(lane)
    if not key:
        return None
    cm = get_cloud(key, user_id)
    if cm is None and key not in _WARNED:
        _WARNED.add(key)
        print(f"[cloud] lane '{lane}' points at '{key}' which is not configured - using local")
    return cm


def lane_kind(lane: str, user_id: Optional[int] = None) -> str:
    return "cloud" if cloud_lane(lane, user_id) else "local"


def lane_client(lane: str, user_id: Optional[int] = None):
    """CloudClient for a cloud-bound lane, else None (caller uses the local lane)."""
    cm = cloud_lane(lane, user_id)
    return CloudClient(cm) if cm else None


def mask_key(k: Optional[str]) -> str:
    if not k:
        return ""
    k = str(k)
    if len(k) <= 8:
        return "*" * len(k)
    return f"{k[:6]}{'*' * 10}{k[-4:]}"


def providers_public(user_id: Optional[int] = None) -> list:
    out = []
    for name, cfg in providers(user_id).items():
        if not isinstance(cfg, dict):
            continue
        opts = cfg.get("options") or {}
        models = cfg.get("models") or {}
        out.append({
            "provider": name,
            "name": str(cfg.get("name") or name),
            "npm": cfg.get("npm"),
            "base_url": str(opts.get("baseURL") or opts.get("base_url") or ""),
            "key_masked": mask_key(opts.get("apiKey") or opts.get("api_key")),
            "has_key": bool(str(opts.get("apiKey") or opts.get("api_key") or "").strip()),
                        "extra_headers": opts.get("extra_headers") or {},
            "models": [{"id": str(mid), "name": str((mcfg or {}).get("name") or mid),
                        "ctx": (mcfg or {}).get("ctx")}
                       for mid, mcfg in (models.items() if isinstance(models, dict) else [])],
        })
    return out


def _client_for(cm: "CloudModel") -> httpx.AsyncClient:
    c = _CLIENTS.get(cm.key)
    if c is None:
        c = httpx.AsyncClient(timeout=httpx.Timeout(cm.timeout_s, connect=20.0))
        _CLIENTS[cm.key] = c
    return c


class CloudClient:
    """Drop-in stand-in for state.client / small-model clients.

    Exposes .stream() and .post() so _llm_chat_stream and the non-streaming
    summarizer path work unchanged. Injects the provider's model id and bearer
    token, and strips llama.cpp-only sampling fields.
    """

    is_cloud = True

    def __init__(self, cm: CloudModel):
        self.cm = cm
        self.base_url = cm.key          # unique id (used in labels/logs)

    @property
    def label(self) -> str:
        return self.cm.label()

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.cm.api_key:
            h["Authorization"] = f"Bearer {self.cm.api_key}"
        # Provider-specific headers (stored per provider in the UI).
        h.update(self.cm.extra_headers)
        # Many OpenAI-compatible gateways (OpenRouter and OpenRouter-style
        # resellers/proxies alike, e.g. agentrouter.org) reject requests without
        # a referer with 401 "unauthorized_client" / "UNAUTHENTICATED". Default
        # both headers unconditionally so users only need URL + key; a value
        # set in the provider's Extra request headers field always wins.
        h.setdefault("HTTP-Referer", "https://localhost/a770-dual-runtime")
        h.setdefault("X-Title", "a770-dual-runtime")
        return h

    def _prepare(self, payload):
        if not isinstance(payload, dict):
            return payload
        p = dict(payload)
        for k in _LLAMA_ONLY_KEYS:
            p.pop(k, None)
        p["model"] = self.cm.model_id
        mt = p.get("max_tokens")
        try:
            if mt is None or int(mt) <= 0:
                p.pop("max_tokens", None)   # OpenAI-compatible clouds reject -1
        except (TypeError, ValueError):
            p.pop("max_tokens", None)
        if p.get("stream") and self.cm.stream_options:
            p.setdefault("stream_options", {"include_usage": True})
        for k, v in self.cm.extra_body.items():
            p.setdefault(k, v)
        return p

    def _timeout(self, timeout):
        return self.cm.timeout_s if timeout is None else timeout

    def stream(self, method="POST", url="/v1/chat/completions", json=None, timeout=None, **kw):
        return _client_for(self.cm).stream(
            method, self.cm.endpoint(), json=self._prepare(json),
            headers=self.headers(), timeout=self._timeout(timeout), **kw)

    async def post(self, url="/v1/chat/completions", json=None, timeout=None, **kw):
        return await _client_for(self.cm).post(
            self.cm.endpoint(), json=self._prepare(json),
            headers=self.headers(), timeout=self._timeout(timeout), **kw)

    async def aclose(self):
        c = _CLIENTS.pop(self.cm.key, None)
        if c is not None:
            try:
                await c.aclose()
            except Exception:
                pass

    def __repr__(self) -> str:
        return f"<CloudClient {self.cm.key}>"


# ---------------- persistence (per-user providers/user_<id>.json) ----------------
# Mutations always take an explicit user_id -- a write must never fall back to
# an ambient "current user" guess, unlike the read-side lane/model lookups.

def _write_providers(user_id: int, cfg: dict) -> None:
    PROVIDERS_DIR.mkdir(parents=True, exist_ok=True)
    _provider_file(user_id).write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def save_provider(user_id: int, name: str, data: dict) -> dict:
    """Create/update one provider (API key included) in this user's own config.
    Persists + reloads that user's cache."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("provider name is required")
    cfg = _read_json(_provider_file(user_id))
    entry = (cfg.setdefault("provider", {})).setdefault(name, {})
    if data.get("npm") is not None:
        entry["npm"] = data["npm"]
    entry["name"] = str(data.get("name") or entry.get("name") or name)
    opts = entry.setdefault("options", {})
    if data.get("base_url"):
        opts["baseURL"] = str(data["base_url"]).strip()
    if data.get("api_key"):
        opts["apiKey"] = str(data["api_key"]).strip()
    if data.get("chat_path"):
        opts["chat_path"] = str(data["chat_path"]).strip()
    if isinstance(data.get("extra_headers"), dict) and data["extra_headers"]:
        opts["extra_headers"] = {str(k): str(v) for k, v in data["extra_headers"].items()}
    if isinstance(data.get("extra_body"), dict) and data["extra_body"]:
        opts["extra_body"] = data["extra_body"]
    if data.get("stream_options") is not None:
        opts["stream_options"] = bool(data["stream_options"])
    if isinstance(data.get("models"), list) and data["models"]:
        models = {}
        for m in data["models"]:
            mid = str((m or {}).get("id") or "").strip()
            if not mid:
                continue
            mc = {"name": str(m.get("name") or mid).strip()}
            try:
                if m.get("ctx"):
                    mc["ctx"] = int(m["ctx"])
            except (TypeError, ValueError):
                pass
            models[mid] = mc
        entry["models"] = {**(entry.get("models") or {}), **models}
    _write_providers(user_id, cfg)
    reload(user_id)
    return entry


def delete_model(user_id: int, provider: str, model_id: str) -> dict:
    """Remove one model from a provider and unbind any lane pointing at it."""
    provider = str(provider or "").strip()
    model_id = str(model_id or "").strip()
    key = f"{provider}/{model_id}"
    cfg = _read_json(_provider_file(user_id))
    entry = (cfg.get("provider") or {}).get(provider)
    if not isinstance(entry, dict):
        raise ValueError(f"provider not found: {provider}")
    models = entry.get("models")
    if not isinstance(models, dict) or model_id not in models:
        raise ValueError(f"model not found: {key}")
    del models[model_id]
    cl = cfg.setdefault("cloud", {})
    for lane in CLOUD_LANES:
        if cl.get(lane) == key:
            cl[lane] = None
    _write_providers(user_id, cfg)
    reload(user_id)
    return entry


def delete_provider(user_id: int, name: str) -> dict:
    """Remove a provider and unbind any lane that pointed at one of its models."""
    name = str(name or "").strip()
    cfg = _read_json(_provider_file(user_id))
    (cfg.get("provider") or {}).pop(name, None)
    cl = cfg.setdefault("cloud", {})
    for lane in CLOUD_LANES:
        v = cl.get(lane)
        if v and str(v).split("/")[0] == name:
            cl[lane] = None
    _write_providers(user_id, cfg)
    reload(user_id)
    return cfg


def set_lanes(user_id: int, updates: dict) -> dict:
    """Bind lanes to '<provider>/<model>' (None / 'local' unbinds)."""
    cfg = _read_json(_provider_file(user_id))
    cl = cfg.setdefault("cloud", {})
    for lane in CLOUD_LANES:
        if lane in updates:
            v = updates[lane]
            cl[lane] = None if v in (None, "", "local", "null") else str(v).strip()
    if "fallback_local" in updates:
        cl["fallback_local"] = bool(updates["fallback_local"])
    if "routing_mode" in updates:
        mode = str(updates["routing_mode"] or "auto").strip().lower()
        cl["routing_mode"] = mode if mode in ("auto", "custom") else "auto"
    _write_providers(user_id, cfg)
    reload(user_id)
    return cl


async def probe(cm: CloudModel, prompt: str = "ping") -> dict:
    """One tiny non-streaming completion - proves URL + key + model id work."""
    t0 = time.time()

    def _hint(status: int, body: str) -> str:
        """Actionable hint for the common gateway rejections."""
        b = (body or "").lower()
        if status in (401, 403):
            if ("referer" in b or "http-referer" in b or "unauthorized_client"
                    in b or "user not found" in b):
                return ("  Hint: this gateway rejected the request headers - add "
                        "'HTTP-Referer' (+ 'X-Title') in the provider's Extra request "
                        "headers field, then Test again.")
            return ("  Hint: bad/expired API key - press the eye button to reveal "
                    "it and compare with the provider dashboard.")
        if status == 404 and ("model" in b or "not found" in b):
            return "  Hint: model id not accepted - check spelling / availability."
        return ""

    try:
        r = await _client_for(cm).post(
            cm.endpoint(),
            json={"model": cm.model_id,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 16, "temperature": 0},
            headers=CloudClient(cm).headers(),
            timeout=CLOUD_PROBE_TIMEOUT_S)
        ms = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            body = r.text[:300]
            return {"ok": False, "ms": ms, "status": r.status_code,
                    "error": body + _hint(r.status_code, body), "endpoint": cm.endpoint()}
        sample = ""
        try:
            sample = str(r.json()["choices"][0]["message"]["content"] or "")
        except Exception:
            pass
        return {"ok": True, "ms": ms, "sample": sample.strip()[:200],
                "provider": cm.provider, "model": cm.model_id,
                "endpoint": cm.endpoint()}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}", "endpoint": cm.endpoint()}