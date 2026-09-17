"""
tests/eval_agent.py - Quality eval harness for the A770 agent runtime.

Two modes:

  offline (default)  Pure-function checks: GBNF grammar build, message
                     compaction, loop detection, JSON repair, needle router.
                     No server or GPU needed - run anywhere.
  --live             Runs scripted agent tasks against a running manager
                     (default http://127.0.0.1:8000) via POST /agent/run SSE
                     and scores: expected tools called, forbidden tools,
                     escalation seen, final answer non-empty.

Usage:
  python tests/eval_agent.py                 # offline suite
  python tests/eval_agent.py --live          # live tasks (mode=auto lane)
  python tests/eval_agent.py --live --mode main
  python tests/eval_agent.py --live --base http://127.0.0.1:8000

Results are printed as a scorecard and saved to tests/eval_results.json.
"""

import argparse
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

RESULTS_FILE = Path(__file__).resolve().parent / "eval_results.json"


# ---------------- offline checks ----------------

def check_grammar() -> tuple:
    from core.grammar import build_tool_call_grammar, envelope_examples
    from core.agent_tools import AGENT_TOOLS
    g = build_tool_call_grammar(AGENT_TOOLS)
    assert g, "grammar build returned None (envelope verification failed?)"
    assert "root ::= chunk" in g and "tco ::=" in g, "grammar missing core rules"
    names = [t["function"]["name"] for t in AGENT_TOOLS]
    for n in names:
        assert f'"{n}"' in g, f"tool {n} missing from grammar alternation"
    assert "\\u" not in g.split("tname")[0], "stray unicode escapes in grammar head"
    ex = envelope_examples()
    assert ex and "<tool_call>" in ex, "few-shot examples missing envelope"
    # prefix-exclusion: plain text branch must forbid starting the open tag
    assert '"<" [^t]' in g, "prefix-exclusion alternation incomplete"
    return True, f"grammar ok ({len(names)} tools, {len(g.splitlines())} rules, {len(ex)}-char few-shot)"


def check_compaction() -> tuple:
    from core.agent_loop import compact_messages, estimate_prompt_tokens
    msgs = [{"role": "system", "content": "SYS"}]
    for i in range(30):
        msgs.append({"role": "user", "content": f"step {i}: " + "x" * 1500})
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "f.py"}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 1800})
    msgs.append({"role": "user", "content": "final question?"})
    est = estimate_prompt_tokens(msgs)
    assert est > 20000, f"synthetic history too small: {est}"
    out = compact_messages(msgs, 8000)
    assert out[0]["content"] == "SYS", "system prompt lost"
    assert out[1]["role"] == "system" and "DIGEST" in out[1]["content"], "digest missing"
    assert out[-1]["content"] == "final question?", "last user message lost"
    # pairing invariant in the kept tail: no tool msg directly after system/user
    for prev, cur in zip(out[:-1], out[1:]):
        if cur.get("role") == "tool":
            assert prev.get("role") == "assistant", "tool message orphaned after compaction"
    assert estimate_prompt_tokens(out) < est, "compaction did not shrink history"
    short = compact_messages(msgs[:4], 8000)
    assert short == msgs[:4], "short history must pass through unchanged"
    return True, f"compaction ok ({est} -> {estimate_prompt_tokens(out)} tokens)"


def check_loop_detection() -> tuple:
    from core.agent_loop import is_degeneration_or_loop
    loop1 = "I will write the code now.\n" * 3
    is_l, _ = is_degeneration_or_loop(loop1)
    assert is_l, "identical-line loop not detected"
    loop2 = "The process has finished. " + ("Here is the summary of the work done. " * 5)
    is_l2, _ = is_degeneration_or_loop(loop2)
    assert is_l2, "repeating-suffix loop not detected"
    normal = "Here is the code:\n```python\nprint(42)\n```\nIt outputs 42."
    is_l3, _ = is_degeneration_or_loop(normal)
    assert not is_l3, "normal code flagged as loop"
    return True, "loop detection ok"


def check_repair() -> tuple:
    from core.agent_loop import safe_parse_and_repair_args
    d = safe_parse_and_repair_args('{"name": "read_file", "path": "a.py"')
    assert isinstance(d, dict) and d.get("name") == "read_file", "truncated JSON not repaired"
    assert d.get("path") == "a.py", "path lost in repair"
    d2 = safe_parse_and_repair_args('{"name": "read_file", "arguments": {"path": "a.py"')
    assert isinstance(d2, dict) and d2.get("path") == "a.py", "regex path recovery broken"
    d3 = safe_parse_and_repair_args({"name": "list_files", "arguments": {}})
    assert d3.get("name") == "list_files", "dict passthrough broken"
    d4 = safe_parse_and_repair_args('{"name": "write_file", "arguments": {"path": "x.py", "content": "abc"}}')
    assert d4.get("arguments", {}).get("path") == "x.py", "args parse broken"
    return True, "JSON repair ok"


def check_needle() -> tuple:
    from core.small_model import needle_available, needle_route
    from core.agent_tools import AGENT_TOOLS
    if not needle_available():
        return None, "needle not installed - skipped"
    nr = needle_route("list all files in directory", AGENT_TOOLS)
    assert nr is None or isinstance(nr, dict), "needle_route returned garbage"
    return True, f"needle ok (route: {nr['name'] if nr else 'no confident match'})"


OFFLINE_CHECKS = [
    ("grammar", check_grammar),
    ("compaction", check_compaction),
    ("loop_detection", check_loop_detection),
    ("json_repair", check_repair),
    ("needle_router", check_needle),
]


# ---------------- live agent tasks ----------------

LIVE_TASKS = [
    {"name": "direct_tool_list", "prompt": "List the files in the workspace root.",
     "expect_tools": ["list_files"], "soft": False},
    {"name": "write_and_verify", "prompt": "Create a file eval_tmp/hello.py that contains print('hello eval'), then run it with run_python to prove it works.",
     "expect_tools": ["write_file", "run_python"], "soft": False},
    {"name": "compute_only", "prompt": "Use run_python to compute 137*29 and reply with just the number.",
     "expect_tools": ["run_python"], "forbid_tools": ["write_file"], "soft": False},
    {"name": "memory_search", "prompt": "Search your memory for anything related to llama or vulkan and summarize what you find.",
     "expect_tools": ["search_memory"], "soft": True},
    {"name": "escalation_task", "prompt": "Refactor every python file in the workspace to use async I/O end-to-end and write a migration report to eval_tmp/MIGRATION.md.",
     "expect_escalation": True, "soft": True},
]


def run_live_task(base: str, task: dict, mode: str, timeout_s: float = 600) -> dict:
    import httpx
    rec = {"name": task["name"], "tools": [], "lanes": [], "escalated": False,
           "final_len": 0, "ok": False, "soft": task.get("soft", False), "error": None,
           "duration_s": None}
    payload = {"messages": [{"role": "user", "content": task["prompt"]}],
               "mode": mode, "max_steps": 12, "temperature": 0.3}
    t0 = time.time()
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
            with client.stream("POST", f"{base}/agent/run", json=payload) as resp:
                if resp.status_code != 200:
                    rec["error"] = f"HTTP {resp.status_code}"
                    return rec
                event = None
                for line in resp.iter_lines():
                    line = line.strip()
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: ") and event:
                        try:
                            data = json.loads(line[6:])
                        except Exception:
                            continue
                        if event == "lane":
                            rec["lanes"].append(data.get("lane"))
                        elif event == "tool_call":
                            rec["tools"].append(data.get("name"))
                        elif event == "delta":
                            rec["final_len"] += len(data.get("text") or "")
                        elif event == "done":
                            break
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["duration_s"] = round(time.time() - t0, 1)
        return rec
    rec["duration_s"] = round(time.time() - t0, 1)
    lanes = rec["lanes"]
    rec["escalated"] = ("executor" in lanes and "main" in lanes
                        and (lanes.index("main") > lanes.index("executor")
                             or lanes[-1] == "main"))
    problems = []
    for want in task.get("expect_tools", []):
        if want not in rec["tools"]:
            problems.append(f"missing tool: {want}")
    for ban in task.get("forbid_tools", []):
        if ban in rec["tools"]:
            problems.append(f"forbidden tool used: {ban}")
    if task.get("expect_escalation") and not rec["escalated"]:
        problems.append("expected escalation to main lane did not happen")
    if not rec["tools"] and rec["final_len"] < 10:
        problems.append("no tools and no answer")
    rec["problems"] = problems
    rec["ok"] = not problems
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="A770 agent eval harness")
    ap.add_argument("--live", action="store_true", help="run live agent tasks")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="manager base URL")
    ap.add_argument("--mode", default="auto", choices=["auto", "main"], help="agent lane mode")
    args = ap.parse_args()

    results = {"ts": time.time(), "offline": [], "live": []}
    failed = 0

    if not args.live:
        print("=" * 60)
        print(" OFFLINE SUITE (pure functions, no server needed)")
        print("=" * 60)
        for name, fn in OFFLINE_CHECKS:
            try:
                ok, note = fn()
                status = "PASS" if ok else "SKIP"
                if ok is False:
                    failed += 1
                print(f"  [{status}] {name:16s} {note}")
                results["offline"].append({"name": name, "status": status, "note": note})
            except AssertionError as e:
                failed += 1
                print(f"  [FAIL] {name:16s} {e}")
                results["offline"].append({"name": name, "status": "FAIL", "note": str(e)})
            except Exception as e:
                failed += 1
                print(f"  [FAIL] {name:16s} {type(e).__name__}: {e}")
                results["offline"].append({"name": name, "status": "FAIL", "note": f"{type(e).__name__}: {e}"})

    if args.live:
        print("=" * 60)
        print(f" LIVE SUITE (base={args.base}, mode={args.mode})")
        print("=" * 60)
        for task in LIVE_TASKS:
            rec = run_live_task(args.base, task, args.mode)
            status = "PASS" if rec["ok"] else ("SOFT-FAIL" if rec.get("soft") else "FAIL")
            if not rec["ok"] and not rec.get("soft"):
                failed += 1
            extra = ""
            if rec["error"]:
                extra = f" err={rec['error']}"
            elif rec.get("problems"):
                extra = " problems=" + ";".join(rec["problems"])
            print(f"  [{status}] {rec['name']:18s} tools={rec['tools'] or '-'} "
                  f"lanes={','.join(rec['lanes']) or '-'} {rec['duration_s']}s{extra}")
            results["live"].append(rec)

    RESULTS_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults saved -> {RESULTS_FILE}")
    print("SUITE:", "FAILED" if failed else "ALL OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
