"""How a file-writing call is kept in the conversation history after it ran.

The full text of every write is not re-sent on each later step (it is in the file, and the tool result says what was
saved). The old stub put a look-alike VALUE in its place - "content": "<5800 chars written to x.py>" - and models copied
that shape: the next append_file sent the stub as the file text (a syntax error in .py, silently written into .md/.txt).
A call cut at the size cap was kept with "content": "" and its whole cut-off text under "_partial", so the model saw
an empty call of its own and believed it had "sent placeholder text".

Now the big argument is REMOVED and a key that is not a tool parameter says why (`content_omitted`). A model that copies
that shape sends no content at all, which fails at once with a cheap, explicit error (repair.py) instead of writing junk.
"""
import re

# argument -> verb used in the note
_BIG_ARGS = {"content": "saved", "new_string": "saved", "text": "saved"}
KEEP_CHARS = 400                       # a short argument is kept as sent: it costs little and shows a real example
OMITTED_SUFFIX = "_omitted"
CUT_NOTE_KEY = "content_note"
_INTERNAL = ("_partial", "_oversize_tokens", "_create")

# the stub text written by older builds (still present in stored sessions) and the markers above, copied as content
STUB_RX = re.compile(r"^\s*<\d+ chars (?:written to|inserted in|replaced in) [^\n>]*>\s*$")
_MARKER_RX = re.compile(r"^\s*\d+ chars - (?:already saved in the file|cut at the size limit)", re.IGNORECASE)


def slim_write_args(args: dict) -> dict:
    """History copy of a write call's arguments: path and small values as sent, big text replaced by a note key."""
    out = dict(args) if isinstance(args, dict) else {}
    cut = int(out.get("_oversize_tokens") or 0)
    for k in _INTERNAL:
        out.pop(k, None)
    if cut:
        out.pop("content", None)
        out[CUT_NOTE_KEY] = (f"about {cut} tokens - cut at the size limit; the tool result says which part was saved. "
                             "Not shown again.")
        return out
    for key in _BIG_ARGS:
        v = out.get(key)
        if isinstance(v, str) and len(v) > KEEP_CHARS:
            out.pop(key)
            out[key + OMITTED_SUFFIX] = f"{len(v)} chars - already saved in the file, not shown again"
    return out


def stub_content_error(tool: str, args: dict):
    """An error when a write call carries a history note instead of file text, else None."""
    if not isinstance(args, dict):
        return None
    for key in ("content", "new_string", "text"):
        v = args.get(key)
        if isinstance(v, str) and (STUB_RX.match(v) or _MARKER_RX.match(v)):
            return (f"error: `{key}` holds a history note (\"{v.strip()[:80]}\"), not file text. Notes like that only "
                    "mark earlier calls whose text is already saved in the file. Nothing was written. Send the real "
                    f"lines of the next section in `{key}`.")
    copied = [k for k in args if k.endswith(OMITTED_SUFFIX) or k == CUT_NOTE_KEY]
    if copied and not any(isinstance(args.get(k), str) and args.get(k) for k in ("content", "new_string", "text")):
        return (f"error: this {tool} call has `{copied[0]}` but no text. `{copied[0]}` only appears in the history of "
                "earlier calls whose text is already saved. Nothing was written. Send the real lines of the next section "
                "in `content` (or `new_string` for edit_file).")
    return None
