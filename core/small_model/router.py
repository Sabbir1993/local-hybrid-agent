import re
import sys
from typing import Optional

from .config import APP_CONFIG

_needle_agent = None
_needle_tools_names = None
_needle_failed = False

_laya_router = None
_laya_failed = False


def reset_router_failures() -> None:
    """Re-arm both CPU routers after an admin edits the router config."""
    global _needle_failed, _laya_failed
    _needle_failed = False
    _laya_failed = False


def router_engine_name() -> str:
    """Active router engine configured in app.json: 'cactus_needle' or 'laya'."""
    eng = str(APP_CONFIG.get("router", {}).get("engine", "cactus_needle")).strip().lower()
    if "laya" in eng:
        return "laya"
    return "cactus_needle"


def needle_available() -> bool:
    global _needle_failed
    if _needle_failed or not APP_CONFIG.get("router", {}).get("enabled", True):
        return False
    if _needle_agent is not None:
        return True
    try:
        import needle  # noqa: F401
    except ImportError:
        _needle_failed = True
        return False
    return True


def needle_route(query: str, tools: list) -> Optional[dict]:
    if not needle_available():
        return None
    global _needle_agent, _needle_tools_names
    try:
        plain = [t["function"] for t in tools if isinstance(t, dict) and "function" in t]
        tool_names = tuple(sorted(t.get("name", "") for t in plain))
        if _needle_agent is None or _needle_tools_names != tool_names:
            import needle as _nd
            _needle_agent = _nd.Needle(tools=plain)
            _needle_tools_names = tool_names
        resp = _needle_agent.complete(query)
        if resp.get("type") != "call" or not resp.get("function_calls"):
            return None
        conf = float(resp.get("confidence") or 0.0)
        threshold = float(APP_CONFIG.get("router", {}).get("confidence_threshold", 0.7))
        if conf < threshold:
            return None
        fc = resp["function_calls"][0]
        return {"name": fc["name"], "args": fc.get("arguments") or {},
                "confidence": conf, "reasoning": resp.get("reasoning") or ""}
    except Exception as e:
        print(f"[server_manager] needle route failed: {e}", file=sys.stderr)
        _needle_failed = True
        return None


def laya_available() -> bool:
    global _laya_failed
    if _laya_failed or not APP_CONFIG.get("router", {}).get("enabled", True):
        return False
    if _laya_router is not None:
        return True
    try:
        import laya  # noqa: F401
    except ImportError:
        _laya_failed = True
        return False
    return True


def _extract_simple_args(tool_name: str, query: str) -> dict:
    """Extract common parameters from query deterministically for non-generative routers
    like Laya. An empty dict means "nothing usable found": the caller declines the route
    and the main loop handles the turn, which is always safe."""
    q_clean = query.strip()
    if tool_name == "list_files":
        m_pat = _GLOB_RX.search(q_clean)
        if m_pat:
            return {"pattern": m_pat.group(1)}
        return {}
    elif tool_name == "read_file":
        m_path = _QUOTED_ARG_RX.search(q_clean)
        if m_path:
            return {"path": m_path.group(1)}
        m_file = _WORKSPACE_PATH_RX.search(q_clean)
        if m_file:
            return {"path": m_file.group(1)}
        return {}
    elif tool_name == "grep":
        m_q = _QUOTED_ARG_RX.search(q_clean)
        if m_q:
            return {"pattern": _safe_grep_pattern(m_q.group(1))}
        m_word = _GREP_TRIGGER_RX.search(q_clean)
        if m_word:
            return {"pattern": _safe_grep_pattern(m_word.group(1))}
        return {"pattern": _safe_grep_pattern(q_clean)}
    return {}


def laya_route(query: str, tools: list) -> Optional[dict]:
    """Route query using Laya System 1 decision engine strictly on CPU."""
    if not laya_available():
        return None
    global _laya_router, _laya_failed
    try:
        laya_cfg = APP_CONFIG.get("router", {}).get("laya", {})
        target_device = "cpu"
        
        if _laya_router is None:
            import laya
            ckpt = laya_cfg.get("checkpoint", "convaiinnovations/laya")
            subfolder = laya_cfg.get("subfolder")
            preload = bool(laya_cfg.get("preload", False))
            try:
                if hasattr(laya, "Router"):
                    _laya_router = laya.Router(preload=preload, device=target_device)
                else:
                    _laya_router = laya.load(ckpt, subfolder=subfolder, device=target_device)
            except Exception:
                _laya_router = laya.load(ckpt, subfolder=subfolder, device=target_device)

        plain = [t["function"] for t in tools if isinstance(t, dict) and "function" in t]
        if not plain:
            return None

        criteria = {}
        for t in plain:
            name = t.get("name")
            desc = t.get("description", name)
            if name:
                criteria[name] = desc[:150]
        criteria["none"] = "None of the above tools apply or the user wants general conversational assistance"

        questions = {
            "selected_tool": {
                "type": "choice",
                "instructions": "Which tool should be invoked to satisfy the user's immediate request?",
                "criteria": criteria
            }
        }
        state_input = {"query": query, "body": query}

        if hasattr(_laya_router, "predict"):
            pred = _laya_router.predict(state_input, questions)
        else:
            pred = _laya_router(state_input, questions)

        ans = pred.get("answers", {}).get("selected_tool", {})
        chosen = ans.get("choice") or ans.get("answer")
        # the chosen option's probability: `confidence` in Laya's reply is a separate, far lower score
        conf = float(ans.get("answer_confidence") or ans.get("probability") or ans.get("confidence") or 0.0)

        threshold = float(APP_CONFIG.get("router", {}).get("confidence_threshold", 0.75))
        if not chosen or chosen == "none" or chosen not in criteria or conf < threshold:
            return None

        args = _extract_simple_args(chosen, query)
        reasoning = f"Laya CPU classified request to {chosen} (confidence: {conf:.2f})"
        return {
            "name": chosen,
            "args": args,
            "confidence": conf,
            "reasoning": reasoning
        }
    except Exception as e:
        print(f"[server_manager] laya route failed: {e}", file=sys.stderr)
        _laya_failed = True
        return None


def router_available() -> bool:
    """Check if the currently active router engine is available."""
    eng = router_engine_name()
    if eng == "laya":
        return laya_available() or needle_available()
    return needle_available() or laya_available()


def router_route(query: str, tools: list) -> Optional[dict]:
    """Unified route dispatcher according to app.json router.engine with fallback."""
    eng = router_engine_name()
    if eng == "laya":
        res = laya_route(query, tools)
        if res is not None:
            return res
        if _laya_failed and needle_available():
            return needle_route(query, tools)
        return None
    res = needle_route(query, tools)
    if res is not None:
        return res
    if needle_available() and laya_available():
        return laya_route(query, tools)
    return None


# Extraction patterns for the argument-free routers. The read_file body class
# deliberately excludes "." and ":" so a match can never carry a drive letter or a
# "../" sequence: adding them would hand core/agent_tools._ws_resolve paths it
# rejects, and the router lane may only short-cut calls the main loop would have
# resolved the same way. "./x" and "../x" therefore keep their historic behaviour
# (the dots are skipped by the class, the tool layer strips the leading separator).
_QUOTED_ARG_RX = re.compile(r"""['"`]([^'"`]+)['"`]""")

_GLOB_RX = re.compile(r"(\*\.[\w]+|\*\*[\w/.*]+|\*\w+\*?)")

# the extension must be letter-led and may chain once more: app.tar.gz matches,
# a bare version number like "2.5" does not
_WORKSPACE_PATH_RX = re.compile(
    r"([A-Za-z0-9_\-\\/]+\.[A-Za-z][A-Za-z0-9]{0,7}(?:\.[A-Za-z][A-Za-z0-9]{0,7})*)")

_GREP_TRIGGER_RX = re.compile(r"(?:grep(?:\s+for)?|search\s+for|find)\s+([^\s]+)", re.IGNORECASE)

def _safe_grep_pattern(pat: str) -> str:
    """A pattern core.agent_tools.tool_grep can actually compile. A valid regex --
    including one the user typed on purpose, e.g. "\\bdef\\b" -- passes through
    untouched; only an invalid pattern is escaped, so the router degrades to a literal
    search instead of losing the whole step to a re.error."""
    try:
        re.compile(pat)
        return pat
    except re.error:
        return re.escape(pat)
