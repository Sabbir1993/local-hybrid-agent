"""
core/grammar.py - GBNF grammar for grammar-constrained executor tool calls.

The executor lane (small local model) occasionally drifts out of the tool-call
JSON format, which historically required the regex repair machinery in
core/agent_loop.py (safe_parse_and_repair_args). Constraining generation with a
GBNF grammar removes that failure class at the sampler level:

  * free text is always allowed  -> final answers and streaming stay unchanged
  * text may never START a "<tool_call>" envelope (prefix-exclusion alternation)
  * therefore any emitted envelope MUST contain strictly valid JSON
  * "name" MUST be one of the tool schemas actually offered to the lane
  * "arguments" MUST be a structurally valid JSON object

The envelope matches the ASCII "<tool_call>{...}</tool_call>" format that the
text-fallback parser (_extract_text_tool_calls) already understands, so even if
llama-server's native --jinja extraction misses it, the calls are still
recovered. Disable at runtime via config/app.json -> router.executor_grammar: false.
"""

import json
import re
import sys
from typing import Optional

# The envelope format used by the fallback parser in core/agent_loop.py.
TC_OPEN = "<tool_call>"
TC_CLOSE = "</tool_call>"

_grammar_cache: dict = {}
_envelope_verified: Optional[bool] = None


def _verify_envelope() -> bool:
    """Check agent_loop's compiled regexes really parse this envelope format."""
    global _envelope_verified
    if _envelope_verified is not None:
        return _envelope_verified
    try:
        from .agent_loop import _extract_text_tool_calls
        joined = "\n".join(
            c for c in _extract_text_tool_calls.__code__.co_consts
            if isinstance(c, str)
        )
        _envelope_verified = TC_OPEN in joined and TC_CLOSE in joined
    except Exception as e:
        print(f"[grammar] envelope verification failed: {e}", file=sys.stderr)
        _envelope_verified = False
    return _envelope_verified


def _lit(s: str) -> str:
    """Quote a literal string for GBNF."""
    out = []
    for ch in s:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20:
            out.append(f"\\x{ord(ch):02X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


# Prefix-exclusion alternation: any text that does not START the open tag.
# "<" not followed by the remaining prefix of "tool_call" stays plain text,
# so the toolcall rule is the ONLY way to open an envelope -> JSON enforced.
_PTEXT_RULE = '''ptext ::= ( pchar )*
pchar ::= [^<] | "<" [^t] | "<t" [^o] | "<to" [^o] | "<too" [^l] | "<tool" [^_]
        | "<tool_" [^c] | "<tool_c" [^a] | "<tool_ca" [^l] | "<tool_cal" [^l]
        | "<tool_call" [^>]'''

_JSON_RULES = '''ws ::= [ \\t\\r\\n]*
jo ::= "{" ws ( jstr ws ":" ws jv ( ws "," ws jstr ws ":" ws jv )* )? ws "}"
ja ::= "[" ws ( jv ( ws "," ws jv )* )? ws "]"
jv ::= jstr | jnum | "true" | "false" | "null" | ja | jo
jstr ::= "\\"" ( [^\\"\\\\\\x00-\\x1F] | "\\\\" ( [\\"\\\\/bfnrt] | "u" hex hex hex hex ) )* "\\""
hex ::= [0-9a-fA-F]
jnum ::= "-"? [0-9]+ ("." [0-9]+)? ([eE] "-"? [0-9]+)?'''


def build_tool_call_grammar(tool_schemas: list) -> Optional[str]:
    """Build a GBNF grammar allowing free text plus valid <tool_call> envelopes.

    Returns None when the envelope cannot be verified or no tools are given, so
    callers can fall back to unconstrained generation transparently.
    """
    try:
        if not _verify_envelope():
            return None
        names = sorted({
            t.get("function", {}).get("name")
            for t in (tool_schemas or [])
            if isinstance(t, dict) and t.get("function", {}).get("name")
        })
        if not names:
            return None
        key = tuple(names)
        if key in _grammar_cache:
            return _grammar_cache[key]

        name_alts = " | ".join(_lit(n) for n in names)
        grammar = (
            "root ::= chunk\n"
            "chunk ::= ptext ( toolcall ptext )*\n"
            + _PTEXT_RULE + "\n"
            + f'toolcall ::= {_lit(TC_OPEN)} ws tco ws {_lit(TC_CLOSE)}\n'
            + 'tco ::= "{" ws "\\"name\\"" ws ":" ws tname ws "," ws "\\"arguments\\"" ws ":" ws jo ws "}"\n'
            + f"tname ::= ( {name_alts} )\n"
            + _JSON_RULES + "\n"
        )
        _grammar_cache[key] = grammar
        return grammar
    except Exception as e:
        print(f"[grammar] build failed: {e}", file=sys.stderr)
        return None


def envelope_examples() -> str:
    """Few-shot tool-call format fragment for the executor system prompt.

    Teaches the exact envelope the grammar constrains, so the model and the
    sampler agree on the format. Returns "" if the envelope can't be verified.
    """
    if not _verify_envelope():
        return ""
    ex = [
        ("write_file", {"path": "notes/todo.md", "content": "# Todo\n- [ ] buy milk"}),
        ("read_file", {"path": "index.html"}),
        ("run_python", {"code": "print(2+2)"}),
    ]
    lines = [
        "TOOL CALL FORMAT — follow exactly. To execute a tool, emit:",
        TC_OPEN,
        '{"name": "tool_name", "arguments": {"param": "value"}}',
        TC_CLOSE,
        "Examples:",
    ]
    for name, args in ex:
        lines.append(TC_OPEN)
        lines.append(json.dumps({"name": name, "arguments": args}, ensure_ascii=False))
        lines.append(TC_CLOSE)
    lines += [
        "Rules: call ONE tool at a time, wait for its result, then either call the "
        "next tool or write the final answer. If no tool is needed, reply with plain "
        "text and NO tool call. Never paste code you did not run when the user asked "
        "you to build or run something — call write_file / run_python instead. Paths "
        "are workspace-relative (e.g. 'app/main.py'), never absolute.",
    ]
    return "\n\n" + "\n".join(lines)
