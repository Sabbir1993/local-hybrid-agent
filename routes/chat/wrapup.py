"""The tool-free wrap-up turn of a chat run.

When a run uses up its tool rounds with work still open, the last model call is made without tools so the model can say
where things stand. A model that wants to keep going tries to call a tool anyway. Local models then write the call out as
text ("<tool_call><function=doc_edit>..."), and some cloud providers answer with their own message ('Tool "x" is not
available in this request. Available tools: (none)'). Neither is an answer: this module keeps both off the screen and
turns them into an honest status the person can act on (the Continue button)."""
import re

# what the model is told before the tool-free call
WRAPUP_NOTE = ("[system note] The tool rounds for this reply are used up, so no tools are available now. Do not call a tool and "
               "do not write one out. In a few plain sentences say what is finished, what is not finished yet, and that the "
               "person can press Continue to carry on.")
# what the model is told while rounds are still left, once they are running low
LOW_ROUNDS_NOTE = ("[system note] Only {n} tool rounds are left in this reply. Put the most important remaining work into as few "
                   "calls as possible (send many operations in ONE call), and do not retry a call that already failed in the same "
                   "way. Then say briefly what is done and what is not.")

_MARKUP_START = re.compile(r"<tool_call>|<function[=:\s]", re.I)
_PREFIXES = ("<tool_call>", "<function=", "<function:", "<function ")
_PROVIDER_NO_TOOL = re.compile(
    r"Tool\s+[\"'`]?[\w.\-]+[\"'`]?\s+is\s+not\s+available\s+in\s+this\s+request[^\n]*?(?:answer\s+as\s+plain\s+text\.?|\.\s*$|$)",
    re.I | re.M)


class ToolTextFilter:
    """Streams text but never the start of a tool call written as text. Characters that could still turn into "<tool_call>"
    are held back until the next chunk shows what they are."""

    def __init__(self):
        self.open = False
        self._hold = ""

    def feed(self, chunk: str) -> str:
        if self.open or not chunk:
            return ""
        text = self._hold + chunk
        self._hold = ""
        m = _MARKUP_START.search(text)
        if m:
            self.open = True
            return text[:m.start()]
        cut = text.rfind("<", max(0, len(text) - 12))
        if cut >= 0 and any(p.startswith(text[cut:].lower()) for p in _PREFIXES):
            self._hold = text[cut:]
            return text[:cut]
        return text

    def flush(self) -> str:
        out, self._hold = ("" if self.open else self._hold), ""
        return out


def strip_markup(text: str) -> str:
    """Remove tool calls written as text, finished or cut off mid-way."""
    text = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", text or "")
    text = re.sub(r"<function[=:\s][\s\S]*?</function>", "", text)
    text = re.sub(r"<tool_call>[\s\S]*$", "", text)
    text = re.sub(r"<function[=:\s][\s\S]*$", "", text)
    return text.strip()


def status_text(turns: int, files) -> str:
    done = ""
    names = [str(f) for f in (files or []) if f]
    if names:
        done = " Already saved: " + ", ".join(dict.fromkeys(names[-4:])) + "."
    return (f"I used all {turns} steps for this reply before the work was finished.{done} "
            "Press **Continue** and I will carry on from where it stopped.")


def finish_text(content: str, turns: int, files) -> tuple:
    """(text to show, whether it replaced what the model wrote). Empty text, a provider's 'tool not available' message or
    leftover tool markup become the status; a real summary is kept with those parts removed."""
    raw = content or ""
    cleaned = _PROVIDER_NO_TOOL.sub("", strip_markup(raw)).strip()
    if len(cleaned) < 20:
        return status_text(turns, files), True
    return cleaned, cleaned != raw.strip()
