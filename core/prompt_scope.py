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
    "1. When the user asks to create, fill, write, generate, or share a file (e.g. 'fill data on that excel file and share with me', 'create data.csv', 'create resume.pdf', 'make a presentation', 'create an HTML dashboard'):\n"
    "   - NEVER refuse by saying 'I don't have the ability to directly edit or open local files on your computer'.\n"
    "   - Call `write_file(path=..., content=...)` immediately with the complete content, code, or data rows.\n"
    "   - If not calling `write_file`, you MUST provide the complete, rich, runnable code inside a markdown code block (e.g. ```html ... ```). NEVER output `[DOWNLOAD: filename]` alone without either calling `write_file` or outputting the complete code block.\n"
    "2. For Excel spreadsheets (.xlsx) or CSV files, provide the tabular data rows in `content` with a descriptive filename. It will automatically be created as a real, valid spreadsheet workbook. The FIRST row is always a header of short column names (never a data record); several sheets are written as `## Sheet: Name` blocks, each with its own table, never as section titles inside one table.\n"
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
    "_proper), never write helper/generator scripts (.py, .sh) to build a document - chat cannot run them and refuses to save scripts or "
    "programs at all (show code in a fenced code block instead) - and link only that one file."
)

# Deck format + style questions: appended to FILE_MANUAL only when the request is about slides.
PPTX_MANUAL = (
    "PRESENTATIONS (.pptx): `content` is ONLY a slide outline in this format, nothing before or after it:\n"
    "   # Deck title\n   subtitle: one line\n   theme: editorial|ember|midnight|corporate|forest|sunset|minimal|royal\n"
    "   art: arches|waves|skyline|geometric|network   (optional drawing on the cover: pick the one that fits the topic)\n"
    "   ---\n   ## Slide title\n   eyebrow: SHORT LABEL ABOVE THE TITLE   (1-3 words: AT A GLANCE, HISTORY, ECONOMY)\n"
    "   - a line that carries a fact (up to 22 words; 3-5 lines)\n   source: where the facts on this slide come from\n"
    "   notes: 2-3 sentences the presenter would say\n   ---\n   (next slide...)\n"
    "   SLIDE TYPES - pick the one that fits the content and vary them (never three of the same kind in a row):\n"
    "   `layout: stats` + bullets `- 36.6M | what it measures` (3-5 big numbers);  `layout: timeline` + `- 21 Feb 1952 | what "
    "happened` (3-5 dated steps);  `layout: cards` + `- Heading | one or two sentences` (3-6 cards);  `layout: textside` = 2-4 "
    "text bullets plus a markdown table (or `chart:` lines) shown beside them;  `layout: comparison` = exactly two `Heading:` "
    "bullets, each with 2-4 sub-bullets;  `layout: agenda` (3-6 short bullets), `section`, `quote`, `two-column`, `table` (a "
    "markdown table, max 5 columns), `full` (+ `image:`), `gallery` (2-3 `image:` lines). Plain bullets only when nothing else "
    "fits.\n"
    "   CHARTS: for trends, shares and comparisons write `chart: bar|hbar|line|pie|stacked | unit=%` under the title, then bullets "
    "`Label: number` (or a markdown table: first column = labels, other columns = numbers = several series). `stacked` is one "
    "bar split into shares.\n"
    "   CONTENT RULES: use 8-14 slides; the first is the title, the second-to-last 'Key takeaways' (or 'Sources'), the last "
    "`## Thank you` or another closing. Every content slide states concrete facts - figures, dates, names, examples - taken "
    "from your research, never filler like 'important', 'many', 'various', and has a `source:` line naming where they come "
    "from (say 'estimate' for a figure you could not verify). Never invent numbers or data. Never write `Slide N:` labels, "
    "headers like 'Markdown source', bold markup, or any text outside the outline.\n"
    "   PICTURES: a slide may carry one picture with a line `image: <3-6 search words> | source=search|generate` (source is "
    "optional: by default the web is searched first, then the local image model if it is loaded). Put it under the slide title "
    "on the cover and on plain bullet slides (text left, picture right); `layout: full` + `image:` makes a full-screen picture "
    "slide. Use pictures on about half the slides, max 8 per deck, for concrete subjects (a place, a product, a scene) - never "
    "for charts, tables, logos or text. Name the SPECIFIC subject and the place/topic of the deck, e.g. `image: Lalbagh Fort "
    "Dhaka Bangladesh`, never a generic word like 'history' and never a sentence. Web pictures are matched to the topic and "
    "credited on the slide automatically; a slide without a good match stays text-only. When the user asks for images / pictures "
    "/ visuals put `image:` lines on the slides - the system searches and inserts them. NEVER list image URLs in the deck, never "
    "add an 'Image sources' / 'Design notes' / 'Instructions' slide (every slide is content for the audience), never draw ASCII "
    "boxes or diagrams, and do not call web_search_images for a deck.\n"
    "   SAME LOOK AS BEFORE: when this chat already has a deck, reuse its `theme:` (and `accent:` / `font:`) line unless the user "
    "asks for a different look, so the decks of one session match.\n"
    "   Before writing a deck, if the user gave neither a slide count nor a style, ask ONE short question in plain sentences (no "
    "table, no code block) offering these looks: editorial (cream + deep teal, gold), ember (cream + maroon, gold), midnight (navy "
    "+ cyan), corporate (white + blue), forest (green + amber), sunset (charcoal + coral), minimal (white + red accent), royal "
    "(indigo + gold), and a slide count (default 10). Skip the question when they already answered, said 'you choose' / 'just make "
    "it', or sent a detailed brief: then pick the theme that fits the topic. To change only the look of an existing deck, call "
    "write_file again with the same slides and a new `theme:` line.\n"
)

_PPTX_RE = re.compile(r"\b(pptx?|power ?point|slides?|slide deck|deck|presentation)\b", re.I)


def pptx_intent(query: str, prior_files=None, msgs=None) -> bool:
    """Deck request now, or earlier in this chat (the user may just be answering the style question)."""
    recent = [str(m.get("content") or "")[-600:] for m in (msgs or [])[-4:] if isinstance(m, dict)]
    return any(_PPTX_RE.search(t) for t in [query or "", *recent]) or any(str(f).lower().endswith((".pptx", ".ppt")) for f in (prior_files or []))


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
    "4. When the user asks for a photo, picture, image, or portrait (e.g. 'Can you give his photo?', 'show a picture of...'), call `web_search_images(query=...)` immediately, and in your final answer include the image links using markdown image syntax `![Description](URL)` (use the Thumbnail URL for list-item pictures).\n"
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

# Advice-style answers (chat.style): recommendation first, sections, thumbnails, a typed questionnaire card when
# key facts are missing. ~250 tokens; added only when style_wanted() says the question is advice/research-like.
CHAT_STYLE = (
    "ANSWER STYLE (advice / how-to / recommendation questions):\n"
    "- Lead with a 1-2 sentence direct answer or recommendation, then 2-4 numbered sections with `##` headings. "
    "Be concise: at most 4 items per section, 1-2 sentences each.\n"
    "- List items that name a product, food, place or thing: `- ![name](THUMBNAIL_URL) **Title** - one or two sentences`, "
    "using the Thumbnail URL from web_search_images when you called it; no image if you have none (never invent URLs).\n"
    "- Medical, legal or safety topics: say what to check with a professional (e.g. a vet) and when it is urgent.\n"
    "- If key facts are missing (age, weight, budget, symptoms...), end with ONE fenced block and nothing after it:\n"
    "```questionnaire\n"
    '{"title":"Tell me a little about ...","questions":[{"id":"age","type":"radio","q":"How old is it?",'
    '"options":["Under 1 year","1-7 years","Over 7 years"]},{"id":"signs","type":"checkbox","q":"Any of these?",'
    '"options":["Vomiting","Poor coat"]},{"id":"weight","type":"text","q":"Current weight, if known"}],'
    '"submit_label":"Get my plan"}\n'
    "```\n"
    "  (valid JSON, 2-5 questions, types radio | checkbox | text). When the user replies with answers, use them to "
    "personalise the plan and do not ask the same questions again.")

# Visual explainers (chat.visuals): a "how does X work / architecture / flow" answer may carry one drawn block. The
# client renders the JSON itself (static/js/markdown.js parseVisual), so the model never writes HTML or styles.
VISUAL_STYLE = (
    "VISUAL BLOCKS: when explaining how something works (a flow, pipeline, architecture or sequence of 3-6 stages), "
    "put ONE fenced `visual` block right after your short intro, then continue in normal markdown. Format (valid "
    "JSON only):\n"
    "```visual\n"
    '{"type":"flow","title":"How it works","steps":[{"title":"Public internet","text":"User visits `https://app.example.com`",'
    '"icon":"globe","tone":"blue","note":"DNS resolves the domain to your public IP"},{"title":"Reverse proxy",'
    '"text":"Terminates TLS on port 443","icon":"shield","tone":"purple","note":"Forwards to a local port"},'
    '{"title":"Local app","text":"`127.0.0.1:3000`","icon":"server","tone":"green"}]}\n'
    "```\n"
    "Rules: at most 6 steps; `note` is the short caption on the arrow AFTER that step; `text` under 12 words. "
    "icon is one of globe, shield, server, database, lock, user, cloud, cpu, file, key, gear, check, alert, code, "
    'network, mobile, mail, money (or omit); tone is one of blue, purple, green, amber, red, gray (or omit). For '
    'a set of parallel items use {"type":"cards","items":[{"title","text","icon","tone"}]}.\n'
    "For numbers (trends, shares, comparisons) NEVER draw ASCII or text-block charts; use a chart block instead:\n"
    "```visual\n"
    '{"type":"chart","kind":"line","title":"Attacks per year","unit":"%","labels":["2024","2025","2026"],'
    '"series":[{"name":"AI-enabled","values":[40,60,89]}]}\n'
    "```\n"
    'kind is bar, line or hbar; up to 4 series; hbar also accepts {"items":[{"label":"Zero-day","value":42}]}. '
    "Chart only numbers you actually know (never invent data) and name the source in the text. Do not put card "
    "numbers, secrets or personal data in a block. Use a block only when it helps; skip it for simple answers.")

_VISUAL_RE = re.compile(
    r"\b(diagram|flow\s*chart|flowchart|architecture|work\s*flow|workflow|pipeline|life\s*cycle|lifecycle|sequence"
    r"|visuali[sz]\w*|step[- ]by[- ]step|overview|request\s+flow|charts?|graphs?|plot|trends?|statistics|stats)\b"
    r"|\bhow\b.{0,60}\b(works?|flows?|happens?|communicates?|connects?)\b",
    re.IGNORECASE)
_VISUAL_SKIP_RE = re.compile(r"```|\bmermaid\b|\b(pptx|docx|xlsx|pdf|slides?|deck)\b", re.IGNORECASE)


def visual_wanted(query: str, voice: bool = False, enabled: bool = True) -> bool:
    """True when the visual-block instructions earn their ~300 tokens: an explain-how-it-works / diagram question
    (not voice, not code or document building, not an explicit mermaid request)."""
    if not enabled or voice:
        return False
    q = str(query or "")
    return bool(_VISUAL_RE.search(q)) and not _VISUAL_SKIP_RE.search(q)


_STYLE_ADVICE_RE = re.compile(
    r"\b(recommend\w*|suggest\w*|advice|advise|should\s+i|can\s+i|how\s+(can|do|to|should)|best|which|compare"
    r"|versus|vs\.?|safe|healthy|health|diet|supplements?|vitamins?|medicine|treatment|remed\w+"
    r"|improve|boost|raise|reduce|lose|gain|plan|tips?|ideas?|better)\b", re.IGNORECASE)
_STYLE_SKIP_RE = re.compile(
    r"```|\b(python|javascript|typescript|java|c\+\+|sql|bash|regex|function|class|traceback|stack\s*trace|compile\w*"
    r"|exception|git|commit|docker|api|endpoint|json|html|css|file|folder|pptx|docx|xlsx|pdf|slides?)\b", re.IGNORECASE)


def style_wanted(query: str, prior_msgs=None, voice: bool = False, enabled: bool = True) -> bool:
    """True when the advice-style block earns its tokens: a real advice/how-to/recommendation question (not code,
    files, small talk or voice), or a conversation already in that style (an earlier questionnaire) so the style
    stays stable across the follow-ups and the user's answers get personalised."""
    if not enabled or voice:
        return False
    for m in prior_msgs or []:
        if m.get("role") == "assistant" and "```questionnaire" in str(m.get("content") or ""):
            # our deck / PDF cards have their own briefs (DECK_BRIEF / PDF_BRIEF): not a second, advice-style one
            return not re.search(r"your (?:deck|pdf)", str(m.get("content") or ""), re.IGNORECASE)
    q = (query or "").strip()
    if len(q) < 25 or _STYLE_SKIP_RE.search(q):
        return False
    return bool(_STYLE_ADVICE_RE.search(q))


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


_WEB_RESEARCH_RE = re.compile(
    r"\b(recommend\w*|suggest\w*|best|which|compare|versus|vs\.?|safe|supplements?|vitamins?|medicine|treatment"
    r"|remed\w+|reviews?|alternatives?|ideas?|tips?|how\s+(can|do|to|should)|should\s+i)\b", re.IGNORECASE)


def web_intent(query: str, url_matches=None, prior_msgs=None, explicit_only: bool = False,
               research: bool = False) -> bool:
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
    if research and len(q) >= 25 and _WEB_RESEARCH_RE.search(q) and not _STYLE_SKIP_RE.search(q):
        return True      # chat.research_web: advice-style questions look things up without the word "search"
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


# PowerPoint requests: a deck built from a one-line ask is generic and the wrong length, so the model asks first
# (typed questionnaire card, rendered by markdown.js), then builds it with write_file as a .pptx.
DECK_BRIEF = (
    "POWERPOINT REQUEST:\n"
    "- Unless the user already gave the slide count AND the colour palette (or has just answered your questions), do NOT "
    "write the file yet. Reply with one short line and ONE fenced block, nothing after it:\n"
    "```questionnaire\n"
    '{"title":"A few quick choices for your deck","questions":['
    '{"id":"slides","type":"radio","q":"How many slides?","options":["5-7 (short)","8-12 (standard)","13-20 (detailed)"]},'
    '{"id":"audience","type":"radio","q":"Who is it for?","options":["Executives","Customers","Technical team","General / non-technical"]},'
    '{"id":"palette","type":"radio","q":"Colour palette?","options":["Editorial (cream + deep teal, gold)","Ember (cream + maroon, gold)",'
    '"Midnight (navy + cyan, dark)","Corporate (white + blue)","Forest (green + amber)","Sunset (charcoal + coral)",'
    '"Minimal (white, black, red)","Royal (indigo + gold)"]},'
    '{"id":"style","type":"radio","q":"Slide style?","options":["Editable (real PowerPoint shapes you can change)",'
    '"Designer (image slides, richest look, not editable)"]},'
    '{"id":"extra","type":"text","q":"Anything it must include? (optional)"}],"submit_label":"Build my deck"}\n'
    "```\n"
    "- After the answers (or when the user already specified them), BUILD THE DECK IN PIECES so the user watches it grow:\n"
    "  1. First work out the slide plan: exactly N slides (the number they chose, mid-range if a range), one "
    "title + one-line purpose each, last slide a closing/thank-you slide. Pass it as the `plan` argument of doc_create, one "
    "line per slide ('1. Title - purpose'); the tool plays it back after every doc_append.\n"
    "  2. Call doc_create(file='<topic>.pptx', total=N, content=...) with ONLY: first line `theme: <editorial|ember|midnight|"
    "corporate|forest|sunset|minimal|royal>` (the palette they chose), `mode: designer` right after it when they chose the "
    "Designer style, `# Deck title`, a subtitle line, an `art:` line for the cover, then slides 1-2 (each `## Slide title` + an "
    "`eyebrow:` label + 3-5 lines of up to 22 words + a `source:` line + a `notes:` line, slides separated by a line "
    "containing only `---`). Each line carries a concrete fact, number or name from your research; use stats, timeline, cards, "
    "a table beside text or a chart where the content fits instead of repeating bullet slides. Write for the audience they "
    "chose: executives = outcomes, numbers, decisions; customers = benefits in plain words; technical team = specifics and "
    "trade-offs; general = no jargon.\n"
    "  3. If the user wants pictures/visuals/images in the deck, add an `image: <3-6 search words>` line under the title of the "
    "cover and of roughly every other content slide (see the picture rules in the presentation format); read the tool result "
    "for slides that got no picture and do not retry them.\n"
    "  4. Then call doc_append(file=<the exact name doc_create returned>, content=<next 2-3 slides>) again and again, following "
    "your plan, until N slides exist; put done=true on the last call. Plain text only: no emoji, no `>` quotes, "
    "no ASCII diagrams (a markdown table is fine for a comparison). The second-to-last slide is 'Key takeaways' (or 'Sources' "
    "naming where facts came from), the last is the closing. Never write a .md file for a PowerPoint request and never send the whole deck in one call.")
_DECK_ASK_RE = re.compile(
    r"\b(ppt|pptx|power\s*point|presentation|slide\s*deck|slides?|deck)\b", re.IGNORECASE)
_DECK_VERB_RE = re.compile(
    r"\b(make|create|generate|build|prepare|design|draft|write|produce|need|want|give|convert)\b", re.IGNORECASE)


def deck_brief_wanted(query: str, prior_msgs=None) -> bool:
    """True for a request to make a PowerPoint (or the user's answers to our deck questionnaire), so the model asks
    for slide count / audience / palette before building. Edits of an existing deck are left to doc_inspect/doc_edit."""
    for m in reversed(prior_msgs or []):
        if m.get("role") == "assistant":
            c = str(m.get("content") or "")
            if "```questionnaire" in c and "deck" in c.lower() and "slides" in c.lower():
                return True
            break                       # the last reply was something else: judge the new request on its own words
    q = (query or "").strip()
    if not q or not _DECK_ASK_RE.search(q) or not _DECK_VERB_RE.search(q):
        return False
    return not re.search(r"\b(edit|change|fix|update|modify|rename|translate|summari[sz]e|explain)\b", q, re.IGNORECASE)


# PDF documents (guide / report / handbook): same ask-first, build-in-pieces flow as the deck brief. The PDF is
# printed once at the end; until then the right-hand pane shows a live HTML draft of the sections written so far.
PDF_BRIEF = (
    "PDF DOCUMENT REQUEST:\n"
    "- Unless the user already gave the length AND audience (or has just answered your questions), do NOT write the file "
    "yet. Reply with one short line and ONE fenced block, nothing after it:\n"
    "```questionnaire\n"
    '{"title":"A few quick choices for your PDF","questions":['
    '{"id":"sections","type":"radio","q":"How long should the PDF be?","options":["Short (4-5 sections)","Standard (6-8 sections)","Detailed (9-12 sections)"]},'
    '{"id":"audience","type":"radio","q":"Who is it for?","options":["Executives","Customers","Technical team","General / non-technical"]},'
    '{"id":"tone","type":"radio","q":"Style?","options":["Formal report","Friendly guide","Step-by-step handbook"]},'
    '{"id":"extra","type":"text","q":"Anything it must include? (optional)"}],"submit_label":"Build my PDF"}\n'
    "```\n"
    "- After the answers (or when the user already specified them), BUILD THE PDF IN PIECES so the user watches it grow:\n"
    "  1. First work out the section plan in your head: exactly N sections (the number their length choice implies), one "
    "heading + one-line purpose each; the last section is a short summary / next steps.\n"
    "  2. Call doc_create(file='<topic>.pdf', total=N, content=...) with ONLY the title (`# Title`), a one-line intro, and sections "
    "1-2 (each `## Heading` + short paragraphs or bullets).\n"
    "     Depth: open with a `## Summary` of 3-4 bullets; every section has 2-4 short paragraphs or a bullet list AND, where it "
    "helps, one table (comparison, options, timeline) or a worked example; every claim names its source or says it is an "
    "estimate; headings of at most 8 words; the last section is 'Next steps'.\n"
    "  3. Then call doc_append(file=<the exact name doc_create returned>, content=<next 1-2 sections>) again and again, following "
    "your plan, until N sections exist; put done=true on the last call (that prints the PDF). Plain markdown only: headings, "
    "paragraphs, bullets, simple tables. No emoji and no ASCII-art diagrams. Never write the whole PDF in one call.")

_PDF_ASK_RE = re.compile(r"\b(pdf|report|guide|handbook|whitepaper|white paper|manual|ebook|e-book)\b", re.IGNORECASE)


def pdf_brief_wanted(query: str, prior_msgs=None) -> bool:
    """True for a request to produce a PDF document (or the user's answers to our PDF questionnaire). Decks go to the
    deck brief, edits of an existing file to doc_inspect/doc_edit, and a plain question about PDFs is not a request."""
    for m in reversed(prior_msgs or []):
        if m.get("role") == "assistant":
            c = str(m.get("content") or "")
            if "```questionnaire" in c and "your pdf" in c.lower() and "sections" in c.lower():
                return True
            break
    q = (query or "").strip()
    if not q or not re.search(r"\bpdf\b", q, re.IGNORECASE) or deck_brief_wanted(q, []):
        return False
    if not _DECK_VERB_RE.search(q):
        return False
    return not re.search(r"\b(edit|change|fix|update|modify|rename|translate|summari[sz]e|explain|read|open|convert)\b", q, re.IGNORECASE)


def brief_phase(query: str, prior_msgs=None) -> str:
    """For a deck / PDF request: "ask" (show the questionnaire card now) or "build" (the user just answered it), else "".
    The two need opposite tool sets: asking needs no research (it was thrown away and burned the turn / web budget),
    building must not ask again."""
    if not (deck_brief_wanted(query, prior_msgs) or pdf_brief_wanted(query, prior_msgs)):
        return ""
    for m in reversed(prior_msgs or []):
        if m.get("role") == "assistant":
            return "build" if "```questionnaire" in str(m.get("content") or "") else "ask"
    return "ask"


def brief_request(prior_msgs=None) -> str:
    """The user's ORIGINAL deck / PDF request: the user message just before our questionnaire card. Once they have answered
    the card, their latest message is only "How many slides?: 8-12 ... Colour palette?: Corporate ..." - routing the company
    Knowledge Base by THAT text pulls in unrelated documents (it matched "Corporate" to annual reports)."""
    msgs = list(prior_msgs or [])
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") == "assistant" and "```questionnaire" in str(m.get("content") or ""):
            for j in range(i - 1, -1, -1):
                if msgs[j].get("role") == "user":
                    return str(msgs[j].get("content") or "")
            break
    return ""


# Appended after the deck / PDF brief once the user has answered the card: stops the model asking again (it did, after
# a few slides, once research results had pushed the answers far up the conversation).
BRIEF_BUILD_NOW = (
    "THE USER HAS ALREADY ANSWERED YOUR QUESTIONNAIRE: slide/section count, audience, palette and extras are in their last "
    "message. Do NOT ask any question and do NOT show the card again - not at the start, not after a few slides, not at the "
    "end. Work in this order:\n"
    "1. RESEARCH (when web tools are offered): search results may already be above - use them first. Then add follow-up "
    "web_search / web_fetch calls for what is still missing - figures, dates, names, recent updates - and READ at least two "
    "pages in full with web_fetch, not just the snippets. Every search and every page read counts against one budget: when a "
    "result says it is used up, STOP researching and write. Never fetch image-search or stock-photo pages (pixabay, "
    "unsplash...): pictures are the system's job. Keep track of where each fact came from.\n"
    "2. PLAN: the slide plan goes in the `plan` argument of doc_create.\n"
    "3. BUILD: doc_create, then doc_append until the planned count exists, done=true on the last call. Every content slide "
    "carries figures, dates and names from your research, a `source:` line, and the slide type that fits it (stats for numbers, "
    "timeline for dates, cards for a list of things, textside for a table beside text, chart for trends or shares). A figure you "
    "could not verify is labelled an estimate in its source line; never invent numbers.\n"
    "4. After the first doc_create do not search again. For pictures write `image:` lines only: the system fetches them; never "
    "call web_search_images."
)

BRIEF_ASK_NOW = (
    "Do not research or call any tool yet: reply with the one short line and the questionnaire card only."
)


# A spreadsheet (xlsx / xls / csv) attached to the conversation: the message carries its PROFILE, data_query gives exact figures.
DATA_ATTACHMENT_RX = re.compile(r"--- FILE:[^\n]*?\.(?:xlsx?|csv)\s*---", re.IGNORECASE)
ATTACHED_DATA = (
    "ATTACHED DATA SET:\n"
    "- The user attached a spreadsheet. Their message contains its PROFILE (sheets, columns, types, ranges, common values, sample "
    "rows), not every row. Work from that first and say what the data is.\n"
    "- For any count, total, average, ranking, comparison or filter, call `data_query` (one filter / one group-by) or `data_sql` "
    "(joins across sheets, pivots, several group-bys, medians / percentiles, month or quarter buckets, window functions; call it "
    "with describe=true first) on the attached file (use the file name from the attachment note): they answer exactly over ALL "
    "rows. Never estimate figures from the sample rows. If the profile shows title rows or formula columns, say so.\n"
    "- Do NOT search the company knowledge base or the web for a question about the attached data. Use them only if the user asks "
    "for company information or outside facts.\n"
    "- Answer with a short summary of the key findings and figures (a small table when it helps); mention anything you could not "
    "verify.")


# Image tool gating: the chat offers generate_image only when the user is asking for a picture. Offered always, a
# local model that has a tool it does not need tends to call it (a "serene mountain landscape" nobody asked for).
_IMAGE_RE = re.compile(
    r"\b(image|images|picture|pictures|photo|photos|illustration|illustrate|draw|drawing|paint|painting|sketch|logo|icon|"
    r"poster|banner|wallpaper|thumbnail|avatar|portrait|artwork|render|generate an? (?:image|picture)|/image)\b|ছবি|চিত্র", re.I)


def image_intent(query: str, prior_msgs=None) -> bool:
    """True when this message (or the user message just before it, for "make it darker") asks for a picture."""
    if _IMAGE_RE.search(query or ""):
        return True
    for m in reversed(prior_msgs or []):
        if m.get("role") == "user":
            return bool(_IMAGE_RE.search(str(m.get("content") or "")[:400])) and len((query or "").strip()) < 80
    return False


# Spreadsheet / workbook BUILD requests: the model writes a workbook outline (core/sheet_engine/build.py) instead of a flat table.
_SHEET_RE = re.compile(r"\b(xlsx|excel|spreadsheets?|sp[a-z]{2,8}\s?sheets?|workbooks?|worksheets?|budget sheet|tracker)\b", re.I)
_SHEET_VERB_RE = re.compile(r"\b(creat\w*|mak(?:e|ing)|build\w*|generat\w*|prepar\w*|draft\w*|design\w*|set ?up|produce|give me|need|want|write|share|send|provide|export|deliver|get me|final)\b", re.I)
_SHEET_EDIT_RE = re.compile(r"\b(edit|change|fix|update|modify|rename|translate|summari[sz]e|explain|analy[sz]e|analysis|attached|uploaded)\b", re.I)


def sheet_build_wanted(query: str, prior_msgs=None) -> bool:
    """True for a request to make an Excel workbook / spreadsheet (not an edit of, or analysis of, an existing one)."""
    q = (query or "").strip()
    if not q or not _SHEET_RE.search(q) or not _SHEET_VERB_RE.search(q):
        return False
    return not _SHEET_EDIT_RE.search(q)


XLSX_MANUAL = (
    "EXCEL WORKBOOK REQUEST (build it with doc_create, file=<name>.xlsx):\n"
    "Write a WORKBOOK OUTLINE, not a flat table:\n"
    "```\n"
    "# Workbook title\n"
    "theme: corporate        (corporate|forest|sunset|minimal|royal|ember|editorial|ocean)\n"
    "## Sheet: Assumptions\n"
    "| Item | Value |\n|---|---|\n| Tax rate | 15% |\n"
    "name: TaxRate = Assumptions!B2\n"
    "## Sheet: Sales\n"
    "| Date | Region | Revenue | Tax |\n|---|---|---|---|\n| 2026-07-05 | Dhaka | 1000 | =C2*TaxRate |\n"
    "format: Revenue=currency:BDT, Tax=currency:BDT, Date=date\n"
    "total: Revenue=sum, Tax=sum\n"
    "highlight: Revenue<500 red\nscale: Revenue\nvalidate: Region in [Dhaka, Khulna]\ntable: SalesTbl\n"
    "chart: bar | title=Revenue by region | x=Region | y=Revenue\n"
    "```\n"
    "SIZE AND DEPTH: a workbook for a business request (analysis, plan, report, tracker, budget, TAP, market study) is NOT one "
    "sheet and never a single table with section titles inside it. Plan 4-6 sheets before writing, for example: Summary (key "
    "figures and a chart, built from formulas over the other sheets), Data / Details (the main table, one row per record), "
    "Analysis (comparisons, scoring, scenarios), Assumptions (inputs and rates), Timeline or Roadmap, Sources & Notes. Every "
    "sheet is its own `## Sheet: Name` with its own table and real content: specific figures, dates and names, no placeholders. "
    "Build it in STEPS so each call stays small: doc_create with the first 1-2 sheets, then one doc_edit per remaining sheet "
    "with {op:'add_outline_sheet', name:'Analysis', content:'| Col | Col |\\n|---|---|\\n| .. | .. |\\nformat: ..'}, then optional "
    "add_chart / set_format ops. Do not stop after one sheet.\n"
    "RULES (professional spreadsheet practice):\n"
    "- Calculations are FORMULAS (=SUM(C2:C9), =SUMIFS(...), =C2*TaxRate), never numbers you worked out yourself. Totals use "
    "`total:`. The system checks and calculates every formula and reports errors: fix them with doc_edit.\n"
    "- Inputs (rates, targets, thresholds) go on an Assumptions sheet and are referenced by cell or `name:`; do not type constants "
    "like 1.15 inside formulas. Put units in headers (Revenue (BDT)). One row per record, no merged cells inside data.\n"
    "- Allowed functions: SUM SUMIF(S) COUNTIF(S) AVERAGE(IF/IFS) MIN MAX MEDIAN IF IFS IFERROR ROUND INDEX MATCH VLOOKUP XLOOKUP "
    "SUMPRODUCT TEXT DATE EOMONTH YEAR MONTH PMT FV PV NPV IRR and similar. No INDIRECT/OFFSET/HYPERLINK/external links.\n"
    "- Summary / dashboard sheets are built from SUMIFS / COUNTIFS formulas over the data sheets (they stay live), with a chart.\n"
    "- Dates as 2026-07-05, percentages as 15%, money as plain numbers with `format:`. Real data only: never invent rows; if the "
    "user gave none, build the structure with the headers and a few clearly-labelled example rows.\n"
    "- Reply with one short summary and the [DOWNLOAD: file] link. Do not describe the formulas at length.")


# When the user NAMES the kind of file they want, that is the deliverable: a request for "the final spreadsheet" that comes back as a
# slide deck (because the same message says "present it to the board") is a wrong answer however good the deck is.
_FORMAT_FAMILIES = (
    (".xlsx", _SHEET_RE),
    (".pdf", re.compile(r"\bpdf\b", re.I)),
    (".pptx", _PPTX_RE),
    (".docx", re.compile(r"\b(docx|word (?:document|file)|ms word)\b", re.I)),
)
_DOC_EXTS = (".xlsx", ".xls", ".csv", ".pdf", ".pptx", ".ppt", ".docx", ".doc")
_FORMAT_LABEL = {".xlsx": "spreadsheet (.xlsx)", ".pdf": "PDF (.pdf)", ".pptx": "presentation (.pptx)", ".docx": "Word document (.docx)"}


def explicit_format(query: str) -> str:
    """The one file extension the message asks for by name ('.xlsx' ...), or '' when it names none or several."""
    hits = [ext for ext, rx in _FORMAT_FAMILIES if rx.search(query or "")]
    return hits[0] if len(hits) == 1 else ""


def format_note(ext: str) -> str:
    return (f"[system note] The user explicitly asked for a {_FORMAT_LABEL.get(ext, ext)}. That is the ONLY deliverable: create exactly "
            f"that file type, do not create any other format (no slides, no PDF, no Word, no markdown copy) even if the message also "
            f"mentions presenting or sharing it, and finish with its [DOWNLOAD: file] link.")


def format_mismatch(ext: str, filename: str) -> str:
    """Refusal text when a tool call would create a document of another kind than the user asked for, else ''."""
    from pathlib import PurePosixPath
    got = PurePosixPath(str(filename or "").replace("\\", "/")).suffix.lower()
    if not ext or got not in _DOC_EXTS:
        return ""
    same = (got == ext) or (ext == ".xlsx" and got in (".xls", ".csv")) or (ext == ".pptx" and got == ".ppt") or (ext == ".docx" and got == ".doc")
    if same:
        return ""
    return (f"error: the user asked for a {_FORMAT_LABEL.get(ext, ext)}, not a {got} file. Create the {ext} file instead "
            f"(write_file / doc_create with a {ext} name).")
