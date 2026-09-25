"""Auto-generate commit messages / PR title+description from a git diff.

Uses the "Commit & PR messages" job (core/lanes.py; the fast executor lane by default) (same pattern as routes/chat.py::_summarize_history) -
a short, low-temperature, non-streaming completion. Never calls out to GitHub/MCP;
this only talks to the app's own configured model. Diffs stay on the executor lane by
default (prefer local) since they may contain secrets/credentials - see generate().
"""

import re
from typing import Optional

from . import lanes

MAX_DIFF_CHARS = 12000  # keep the prompt small/cheap; a huge diff gets truncated

_COMMIT_SYSTEM_PROMPT = (
    "You write a single concise git commit message summarizing a diff. "
    "Use the conventional style: a short imperative summary line (max ~72 chars), "
    "optionally followed by a blank line and 1-3 bullet points for non-obvious details. "
    "Do not invent changes not present in the diff. Output ONLY the commit message, "
    "no preamble, no markdown fences."
)

_PR_SYSTEM_PROMPT = (
    "You write a pull request title and description summarizing a diff. "
    "Output exactly two lines in this format, nothing else:\n"
    "TITLE: <short imperative title, max ~70 chars>\n"
    "BODY: <1-3 sentence summary of what changed and why, plain text, no markdown fences>"
)


def _strip_think(text: str) -> str:
    m = re.search(r"<think>[\s\S]*?</think>", text or "")
    return (text[m.end():] if m else (text or "")).strip()


async def _complete(system_prompt: str, diff_text: str, user_id: Optional[int] = None) -> str:
    if not diff_text.strip():
        raise ValueError("no diff content to summarize")
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = diff_text[:MAX_DIFF_CHARS] + "\n... (diff truncated)"

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"DIFF:\n{diff_text}"},
        ],
        # Reasoning models (e.g. Spark-X2.5) can spend the whole budget on a hidden
        # <think> block before the actual answer - match the codebase's other
        # auxiliary-completion budget (routes/chat.py::_summarize_history) for headroom.
        "max_tokens": 2048,
        "temperature": 0.2,
        "stream": False,
    }

    # "Commit & PR messages" job: the fast helper by default, remappable in
    # Settings -> Models (core/lanes.py walks the fallback chain)
    data, _t = await lanes.post_chat("commit_msg", payload, user_id)
    text = lanes.message_text(data)
    return _strip_think(text)


async def generate_commit_message(diff_text: str, user_id: Optional[int] = None) -> str:
    return await _complete(_COMMIT_SYSTEM_PROMPT, diff_text, user_id)


async def generate_pr_summary(diff_text: str, user_id: Optional[int] = None) -> dict:
    text = await _complete(_PR_SYSTEM_PROMPT, diff_text, user_id)
    title, body = "", ""
    for line in text.splitlines():
        if line.upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()
        elif line.upper().startswith("BODY:"):
            body = line.split(":", 1)[1].strip()
    if not title and not body:
        # model didn't follow the format; use the raw text as the title/body split
        parts = text.split("\n", 1)
        title = parts[0].strip()[:70]
        body = parts[1].strip() if len(parts) > 1 else ""
    return {"title": title, "body": body}
