"""scripts/bench_chat_files.py - live benchmark: how well does the loaded local model generate DOCX / PDF files in chat?

Runs a fixed prompt set (tests/file_bench_prompts.py) through the real POST /chat/run tool loop, then downloads each file and
checks it (tests/file_bench_checks.py). One run = one context-size label, so compare two runs of the same prompts:

    # server already running (python server_manager.py), model loaded at the context you are measuring
    set BENCH_USER=...  &  set BENCH_PASSWORD=...        (a dedicated non-MFA account; never put them on the command line)
    python scripts/bench_chat_files.py run --label ctx32k --ctx 32768 --formats docx,pdf --out bench_out
    python scripts/bench_chat_files.py run --label ctx128k --ctx 131072 --formats docx,pdf --out bench_out
    python scripts/bench_chat_files.py report bench_out/ctx32k bench_out/ctx128k

Never start a second server_manager for this: it kills the running llama servers. To change the context use a temp copy of the
launch file and restart the user's own instance with A770_MODEL_CONFIGS_FILE=<copy> (see core/config.py); the real
config/model_configs.json stays untouched.

What is recorded per run (a JSON line in <out>/<label>/results.jsonl, plus route_log rows with mode "bench"):
  - tool calls and their ok flag, failure code via route_log.classify_tool_result
  - build completion: PDF doc_create -> doc_append* -> done=true; DOCX write_file of the .docx
  - finished-without-nudge: no announced-but-not-delivered retry and no "file was not generated" note
  - tokens: the server's prompt/completion totals, and an ESTIMATE of tokens per model turn (stream chars / 3.5, thinking
    included) against the 8000-token per-turn cap of core/deck_research.py build_limits (deck_max_tokens)
  - findings: the app's own in-loop "PDF check found" sentence plus our checks on the downloaded file
No prompt or answer text is stored; findings are fixed phrases and counts.
"""
import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TURN_TOKEN_CAP = 8000          # core/deck_research.py build_limits: deck_max_tokens
CHARS_PER_TOKEN = 3.5
_DL_RX = re.compile(r"\[DOWNLOAD:\s*([^\]]+?)\s*\]")
_PDF_CHECK_RX = re.compile(r"PDF check found (\d+) issue")
_NOT_GENERATED = "was not generated this time"
MAX_CARD_ROUNDS = 2
# the Word card has no per-prompt answers in tests/file_bench_prompts.py: the same neutral choices every time
DEFAULT_ANSWERS = {"How long should the document be?": "Short (4-5 sections)", "Who is it for?": "Technical team",
                   "Style?": "Formal report"}


# ------------------------------------------------------------------ SSE folding (pure, unit-tested)

def fold_chat_sse(lines) -> dict:
    """Fold a /chat/run SSE stream into one record. Pure."""
    rec = {"tools": [], "results": [], "turns": [], "nudges": 0, "text": "", "done": None, "events": 0, "md": ""}
    event, events = None, []
    cur = {"chars": 0}
    for line in lines:
        line = line.strip()
        if line.startswith("event: "):
            event = line[7:].strip()
            continue
        if not line.startswith("data: ") or not event:
            continue
        try:
            data = json.loads(line[6:])
        except ValueError:
            continue
        if not isinstance(data, dict):
            data = {}
        events.append(event)
        rec["events"] += 1
        if event == "delta":
            t = data.get("text") or ""
            rec["text"] += t
            cur["chars"] += len(t)
        elif event == "thought_delta":
            cur["chars"] += len(data.get("delta") or "")
        elif event in ("delta_reset", "delta_replace"):
            if event == "delta_reset":
                rec["text"] = ""
            else:
                rec["text"] = data.get("text") or rec["text"]
        elif event == "tool_call":
            args = data.get("args") if isinstance(data.get("args"), dict) else {}
            cur["chars"] += len(json.dumps(args, ensure_ascii=False))
            if data.get("name") in ("write_file", "doc_create", "doc_append"):
                rec["md"] += "\n" + str(args.get("content") or "")       # kept in memory for the outline checks, never written out
            rec["tools"].append({"name": data.get("name"), "id": data.get("id"),
                                 "done_flag": bool(args.get("done")), "file": str(args.get("file") or args.get("path") or ""),
                                 "args_chars": len(json.dumps(args, ensure_ascii=False))})
        elif event == "tool_result":
            res = str(data.get("result") or "")
            rec["results"].append({"name": data.get("name"), "ok": bool(data.get("ok")), "result": res[:2000]})
            rec["turns"].append(cur["chars"])
            cur = {"chars": 0}
        elif event == "done":
            rec["done"] = {k: data.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
            rec["turns"].append(cur["chars"])
            break
    # a delta_reset NOT followed by a tool call is the server discarding a turn to nudge the model again
    for i, ev in enumerate(events):
        if ev != "delta_reset":
            continue
        nxt = next((e for e in events[i + 1:] if e in ("tool_call", "delta", "done", "thought_delta")), "done")
        if nxt != "tool_call":
            rec["nudges"] += 1
    rec["turn_tokens_est"] = [round(c / CHARS_PER_TOKEN) for c in rec["turns"] if c]
    return rec


def downloads_in(text: str) -> list:
    return _DL_RX.findall(text or "")


def is_card(rec: dict) -> bool:
    """True when the reply is a questionnaire card (the UI shows it as choices) and no tool ran."""
    return "questionnaire" in (rec.get("text") or "") and not rec.get("tools")


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """95% Wilson score interval for k successes in n runs: single runs prove nothing, intervals show how much a rate can be trusted."""
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def reply_excerpt(text: str, limit: int = 200) -> str:
    """First characters of a reply for the failure log: card numbers masked, newlines flattened, length capped. Stays in the local bench folder."""
    from core.pdf_tools.common import mask
    return mask(" ".join((text or "").split()))[:limit]


def classify_run(fmt: str, rec: dict, findings: list, error: str = "", card_pending: bool = False) -> str:
    """One fixed outcome code per run (route_log vocabulary friendly: short, no text)."""
    if error:
        return "timeout" if "Timeout" in error else "error"
    if rec.get("done") is None:
        return "error"
    if card_pending:
        return "card_unanswered"          # the chat is still waiting for answers the harness did not give: not a model failure
    names = [t["name"] for t in rec["tools"]]
    failed = [r for r in rec["results"] if not r["ok"]]
    if fmt == "pdf":
        built = "doc_create" in names and any(t["name"] == "doc_append" and t["done_flag"] for t in rec["tools"])
    else:
        built = any(t["name"] in ("write_file", "doc_create") and t["file"].lower().endswith(".docx") for t in rec["tools"]) \
            or any(f.lower().endswith(".docx") for f in downloads_in(rec["text"]))
    if not built:
        if names:
            return "max_steps"
        return "asked_question" if (rec.get("text") or "").rstrip().endswith("?") else "no_progress"
    if _NOT_GENERATED in rec["text"]:
        return "no_progress"
    if findings:
        return "ok_findings"
    if rec["nudges"]:
        return "ok_nudged"
    return "ok_tool_errors" if failed else "ok"


# ------------------------------------------------------------------ live run

def _post_chat(base, client, messages, timeout_s):
    import httpx
    payload = {"messages": messages, "web_search": False, "kb": "off", "verify": "off", "temperature": 0.3}
    with client.stream("POST", f"{base}/chat/run", json=payload, timeout=httpx.Timeout(timeout_s)) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.read().decode('utf-8', 'replace')[:200]}")
        return fold_chat_sse(resp.iter_lines())


def _card_answers(text: str, answers: dict) -> str:
    """The user's reply to our questionnaire card, built the way the UI does ('<question>: <choice>' lines)."""
    asked = re.findall(r'"q"\s*:\s*"([^"]+)"', text or "")
    lines = [f"{q}: {answers[q]}" for q in asked if q in answers]
    return "\n".join(lines) or "\n".join(f"{q}: {a}" for q, a in answers.items())


def run_prompt(base, client, fmt, name, spec, timeout_s):
    from core import route_log
    from file_bench_checks import check_docx, check_pdf
    rec_out = {"format": fmt, "prompt": name, "ts": round(time.time()), "error": "", "findings": [], "inloop_findings": 0, "rounds": 1}
    card_pending = False
    msgs = [{"role": "user", "content": spec["prompt"]}]
    t0 = time.time()
    try:
        rec = _post_chat(base, client, msgs, timeout_s)
        answers = spec.get("answers") or DEFAULT_ANSWERS
        for rnd in range(MAX_CARD_ROUNDS):     # a questionnaire card is the answer to the first request: reply like the UI does
            if rnd == 0:
                rec_out["card_turn"] = "card" if is_card(rec) else (
                    "card_saved_as_file" if any(t["name"] == "write_file" for t in rec["tools"]) else "no_card")
                rec_out["card_turn_tokens"] = (rec["done"] or {}).get("completion_tokens")
            card = rec["text"]
            if not is_card(rec):
                if not (rnd == 0 and fmt == "pdf"):
                    break
                from core import prompt_scope      # a PDF card that never reached the user: use the server's own copy so the build phase is still measured
                card = prompt_scope.brief_card("pdf")
            msgs += [{"role": "assistant", "content": card},
                     {"role": "user", "content": _card_answers(card, answers)}]
            rec = _post_chat(base, client, msgs, timeout_s)
            rec_out["rounds"] = rnd + 2
        card_pending = is_card(rec)
    except Exception as e:                     # noqa: BLE001 - transport / timeout: reported, never fatal to the batch
        rec_out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        rec = {"tools": [], "results": [], "turns": [], "nudges": 0, "text": "", "done": None, "turn_tokens_est": [], "md": ""}
    rec_out["duration_s"] = round(time.time() - t0, 1)
    ext = ".docx" if fmt == "docx" else ".pdf"
    links = [f for f in downloads_in(rec["text"]) if f.lower().endswith(ext)]
    data = None
    if links:
        try:
            r = client.get(f"{base}/download", params={"path": links[-1]}, timeout=60)
            data = r.content if r.status_code == 200 else None
        except Exception:                      # noqa: BLE001
            data = None
    if links and data is None:
        rec_out["findings"].append("download link did not resolve")
    if data:
        md = rec.get("md", "")
        rec_out["findings"] += (check_docx(data, md, spec.get("min_words", 0)) if fmt == "docx"
                                else check_pdf(data, md, spec.get("min_pages", 1)))
        rec_out["bytes"] = len(data)
    elif not rec_out["error"]:
        rec_out["findings"].append("no file delivered")
    for r in rec["results"]:
        m = _PDF_CHECK_RX.search(r["result"])
        if m:
            rec_out["inloop_findings"] += int(m.group(1))
    names = [t["name"] for t in rec["tools"]]
    rec_out.update({
        "tool_calls": len(names), "tool_names": names,
        "tool_ok": sum(1 for r in rec["results"] if r["ok"]), "tool_total": len(rec["results"]),
        "tool_errs": sorted({route_log.classify_tool_result(r["result"]) or "error" for r in rec["results"] if not r["ok"]}),
        "appends": names.count("doc_append"), "nudges": rec["nudges"],
        "prompt_tokens": (rec["done"] or {}).get("prompt_tokens"),
        "completion_tokens": (rec["done"] or {}).get("completion_tokens"),
        "turn_tokens_est": rec["turn_tokens_est"],
        "max_turn_tokens_est": max(rec["turn_tokens_est"] or [0]),
        "turns_over_cap": sum(1 for x in rec["turn_tokens_est"] if x >= TURN_TOKEN_CAP * 0.9),
    })
    rec_out["outcome"] = classify_run(fmt, rec, rec_out["findings"] + (["in-loop"] if rec_out["inloop_findings"] else []),
                                      rec_out["error"], card_pending)
    if not rec_out["outcome"].startswith("ok"):
        rec_out["reply_excerpt"] = reply_excerpt(rec.get("text"))      # why it failed, 200 chars, masked, local file only
    return rec_out


def log_route(row, label):
    """One route_log run per benchmark prompt, category bench_<format>, so tuner views can filter it out."""
    try:
        from core import route_log
        rid = f"bench-{label}-{uuid.uuid4().hex[:8]}"
        route_log.run_start(rid, None, "bench", f"bench_{row['format']}")
        for i, n in enumerate(row["tool_names"]):
            route_log.event(rid, i, f"bench_{row['format']}", "main", label, tool_name=n,
                            tool_ok=None, model=label)
        route_log.run_end(rid, row["tool_calls"], row["outcome"], label)
    except Exception as e:                     # noqa: BLE001
        print(f"[bench] route_log write skipped: {type(e).__name__}", file=sys.stderr)


def cmd_run(a):
    from tests import eval_agent
    user, pw = os.environ.get("BENCH_USER", ""), os.environ.get("BENCH_PASSWORD", "")
    if not user or not pw:
        sys.exit("set BENCH_USER and BENCH_PASSWORD (a dedicated non-MFA account) in the environment")
    from file_bench_prompts import DOCX_PROMPTS, PDF_PROMPTS
    sets = {"docx": DOCX_PROMPTS, "pdf": PDF_PROMPTS}
    client = eval_agent._live_login(a.base, user, pw)
    out = Path(a.out) / a.label
    out.mkdir(parents=True, exist_ok=True)
    env = eval_agent._live_env(a.base, client, "chat", 0.3)
    env.update({"label": a.label, "ctx": a.ctx})
    (out / "env.json").write_text(json.dumps(env, indent=1), encoding="utf-8")
    fmts = [f for f in a.formats.split(",") if f in sets]
    with open(out / "results.jsonl", "a", encoding="utf-8") as fh:
        for rep in range(a.repeat):
            for fmt in fmts:
                for name, spec in list(sets[fmt].items())[: a.limit or None]:
                    print(f"[{a.label}] {fmt}/{name} #{rep + 1} ...", flush=True)
                    row = run_prompt(a.base, client, fmt, name, spec, a.timeout)
                    row["label"], row["ctx"], row["rep"] = a.label, a.ctx, rep + 1
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
                    if not a.no_route_log:
                        log_route(row, a.label)
                    print(f"    -> {row['outcome']} {row['duration_s']}s tools={row['tool_calls']} nudges={row['nudges']} "
                          f"findings={len(row['findings'])}+{row['inloop_findings']}", flush=True)
    client.close()
    print(report([out]))


# ------------------------------------------------------------------ report

def outcome_counts(rows) -> str:
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))


def report(dirs) -> str:
    rows = []
    for d in dirs:
        p = Path(d) / "results.jsonl"
        if p.is_file():
            rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines = []
    for fmt in ("docx", "pdf"):
        sub = [r for r in rows if r["format"] == fmt]
        if not sub:
            continue
        lines += [f"### {fmt.upper()}", "",
                  "| ctx | prompts | tool-call ok | built | clean (no findings) | no nudge | max turn tok (est) | turns near 8k cap | "
                  "avg prompt tok | avg completion tok | avg s | built 95% interval | outcomes |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for label in sorted({r["label"] for r in sub}):
            g = [r for r in sub if r["label"] == label]
            n = len(g)
            tc_ok, tc_all = sum(r["tool_ok"] for r in g), sum(r["tool_total"] for r in g)
            built = sum(1 for r in g if r["outcome"].startswith("ok"))
            clean = sum(1 for r in g if r["outcome"] in ("ok", "ok_nudged", "ok_tool_errors"))
            nonudge = sum(1 for r in g if r["nudges"] == 0)
            avg = lambda k: round(sum((r.get(k) or 0) for r in g) / n)      # noqa: E731
            lines.append(f"| {g[0]['ctx']} | {n} | {tc_ok}/{tc_all} | {built}/{n} | {clean}/{n} | {nonudge}/{n} | "
                         f"{max(r['max_turn_tokens_est'] for r in g)} | {sum(r['turns_over_cap'] for r in g)} | "
                         f"{avg('prompt_tokens')} | {avg('completion_tokens')} | {round(sum(r['duration_s'] for r in g) / n)} | "
                         f"{wilson(built, n)[0]:.0%}-{wilson(built, n)[1]:.0%} | {outcome_counts(g)} |")
        lines += ["", "| prompt | ctx | outcome | tools | nudges | findings |", "|---|---|---|---|---|---|"]
        for r in sub:
            lines.append(f"| {r['prompt']} | {r['ctx']} | {r['outcome']} | {r['tool_calls']} | {r['nudges']} | "
                         f"{'; '.join(r['findings']) or '-'}{' +' + str(r['inloop_findings']) + ' in-loop' if r['inloop_findings'] else ''} |")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--base", default="http://127.0.0.1:8000")
    r.add_argument("--label", required=True, help="e.g. ctx32k")
    r.add_argument("--ctx", type=int, required=True, help="context the server was started with (recorded, not applied)")
    r.add_argument("--formats", default="docx,pdf")
    r.add_argument("--limit", type=int, default=0, help="first N prompts per format (0 = all)")
    r.add_argument("--repeat", type=int, default=3, help="runs per prompt (rates need n >= 10 per format to mean anything)")
    r.add_argument("--timeout", type=float, default=1800)
    r.add_argument("--out", default="bench_out")
    r.add_argument("--no-route-log", action="store_true")
    p = sub.add_parser("report")
    p.add_argument("dirs", nargs="+")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        cmd_run(a)
    else:
        print(report(a.dirs))


if __name__ == "__main__":
    main()
