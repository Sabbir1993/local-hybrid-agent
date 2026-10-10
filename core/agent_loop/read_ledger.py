"""Per-run ledger of file reads: stops the read -> cleared -> read-again cycle, including overlapping ranges.

A model that keeps asking for the same lines makes no progress and burns a full model round trip each time. The
exact same call is the easy case; the common one is overlap: offset 185 limit 30, then offset 160 limit 60, then
offset 170 limit 40 - mostly lines it already has. So the ledger tracks WHICH LINES of each file it has seen, not
just which calls it made:

  * a request fully covered by lines still in the prompt gets a one-line note instead of the text again;
  * a request partly covered is trimmed to the lines it does not have yet (the model is told what was left out);
  * lines whose earlier result was cleared to save context (core/agent_loop/clearing.py) may be read once more, but
    a repeat after that gets the digest the clearing step kept, plus an instruction to act.

A write to the workspace invalidates the ledger (the file may have changed), exactly like `seen_reads` for grep.
"""

import re
from typing import Optional

from ..agent_tools.limits import agent_limit
from .clearing import CLEARED_PREFIX

DIGEST_CHARS = 700
MAX_NOTES = 2              # "you already have it" notes for one range before the lines are re-sent with an order to act
MAX_FRESH_READS = 2         # after lines were cleared, they are served in full this many times per run
_FOOTER_RX = re.compile(r"\[[^\]\n]*?: lines (\d+)-(\d+) of (\d+)")


def _int(v, default):
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _subtract(start: int, end: int, ranges: list) -> list:
    """The parts of [start, end] not inside any of `ranges` ([(a, b), ...])."""
    gaps, cur = [], start
    for a, b in sorted(ranges):
        if b < cur:
            continue
        if a > end:
            break
        if a > cur:
            gaps.append((cur, min(a - 1, end)))
        cur = max(cur, b + 1)
        if cur > end:
            break
    if cur <= end:
        gaps.append((cur, end))
    return gaps


class ReadLedger:
    def __init__(self):
        self._files: dict = {}      # path -> {"reads": [{"a","b","tc"}], "rereads": int, "digest": str}
        self._notes: dict = {}      # (path, start, end) -> times the model was told "you already have this"

    @staticmethod
    def _path(args: dict) -> str:
        a = args or {}
        return str(a.get("path") or a.get("file") or "")

    @staticmethod
    def _span(args: dict) -> tuple:
        a = args or {}
        start = max(1, _int(a.get("offset") or a.get("start_line"), 1))
        limit = max(1, _int(a.get("limit"), agent_limit("read_file_default_limit")))
        return start, start + limit - 1

    @staticmethod
    def _in_context(tc_id: str, msgs: list) -> bool:
        for m in msgs:
            if m.get("role") == "tool" and m.get("tool_call_id") == tc_id:
                return not str(m.get("content") or "").startswith(CLEARED_PREFIX)
        return False

    def plan(self, args: dict, msgs: list) -> Optional[dict]:
        """None: run read_file as asked. {"note": text}: answer with this text instead of running it.
        {"args": new_args, "prefix": text}: run it with these (trimmed) args and put `prefix` before the result."""
        path = self._path(args)
        rec = self._files.get(path)
        if not path or not rec or not rec["reads"]:
            return None
        start, end = self._span(args)
        total = rec.get("total") or 0
        if total:
            if start > total:
                return {"note": f"note: {path} has only {total} lines, so there is nothing at line {start}. You have "
                                "already read to the end of it; use what you have."}
            end = min(end, total)          # asking for more lines than the file has is not asking for more of the file
        live = [(r["a"], r["b"]) for r in rec["reads"] if self._in_context(r["tc"], msgs)]
        gaps = _subtract(start, end, live)
        if live and not gaps:
            key = (path, start, end)
            self._notes[key] = self._notes.get(key, 0) + 1
            if self._notes[key] > MAX_NOTES:
                # a note did not get through: hand the lines over once more and order it to act, then the caller
                # pauses read_file for a few steps ("escalate")
                return {"args": dict(args), "escalate": True,
                        "prefix": f"note: you have asked for lines {start}-{end} of {path} {self._notes[key]} times; "
                                  "the earlier copy is above and the file has not changed. Here they are once more. "
                                  "STOP reading: act on them now (edit_file / write_file, run a command, or give your answer)."}
            seen = ", ".join(f"{a}-{b}" for a, b in sorted(set(live)))
            return {"note": f"note: lines {start}-{end} of {path} are already in this conversation (you read {seen}) "
                            "and the file has not changed. Use them instead of reading again. If you need lines "
                            "outside that, ask for those lines only; otherwise continue with the task."}
        new_span = (gaps[-1][1] - gaps[0][0] + 1) if gaps else 0
        # trim only when it saves something real: a hole in the middle of a long range would "trim" to the whole range
        if live and gaps and new_span <= 0.8 * (end - start + 1):                      # partly covered
            new_start, new_end = gaps[0][0], gaps[-1][1]
            kept = [(max(a, start), min(b, end)) for a, b in live if b >= start and a <= end]
            have = ", ".join(f"{a}-{b}" for a, b in sorted(kept))
            trimmed = dict(args)
            trimmed.pop("start_line", None)
            trimmed["offset"] = new_start
            trimmed["limit"] = new_end - new_start + 1
            return {"args": trimmed,
                    "prefix": f"note: you asked for lines {start}-{end} but already have {have} above; "
                              f"showing only the new lines {new_start}-{new_end}."}
        # nothing of it is in the prompt any more: earlier reads were cleared to save context
        covered = [(r["a"], r["b"]) for r in rec["reads"]]
        if not _subtract(start, end, covered):
            rec["rereads"] += 1
            if rec["rereads"] > MAX_FRESH_READS:
                return {"note": f"note: you have already read these lines of {path} {rec['rereads']} times in this run "
                                "and are not making progress with them. Do not read them again. What you saw (the top "
                                "of the file):\n" + rec["digest"]
                                + "\n...\nWrite down what you learned (update_plan_item / your answer / AGENTS.md) and "
                                  "move on to the next file or to the result."}
        return None

    def record(self, args: dict, tc_id: str, result) -> None:
        path = self._path(args)
        text = str(result or "")
        if not path or text.startswith("error:") or text.startswith("File not found"):
            return
        m = _FOOTER_RX.search(text[-400:])
        if not m:
            return
        rec = self._files.setdefault(path, {"reads": [], "rereads": 0, "digest": text[:DIGEST_CHARS], "total": 0})
        rec["total"] = int(m.group(3))
        rec["reads"].append({"a": int(m.group(1)), "b": int(m.group(2)), "tc": tc_id})

    def clear(self) -> None:
        self._files.clear()
        self._notes.clear()
