"""Keeps the start of a chat's prompt the same from turn to turn, so the model server can reuse what it already read.

llama-server (cache_prompt) and vLLM (prefix caching) reuse a conversation only up to the first byte that differs from the
previous request. The system message and the tool list come first, before the whole history, and chat used to rebuild both
from the topic of the current message: a KB hit, a file request or a different tool set on turn 5 made the server read
turns 1-4 again. arrange() changes that in three ways:

  1. Text that belongs to one turn (KB hits, the files-so-far block, resume / format notes, briefs ...) is not put in the
     system message. It is appended to the user's own message, at the end of the prompt.
  2. Constant manuals (file / deck / workbook / html manuals, style, skills ...) and the document / data / pdf / sandbox
     tools stay for the rest of the chat once a turn needed them, in the order they first appeared, so the prefix only
     ever grows at its end. A manual is only kept while the tools it talks about are offered.
  3. Tools are sent in a stable order: the ones this chat has already seen first, new ones after them.

Web, image and MCP tools are never kept on by this module: whether they are offered stays the turn's own decision
(user's globe switch, knowledge-base coverage, privacy rules). Without a session id nothing is remembered.
Switch: config/app.json chat.stable_prefix (default true)."""
import threading
import time
from collections import OrderedDict

# parts whose text changes with the turn -> appended to the last user message
TURN_LABELS = {"kb", "kb-note", "kb-web-note", "prior-files", "attached-data-files", "web-lean", "deep", "brief-ask",
               "brief-build", "deck-brief", "pdf-brief", "docx-brief", "explicit-format", "missing-type", "build-resume",
               "cut-off-resume", "project-resume", "mcp"}
# constant manuals that stay once used -> the tool (name or prefix*) that must be offered for the manual to make sense
STICKY_LABELS = {"file-manual": None, "pptx-manual": "doc_create", "xlsx-manual": "doc_create", "html-manual": None,
                 "pdf-tools": "pdf_*", "attached-data": "data_query", "style": None, "visual": None, "sandbox":
                 "run_sandbox_python", "design-skill": None, "pptx-skill": "doc_create", "pdf-skill": None,
                 "spreadsheet-skill": "doc_create"}
# a kept manual replaces its short stand-in
REPLACES = {"file-manual": "file-lean", "visual": "no-ascii"}
# tools that stay offered for the rest of the chat once a turn offered them
STICKY_TOOL_PREFIXES = ("data_", "pdf_", "doc_")
STICKY_TOOL_NAMES = {"run_sandbox_python"}
TURN_MARK = "\n\n[context for this turn - provided by the system, not typed by the user]\n"

TTL_S = 8 * 3600
MAX_CHATS = 2000
_store: "OrderedDict" = OrderedDict()      # (uid, session_id) -> {"t", "parts": OrderedDict label->text, "tools": OrderedDict name->schema}
_lock = threading.Lock()


def enabled() -> bool:
    try:
        from .small_model import APP_CONFIG
        return (APP_CONFIG.get("chat") or {}).get("stable_prefix", True) is not False
    except Exception:
        return True


def _max_chars() -> int:
    try:
        from .small_model import APP_CONFIG
        return int((APP_CONFIG.get("chat") or {}).get("sticky_max_tokens", 6000)) * 4
    except Exception:
        return 24000


def _name(tool: dict) -> str:
    return str((tool.get("function") or {}).get("name") or "")


def _sticky_tool(name: str) -> bool:
    return name in STICKY_TOOL_NAMES or name.startswith(STICKY_TOOL_PREFIXES)


def _offered(need, names: set) -> bool:
    if need is None:
        return True
    return any(n.startswith(need[:-1]) for n in names) if need.endswith("*") else need in names


def reset(uid=None, session_id=None) -> None:
    """Forget one chat (or everything): used when a model is swapped and by tests."""
    with _lock:
        if uid is None and session_id is None:
            _store.clear()
        else:
            _store.pop((uid, session_id), None)


def _state(key):
    now = time.time()
    with _lock:
        st = _store.get(key)
        if st is None or now - st["t"] > TTL_S:
            st = {"t": now, "parts": OrderedDict(), "tools": OrderedDict()}
        st["t"] = now
        _store[key] = st
        _store.move_to_end(key)
        while len(_store) > MAX_CHATS:
            _store.popitem(last=False)
        return st


def arrange(parts: list, tools: list, uid=None, session_id=None, allow_tools: bool = True) -> tuple:
    """parts: [(label, text)] in the order the turn built them; tools: this turn's tool schemas.
    -> (system parts [(label, text)], turn parts [(label, text)], tools).
    allow_tools False (the turn offers no tools at all, or restricts them on purpose): the tool list is left as given."""
    if not enabled():
        return list(parts), [], list(tools)
    turn = [(l, t) for l, t in parts if l in TURN_LABELS]
    system = [(l, t) for l, t in parts if l not in TURN_LABELS]
    if uid is None or session_id is None:
        return system, turn, list(tools)
    st = _state((uid, session_id))
    with _lock:
        # ---- tools: remembered ones first, in the order this chat first saw them; then this turn's new ones
        out_tools = list(tools)
        if allow_tools and tools:
            wanted = OrderedDict((_name(t), t) for t in tools)
            for name, schema in wanted.items():
                if _sticky_tool(name):
                    st["tools"].setdefault(name, schema)
                    st["tools"][name] = schema            # the newest definition wins, the position stays
            first_seen = st.setdefault("order", [])
            for name in wanted:
                if name not in first_seen:
                    first_seen.append(name)
            merged = OrderedDict()
            for name in first_seen:
                if name in wanted:
                    merged[name] = wanted[name]
                elif name in st["tools"]:
                    merged[name] = st["tools"][name]
            out_tools = list(merged.values())
        names = {_name(t) for t in out_tools}
        # ---- manuals: this turn's, plus the ones an earlier turn used while their tools are still offered
        now_labels = {l for l, _ in system}
        for l, t in system:
            if l in STICKY_LABELS:
                st["parts"][l] = t                        # same position, newest text
        while sum(len(t) for t in st["parts"].values()) > _max_chars() and st["parts"]:
            st["parts"].popitem(last=False)               # too much kept: let the oldest manual go
        kept = [(l, t) for l, t in st["parts"].items() if _offered(STICKY_LABELS[l], names)]
        kept_labels = {l for l, _ in kept}
        dropped = {REPLACES[l] for l in kept_labels if l in REPLACES}
        base = [(l, t) for l, t in system if l not in STICKY_LABELS and l not in dropped]
        system = base + kept
        _ = now_labels
    return system, turn, out_tools


def turn_block(turn: list) -> str:
    """The per-turn parts as one block for the end of the user's message ('' when there are none)."""
    text = "\n\n".join(t.strip() for _, t in turn if t and t.strip())
    return (TURN_MARK + text) if text else ""


def attach(msgs: list, block: str) -> None:
    """Append the turn block to the last user message (text or multi-part content), in place."""
    if not block:
        return
    for m in reversed(msgs):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            m["content"] = list(c) + [{"type": "text", "text": block.lstrip("\n")}]
        else:
            m["content"] = (str(c or "")) + block
        return
    msgs.append({"role": "user", "content": block.lstrip("\n")})
