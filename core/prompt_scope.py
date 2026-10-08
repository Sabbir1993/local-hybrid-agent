"""core/prompt_scope.py - the prompt-efficiency standard (tiers + budgets).

Every prompt-building endpoint must follow this:

  S1  Tiered capabilities. System blocks and tools are `always` (identity,
      date, safety), `on-intent` (file/web/doc/media manuals + schemas),
      `on-mention` (MCP servers, KB context) or `on-first-use` (long how-tos
      move into the first tool result, never the prompt).
  S2  Stable prefix first (date, always-manuals, gated manuals, dynamic last)
      so prefix caching reuses the most tokens across turns.
  S3  Per-endpoint budgets, enforced in tests (CHAT_LEAN_BUDGET, ...).
  S4  Tool-schema diet (short descriptions, no examples in schemas).
  S5  Measure first: maybe_log() + estimate, proved in usage.db.
  S6  History hygiene (compaction/clearing) is unchanged.

Guards, KB permission scoping and cloud-egress rules are `always` and are
never gated by this module.
"""

import os
import re
import sys

# Per-endpoint budgets for a trivial turn (repo estimator tokens).
CHAT_LEAN_BUDGET = 1500
# Agent system prompt alone (was ~3.3k before the S4 compression).
AGENT_SYSTEM_BUDGET = 2700

WEB_TOOL_NAMES = ("web_search", "web_fetch", "web_search_images")


# Full file-creation manual: verbatim, on file intent only.
FILE_MANUAL = (
    "FILE CREATION & DOWNLOAD SYSTEM (COMMON STORAGE):\n"
    "You have the capability to create, write, generate, or fill files using the `write_file(path, content)` tool.\n"
    "All files you write are automatically saved to the shared common storage space and made available as direct download links.\n\n"
    "RULES FOR FILES:\n"
    "1. When the user asks to create, fill, write, generate, or share a file (e.g. 'fill data on that excel file and share with me', 'create data.csv', 'make a script', 'create resume.pdf', 'make a presentation', 'create an HTML dashboard'):\n"
    "   - NEVER refuse by saying 'I don't have the ability to directly edit or open local files on your computer'.\n"
    "   - Call `write_file(path=..., content=...)` immediately with the complete content, code, or data rows.\n"
    "   - If not calling `write_file`, you MUST provide the complete, rich, runnable code inside a markdown code block (e.g. ```html ... ```). NEVER output `[DOWNLOAD: filename]` alone without either calling `write_file` or outputting the complete code block.\n"
    "2. For Excel spreadsheets (.xlsx) or CSV files, provide the tabular data rows in `content` with a descriptive filename. It will automatically be created as a real, valid spreadsheet workbook.\n"
    "3. For PDF files (.pdf) and presentations (.pptx), provide clean, well-structured document or presentation content using `write_file(path='filename.pdf', content=...)`. It will automatically be compiled into a publication-ready PDF or presentation. CRITICAL: NEVER mention, display, or acknowledge to the user that any intermediate HTML generation or conversion is happening under the hood. Present it strictly as direct PDF or presentation creation.\n"
    "4. All files generated in common storage automatically append a unique revision ID to ensure media and documents are never duplicated or overwritten.\n"
    "5. When you generate or write a file, you MUST include a download link in your final response using this exact syntax:\n"
    "   [DOWNLOAD: filename]\n"
    "   The user interface will automatically convert `[DOWNLOAD: filename]` into a clickable download and preview button.\n"
    "6. ANTI-HALLUCINATION POLICY FOR COMPANY & INTERNAL DATA:\n"
    "   When the user asks questions about the company, employee records, internal policies, or company knowledge base, "
    "NEVER invent fake employee names, placeholder records, or fictional datasets. NEVER call `write_file` to create "
    "sample or dummy CSV/Excel spreadsheets unless the user explicitly requested a file export (e.g. 'export this to CSV' or 'save as Excel file'). "
    "Answer with the authentic internal company knowledge base data provided in the prompt.\n"
    "7. CHANGING AN EXISTING DOCUMENT (.pptx, .xlsx, .docx, .csv, .pdf - one you created or one the user attached): "
    "never regenerate it with write_file. Call `doc_inspect(file)` for its outline, then `doc_edit(file, ops)` "
    "targeting only what the user asked to change; everything else stays exactly as it was, and a new "
    "version is saved with its own [DOWNLOAD: ...] link.\n"
    "8. ONE DELIVERABLE PER REQUEST: write exactly one file in the format the user asked for (a PDF request "
    "gets one .pdf, a slide request gets one .pptx). Never save drafts or numbered versions (v2, _final, "
    "_proper), never write helper/generator scripts (.py) to build a document, and link only that one file."
)

# Lean pointer shipped on every turn so the model still knows files exist.
FILE_MANUAL_LEAN = (
    "FILES (only when asked): you can create and share files with the "
    "`write_file(path, content)` tool and link them as [DOWNLOAD: filename]. "
    "Never claim you cannot create files."
)

# Full web manual: verbatim, on web intent only.
WEB_MANUAL = (
    "You are a helpful, knowledgeable, and accurate AI assistant equipped with LIVE REAL-TIME INTERNET BROWSING & SEARCH.\n"
    "You have access to web tools:\n"
    "- `web_search(query)`: Search the live web to retrieve up-to-date facts, current news, documentation, releases, or any information you are uncertain about or do not know.\n"
    "- `web_search_images(query)`: Search the live web specifically for photos, pictures, portraits, logos, diagrams, and images. Returns image URLs and thumbnails.\n"
    "- `web_fetch(url)`: Fetch and read the full readable text content of any website or page URL.\n\n"
    "CRITICAL OPERATING RULES:\n"
    "1. YOU HAVE ACTIVE REAL-TIME INTERNET ACCESS. NEVER say 'I cannot browse the live internet' or 'I don't have internet access'.\n"
    "2. When the user provides a URL or asks to inspect, read, browse, or summarize a website (e.g. 'summarise https://...'), call `web_fetch(url=...)` immediately.\n"
    "3. When the user asks about recent events, real-time facts, current versions, weather, or anything outside your certain knowledge, call `web_search(query=...)` immediately.\n"
    "4. When the user asks for a photo, picture, image, or portrait (e.g. 'Can you give his photo?', 'show a picture of...'), call `web_search_images(query=...)` immediately, and in your final answer include the image links using markdown image syntax `![Description](URL)`.\n"
    "5. If you are confident in your knowledge (e.g. general explanations, basic math, creative writing, common programming concepts), answer directly without calling tools.\n"
    "6. When answering based on web search or fetch results, synthesize a clear, helpful response and cite each fact inline with the source number and link from the results, e.g. [1](URL), or `![Title](URL)` for images. Prefer the page excerpts (lines starting with '>') over snippets, and say so when the sources disagree or don't answer the question.\n"
    "7. Never put personal data (names of customers, emails, phone or account numbers) into search queries.\n"
    "8. COMPANY / ORGANIZATION OVERVIEW ('summarize X', 'what does X do', X's products): search the plain "
    "name first (e.g. `web_search(query='<name> official website')`, no recency, no extra product names), "
    "identify the official domain from the results, then `web_fetch` its homepage and its About / Products / "
    "Services pages (up to 3 pages) and base the summary on them; use other sources only for news or "
    "third-party facts. Never add product, server or tool names from this system prompt to a search query "
    "unless the user wrote them."
)

# Lean web hint (~150 tokens instead of ~800): the knowledge base already covers (part of) the question, so
# the web is only for what it does not contain. Format with the call budget.
WEB_LEAN = (
    "WEB (gap-filling): {kb} Use `web_search` only for the part that is missing - public or general facts - "
    "at most {n} call(s), and stop as soon as you have enough. Tag claims [KB: title] or [Web: site]; never "
    "present web results as company records, and never put company data in a search query.")
WEB_LEAN_KB = {
    "used": "The company knowledge base above covers the company-specific part of this question.",
    "blocked": "The company knowledge base is withheld from this run, so internal facts are unavailable.",
    "none": "The company knowledge base has nothing on this.",
}
# only when both the KB block and the full web manual are in the prompt
PROVENANCE = ("SOURCES: tag claims [KB: title] when they come from the company knowledge base and [Web: site] "
              "when they come from the web. Never present web results as company records.")


def file_intent(query: str, file_followup=None, prior_files=None) -> bool:
    """True when the file manual + doc tools earn their tokens this turn."""
    if file_followup:
        return True
    try:
        from routes.chat.file_intent import wants_file_output
    except Exception:
        return False
    try:
        return bool(wants_file_output(query or ""))
    except Exception:
        return False


_WEB_RECENCY_RE = re.compile(
    r"\b(latest|newest|current|today|tonight|yesterday|this\s+(week|month|year)"
    r"|breaking|news|price[sd]?|stock[s]?|weather|forecast|release[sd]?"
    r"|versions?|updates?|scores?|election|launched?)\b", re.IGNORECASE)
_WEB_VERB_RE = re.compile(
    r"\b(search|google|brows(e|ing)|fetch|look\s*up)\b", re.IGNORECASE)


def _prior_tool_use(prior_msgs):
    """(web_used, mcp_servers) from earlier tool calls - keeps research mode on."""
    web = False
    mcps = set()
    for m in prior_msgs or []:
        for tc in (m.get("tool_calls") or []):
            fn = str(((tc.get("function") or {}).get("name")) or "")
            if fn in WEB_TOOL_NAMES:
                web = True
            elif fn.startswith("mcp__"):
                parts = fn.split("__")
                if len(parts) >= 3 and parts[1]:
                    mcps.add(parts[1])
    return web, mcps


def web_intent(query: str, url_matches=None, prior_msgs=None, explicit_only: bool = False) -> bool:
    """True when the web manual + web tools earn their tokens this turn.

    Deliberately lean: general questions are answered from knowledge (the old
    rule 5). A miss only costs browsing, never correctness of the answer;
    the user can still force it with the web_search request flag + a URL or
    recency wording. explicit_only (voice): only a URL or an explicit search verb counts.
    """
    if url_matches:
        return True
    q = query or ""
    if _WEB_VERB_RE.search(q):
        return True
    if explicit_only:        # spoken turn: "today" / "current" in small talk must not start a web search
        return False
    if _WEB_RECENCY_RE.search(q):
        return True
    web, _ = _prior_tool_use(prior_msgs)
    return web


def mcp_mentions(text: str, server_names, prior_msgs=None) -> set:
    """Which MCP servers earn their prompt + schemas this turn."""
    names = set(server_names or [])
    found = {n for n in names
             if re.search(r"(?<![\w])" + re.escape(n) + r"(?![\w])", text or "", re.IGNORECASE)}
    _, mcps = _prior_tool_use(prior_msgs)
    return (found | mcps) & names


def mcp_schemas_for(schemas, servers) -> list:
    """Only the schemas of the named MCP servers (`mcp__<server>__*`)."""
    want = {"mcp__%s__" % s for s in servers or []}
    out = []
    for s in schemas or []:
        name = str(((s.get("function") or {}).get("name")) or "")
        if any(name.startswith(p) for p in want):
            out.append(s)
    return out


def agent_mcp_servers(mentioned, custom_agent_tools) -> set:
    """MCP servers an agent step may offer: mentioned/used plus an explicit
    custom-agent allowlist (admin config always wins, S1)."""
    out = set(mentioned or [])
    for n in custom_agent_tools or []:
        if n.startswith("mcp__") and n.count("__") >= 2:
            out.add(n.split("__")[1])
    return out


def hide_unmentioned_mcp(tools, servers) -> list:
    """Drop well-formed `mcp__<server>__*` schemas outside `servers`.

    Malformed names fail open. A lane that loses a family it genuinely needs
    delegates via spawn_agent (full set), per the documented escape hatch.
    """
    keep = set(servers or [])
    out = []
    for t in tools or []:
        name = str(((t.get("function") or {}).get("name")) or "")
        if name.startswith("mcp__"):
            parts = name.split("__")
            if len(parts) >= 3 and parts[1] and parts[1] not in keep:
                continue
        out.append(t)
    return out


def breakdown(sections, tools=None) -> dict:
    """Per-section token sizes using the repo's own estimator.

    sections: [(label, text)]. Returns {total, parts: {label: tokens}}.
    """
    from .agent_loop import estimate_prompt_tokens
    parts = {}
    for label, text in sections or []:
        parts[label] = parts.get(label, 0) + estimate_prompt_tokens(
            [{"role": "system", "content": text}])
    parts["tools"] = estimate_prompt_tokens([], tools) if tools else 0
    return {"total": sum(parts.values()), "parts": parts}


def scope_logging_enabled() -> bool:
    """P0 switch: env PROMPT_BREAKDOWN=1 or app.json debug.prompt_breakdown."""
    if os.environ.get("PROMPT_BREAKDOWN") in ("1", "true", "yes"):
        return True
    try:
        from .small_model import APP_CONFIG
        return bool((APP_CONFIG.get("debug") or {}).get("prompt_breakdown"))
    except Exception:
        return False


def maybe_log(tag: str, sections, tools=None, extra: str = "") -> None:
    """One stderr line per turn when enabled - the before/after ruler (S5)."""
    if not scope_logging_enabled():
        return
    try:
        b = breakdown(sections, tools)
        detail = " ".join(f"{k}={v}" for k, v in b["parts"].items())
        print(f"[prompt] {tag}: ~{b['total']} tok {detail}"
              + (f" {extra}" if extra else ""), file=sys.stderr)
    except Exception:
        pass
