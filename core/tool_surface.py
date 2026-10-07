"""Which tool schemas a lane is offered, and why.

Measured on this install: the full registry is 48 tools / ~8,527 tokens of JSON
schema - about a quarter of a 32,768 window spent before the conversation even
starts, and re-sent on every step. Most of that weight is situational: browser
automation (11 tools), device automation (11), office-document editing (3) and
image generation (1). A coding task never needs them, yet paid for them every
step.

Policy (`tool_surface.main_lane_on_demand` in config/app.json):

  * the main lane gets a core set (files, code, shell, skills, plan, delegation,
    memory, web search, image *reading*) plus only the situational families the
    request that opened the run actually names;
  * the executor lane, sub-agents and roles.json keep exactly what they had - they
    are already narrow, and narrowing them further is not this change's job;
  * a custom agent's allowlist still wins, because it is applied after this.

Escape hatch: a sub-agent spawned without a role is given the whole registry minus
the denied tools (core/subagent.py), so the orchestrator can still reach the browser
or device tools by delegating ("use spawn_agent for that") even when they are not in
its own schema list. Image *generation* is in that denied set, so it must be named
in the request to be enabled.

Turning the flag off restores the previous behaviour exactly: every tool, every step.
"""

import re
from typing import Iterable, Optional

# Situational families: a name prefix (or explicit names) plus the request wording
# that turns them on. Keywords are matched case-insensitively; multi-word keywords
# match as a substring, single words match on word boundaries so "app" does not fire
# on "apple".
FAMILIES: dict = {
    "browser": {
        "prefix": "browser_",
        "keywords": ["browser", "web page", "webpage", "website", "navigate", "click",
                     "screenshot", "scrape", "form", "login", "log in", "log into",
                     "sign in", "playwright", "open the site", "cookie", "javascript console"],
    },
    "mobile": {
        "prefix": "mobile_",
        "keywords": ["mobile", "android", "iphone", "ios app", "emulator", "adb",
                     "phone screen", "app screen", "swipe", "simulator"],
    },
    "docs": {
        "prefix": "doc_",
        "keywords": ["docx", "word document", "word file", "excel", "xlsx", "spreadsheet",
                     "powerpoint", "pptx", "pdf", "office document", "workbook", "slide deck"],
    },
    "image_gen": {
        "names": ("generate_image",),
        "keywords": ["generate image", "generate a picture", "create an image", "draw",
                     "illustrate", "illustration", "picture of", "image of", "logo",
                     "make me a picture", "make an image", "wallpaper", "avatar"],
    },
}

DEFAULT_CFG = {"main_lane_on_demand": True}

WEB_NAMES = ("web_search", "web_fetch", "web_search_images")


def _cfg(cfg: Optional[dict]) -> dict:
    base = dict(DEFAULT_CFG)
    for k, v in (cfg or {}).items():
        if k == "keywords" and isinstance(v, dict):
            for fam, kws in v.items():
                if fam in base and isinstance(kws, list):
                    base[fam] = {**base[fam], "keywords": [str(x) for x in kws]}
        else:
            base[k] = v
    return base


def _family_of(name: str) -> Optional[str]:
    for fam, spec in FAMILIES.items():
        if name.startswith(spec.get("prefix") or "\0") or name in (spec.get("names") or ()):
            return fam
    return None


def _matches(query: str, keyword: str) -> bool:
    q = query.lower()
    k = keyword.lower().strip()
    if not k:
        return False
    if " " in k:
        return k in q
    return re.search(rf"\b{re.escape(k)}", q) is not None


def surface_query(msgs: Iterable[dict], recent_user_turns: int = 3) -> str:
    """The wording the tool surface is decided on: the latest user turns plus the names of tools
    already called in this conversation.

    Deciding on the last message alone hid `browser_*` on a follow-up such as "yes, go ahead"
    or "fix it", in the middle of a browser task. A family the conversation has already used
    stays available.
    """
    parts, users, used = [], 0, []
    for m in reversed(list(msgs or [])):
        role = m.get("role")
        if role == "user" and users < recent_user_turns:
            c = m.get("content")
            if not isinstance(c, str):
                c = " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict)) if isinstance(c, list) else ""
            if not c.lstrip().startswith("["):      # skip [continue]/[stuck] style control turns
                parts.append(c)
            users += 1
        elif role == "assistant":
            for tc in m.get("tool_calls") or []:
                n = (tc.get("function") or {}).get("name") or ""
                if n and (_family_of(n) or n in WEB_NAMES):
                    used.append(n)
    return "\n".join(parts + sorted(set(used)))


def _web_wanted(query: str, cfg: dict) -> bool:
    if not cfg.get("web_on_demand", False) or not cfg.get("main_lane_on_demand", True):
        return True                                   # flag off: web is a core tool, as before
    if cfg.get("web_extra"):
        return True
    q = str(query or "")
    if re.search(r"https?://|\bweb_(?:search|fetch)", q):
        return True
    try:
        from . import prompt_scope
        return bool(prompt_scope.web_intent(q, [], []))
    except Exception:
        return False


def needed_families(query: str, cfg: Optional[dict] = None) -> set:
    """Situational families named by this request. Never raises: an empty set just
    means the core set is used."""
    try:
        cfg = _cfg(cfg)
        if not cfg.get("main_lane_on_demand", True):
            return set(FAMILIES)          # flag off == old behaviour
        q = str(query or "")
        return {fam for fam, spec in FAMILIES.items()
                if any(_matches(q, kw) for kw in spec["keywords"])}
    except Exception:
        return set()


def _name(tool: dict) -> str:
    return (tool.get("function") or {}).get("name") or ""


def filter_tools(tools: Iterable[dict], query: str = "", cfg: Optional[dict] = None) -> list:
    """Core tools plus the families this request names.

    MCP tools pass through here untouched: the agent/chat lanes re-hide
    unmentioned servers per step (core/prompt_scope.hide_unmentioned_mcp),
    which this family filter knows nothing about.
    """
    wanted = needed_families(query, cfg)
    web_ok = _web_wanted(query, _cfg(cfg))
    out = []
    for t in tools or []:
        name = _name(t)
        if not name or name.startswith("mcp__"):
            out.append(t)
            continue
        if name in WEB_NAMES:
            if web_ok:
                out.append(t)
            continue
        fam = _family_of(name)
        if fam is None or fam in wanted:
            out.append(t)
    return out


def hidden_families(query: str = "", cfg: Optional[dict] = None) -> list:
    """Families that were withheld - for the event: ctx payload and /control/status,
    so a missing tool is explainable instead of mysterious."""
    wanted = needed_families(query, cfg)
    hidden = sorted(f for f in FAMILIES if f not in wanted)
    if not _web_wanted(query, _cfg(cfg)):
        hidden.append("web")
    return hidden
