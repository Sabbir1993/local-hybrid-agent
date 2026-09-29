"""
routes/common/think_splitter.py - ThinkSplitter: splits streamed content into
answer text and inline <think> reasoning chunks.
"""

from core.monitor import monitor_token


_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"


class ThinkSplitter:
    """Splits streamed `content` into answer text and inline <think> reasoning.

    Handles tags split across chunks, and the "forced-open" case: templates that
    pre-fill <think> in the prompt, so the model streams reasoning with no opening
    tag and then a bare </think>. That first orphan close yields ("reclaim", "") -
    everything streamed as content so far was reasoning."""

    def __init__(self):
        self.in_think = False
        self.seen_tag = False
        self.carry = ""
        self.strip_ws = False   # drop the blank lines right after </think>

    def _hold(self, s: str) -> int:
        """Length of a trailing partial tag in `s` to keep for the next chunk."""
        tags = [_THINK_CLOSE] if self.in_think else [_THINK_OPEN] + ([] if self.seen_tag else [_THINK_CLOSE])
        for k in range(min(len(s), len(_THINK_CLOSE) - 1), 0, -1):
            if any(t.startswith(s[-k:]) for t in tags):
                return k
        return 0

    def _content(self, out: list, text: str):
        if self.strip_ws:
            text = text.lstrip()
            if text:
                self.strip_ws = False
        if text:
            out.append(("content", text))

    def feed(self, chunk: str) -> list:
        s, self.carry, out = self.carry + chunk, "", []
        while s:
            if self.in_think:
                i = s.find(_THINK_CLOSE)
                if i < 0:
                    k = self._hold(s)
                    if k:
                        s, self.carry = s[:-k], s[-k:]
                    if s:
                        out.append(("thought", s))
                    break
                if i:
                    out.append(("thought", s[:i]))
                self.in_think, self.strip_ws = False, True
                s = s[i + len(_THINK_CLOSE):]
                continue
            i_open = s.find(_THINK_OPEN)
            i_close = -1 if self.seen_tag else s.find(_THINK_CLOSE)
            if i_close >= 0 and (i_open < 0 or i_close < i_open):
                # orphan close: the reasoning had no opening tag
                out.append(("reclaim", ""))
                if i_close:
                    out.append(("thought", s[:i_close]))
                self.seen_tag, self.strip_ws = True, True
                s = s[i_close + len(_THINK_CLOSE):]
                continue
            if i_open >= 0:
                self._content(out, s[:i_open])
                self.in_think = self.seen_tag = True
                s = s[i_open + len(_THINK_OPEN):]
                continue
            k = self._hold(s)
            if k:
                s, self.carry = s[:-k], s[-k:]
            self._content(out, s)
            break
        return out

    def flush(self) -> list:
        s, self.carry = self.carry, ""
        if not s:
            return []
        return [("thought", s)] if self.in_think else [("content", s)]


def _emit_split(think: ThinkSplitter, parts: list, content_acc: list, reasoning_acc: list, rid):
    """Turn ThinkSplitter output into stream events, keeping the accumulators in sync."""
    for kind, text in parts:
        if kind == "reclaim":
            moved = "".join(content_acc)
            content_acc.clear()
            if moved:
                reasoning_acc.append(moved)
                yield ("content_to_thought", moved)
            continue
        if rid:
            monitor_token(rid, 1)
        if kind == "thought":
            reasoning_acc.append(text)
            yield ("thought_delta", text)
        else:
            content_acc.append(text)
            yield ("content_delta", text)
