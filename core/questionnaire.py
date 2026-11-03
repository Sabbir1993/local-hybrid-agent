"""
core/questionnaire.py - make the ```questionnaire card block a model wrote readable by the client.

The client (frontend/src/utils/questions.ts, static/js/markdown.js) parses the fenced JSON with JSON.parse; one trailing
comma, a curly quote or a raw line break inside a string and the card falls back to raw JSON in a code block. This
repairs those slips on the server before the reply is stored, and logs the raw text of a block that cannot be repaired
so the cause can be found.
"""

import json
import logging
import re

log = logging.getLogger("questionnaire")

_FENCE_RX = re.compile(r"```questionnaire[ \t]*\r?\n([\s\S]*?)```")
_TRAILING_COMMA_RX = re.compile(r",(\s*[}\]])")
_QUOTES = {"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"}


def _balanced_object(text: str) -> str:
    """The first {...} with balanced braces (strings respected), or the text itself."""
    start = text.find("{")
    if start < 0:
        return text
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def _escape_raw_newlines(text: str) -> str:
    """A raw line break or tab inside a JSON string is invalid: escape it."""
    out, in_str, esc = [], False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            elif ch == "\n":
                ch = "\\n"
            elif ch == "\r":
                ch = ""
            elif ch == "\t":
                ch = "\\t"
        elif ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


def parse_card(raw: str):
    """-> dict, or None when even the repairs cannot read it."""
    attempts = [raw]
    body = _balanced_object(raw.strip())
    attempts.append(body)
    attempts.append(_TRAILING_COMMA_RX.sub(r"\1", _escape_raw_newlines(body)))
    attempts.append(_TRAILING_COMMA_RX.sub(r"\1", _escape_raw_newlines("".join(_QUOTES.get(c, c) for c in body))))
    for a in attempts:
        try:
            data = json.loads(a)
            if isinstance(data, dict) and isinstance(data.get("questions"), list):
                return data
        except ValueError:
            continue
    return None


def repair(content: str) -> tuple[str, bool]:
    """Rewrite every unreadable ```questionnaire block of `content` as clean JSON. -> (content, changed)."""
    if not content or "```questionnaire" not in content:
        return content, False
    changed = False

    def fix(m):
        nonlocal changed
        raw = m.group(1)
        try:
            ok = isinstance(json.loads(raw), dict)
        except ValueError:
            ok = False
        if ok:
            return m.group(0)
        data = parse_card(raw)
        if data is None:
            log.warning("questionnaire block is not valid JSON and could not be repaired: %r", raw[:2000])
            return m.group(0)
        changed = True
        log.info("questionnaire block repaired")
        return "```questionnaire\n" + json.dumps(data, ensure_ascii=False) + "\n```"

    return _FENCE_RX.sub(fix, content), changed
