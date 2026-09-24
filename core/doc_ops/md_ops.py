"""Markdown source documents: the editable form behind generated PDFs (and .md).

A generated PDF keeps its markdown `source_spec`; edits patch that source by
section and the PDF is re-rendered, so unchanged sections render identically.

Addresses: `sec0` (text before the first heading), `sec3` (3rd heading and its
body, up to the next heading of any level).
"""

import re

from .base import DocOpError, EditResult, Snapshot, digest, op_arg, require

_H = re.compile(r"^(#{1,6})\s+(.*)")


class _Sec:
    __slots__ = ("heading", "body")

    def __init__(self, heading, body):
        self.heading, self.body = heading, body

    def text(self) -> str:
        return (self.heading + "\n" if self.heading else "") + self.body


def _split(md: str) -> list[_Sec]:
    secs = [_Sec("", "")]
    in_code = False
    for line in md.splitlines(keepends=True):
        if line.strip().startswith("```"):
            in_code = not in_code
        if not in_code and _H.match(line.strip()):
            secs.append(_Sec(line.rstrip("\r\n"), ""))
        else:
            secs[-1].body += line
    return secs


def _join(secs: list[_Sec]) -> str:
    out = []
    for s in secs:
        t = s.text()
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(t)
    return "".join(out)


def inspect_text(md: str) -> dict:
    secs = _split(md)
    els = []
    for i, s in enumerate(secs):
        if i == 0 and not s.body.strip():
            continue
        body = " ".join(s.body.split())
        els.append({"addr": f"sec{i}", "kind": s.heading.split(" ")[0] if s.heading else "preamble",
                    "text": (s.heading.lstrip("# ") + " :: " if s.heading else "") + (body[:300] + ("..." if len(body) > 300 else ""))})
    return {"kind": "markdown", "header": f"Document source: {len(secs) - 1} sections", "elements": els}


def _idx(secs, addr) -> int:
    m = re.fullmatch(r"sec(\d+)", str(addr).strip(), re.I)
    require(bool(m), f"bad section address '{addr}' (use sec3)")
    i = int(m.group(1))
    require(0 <= i < len(secs), f"section {i} does not exist ({len(secs) - 1} sections)")
    return i


def apply_text(md: str, ops: list[dict]) -> tuple[str, list[str]]:
    secs = _split(md)
    snap = Snapshot(lambda s: digest(s.text().encode()))
    snap.take((f"sec{i}", s) for i, s in enumerate(secs))
    changes = []
    for op in ops:
        kind = str(op.get("op", "")).lower()
        if kind in ("replace_section", "set_text", "set_section"):
            i = _idx(secs, op_arg(op, "addr", "section", "target"))
            new = str(op_arg(op, "text", "markdown", default=""))
            old_gap = secs[i].body.endswith("\n\n")
            first = new.lstrip("\n").split("\n", 1)
            if _H.match(first[0].strip()):
                secs[i].heading = first[0].strip()
                secs[i].body = (first[1] if len(first) > 1 else "")
            else:
                secs[i].body = new
            if not secs[i].body.endswith("\n"):
                secs[i].body += "\n"
            if old_gap and not secs[i].body.endswith("\n\n"):
                secs[i].body += "\n"   # keep the blank line before the next heading
            snap.touch(secs[i])
            changes.append(f"sec{i} rewritten")
        elif kind == "set_heading":
            i = _idx(secs, op_arg(op, "addr", "section"))
            require(bool(secs[i].heading), f"sec{i} has no heading")
            level = secs[i].heading.split(" ")[0]
            secs[i].heading = f"{level} {op_arg(op, 'text', 'title')}"
            snap.touch(secs[i])
            changes.append(f"sec{i} heading set")
        elif kind == "insert_section_after":
            i = _idx(secs, op_arg(op, "after", "addr"))
            md_new = str(op_arg(op, "text", "markdown"))
            new_secs = [s for s in _split(md_new if md_new.endswith("\n") else md_new + "\n")
                        if s.heading or s.body.strip()]
            for s in new_secs:
                snap.touch(s)
            secs[i + 1:i + 1] = new_secs
            changes.append(f"{len(new_secs)} section(s) inserted after sec{i}")
        elif kind == "delete_section":
            i = _idx(secs, op_arg(op, "addr", "section"))
            snap.touch(secs[i])
            del secs[i]
            changes.append(f"sec{i} deleted")
        elif kind == "replace_text":
            find, repl = str(op_arg(op, "find")), str(op.get("replace", ""))
            n = 0
            for s in secs:
                if find in s.text():
                    s.heading, s.body = s.heading.replace(find, repl), s.body.replace(find, repl)
                    snap.touch(s)
                    n += 1
            require(n > 0, f"'{find}' not found")
            changes.append(f"replaced '{find}' in {n} section(s)")
        else:
            raise DocOpError(f"unknown op '{op.get('op')}' for a markdown/PDF source (have: replace_section, "
                             "set_heading, insert_section_after, delete_section, replace_text)")
    problems = snap.problems((f"sec{i}", s) for i, s in enumerate(secs))
    require(not problems, "edit would change sections you did not ask for: " + "; ".join(problems[:8]))
    return _join(secs), changes


def inspect(data: bytes) -> dict:
    return inspect_text(data.decode("utf-8", errors="replace"))


def apply(data: bytes, ops: list[dict]) -> EditResult:
    text, changes = apply_text(data.decode("utf-8", errors="replace"), ops)
    return EditResult(text.encode("utf-8"), changes, [])
