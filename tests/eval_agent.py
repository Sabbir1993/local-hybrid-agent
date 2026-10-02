"""
tests/eval_agent.py - Quality eval harness for the A770 agent runtime.

Three modes:

  offline (default)  Pure-function checks: GBNF grammar build, message
                     compaction, loop detection, JSON repair, needle router.
                     No server or GPU needed - run anywhere.
  --mock             Scripted-model agent runs: the REAL /agent/run loop with a
                     deterministic fake LLM at the httpx transport level
                     (tests/eval_mock.py). No server, GPU or network needed.
                     Repeats measure loop determinism, not model variance.
  --live             Runs scripted agent tasks against a running manager
                     (default http://127.0.0.1:8000) via POST /agent/run SSE
                     and scores: expected tools called, forbidden tools,
                     escalation seen, final answer non-empty.

Usage:
  python tests/eval_agent.py                 # offline suite
  python tests/eval_agent.py --mock          # mock suite (N=3 repeats)
  python tests/eval_agent.py --mock --repeats 1 --tasks smoke_list_files,compute_only
  python tests/eval_agent.py --mock --regression     # fail if worse than baseline
  python tests/eval_agent.py --mock --update-baseline  # record a new baseline (review the diff!)
  python tests/eval_agent.py --live          # live tasks (mode=auto lane)
  python tests/eval_agent.py --live --mode main
  python tests/eval_agent.py --live --base http://127.0.0.1:8000
  python tests/eval_agent.py --live --live-user eval --live-password '...'  # sign in first
                                                             # (eval account: dedicated project on a
                                                             #  throwaway workspace, never MFA)

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
    # The digest is folded into the first user turn rather than inserted as a new
    # system message at index 1, so the system-prompt + tool-schema prefix stays
    # byte-identical and llama.cpp can reuse that part of the KV cache
    # (core/agent_loop.py::compact_messages).
    assert any("DIGEST" in str(m.get("content") or "") for m in out[1:3]), "digest missing"
    assert not any(m.get("role") == "system" for m in out[1:3]), \
        "compaction must not insert a synthetic system message (it breaks the KV prefix)"
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
    from core.small_model import router_available, router_route, router_engine_name
    from core.agent_tools import AGENT_TOOLS
    eng = router_engine_name()
    if not router_available():
        return None, f"router ({eng}) not installed or unavailable - skipped"
    nr = router_route("list all files in directory", AGENT_TOOLS)
    assert nr is None or isinstance(nr, dict), "router_route returned garbage"
    return True, f"router ({eng}) ok (route: {nr['name'] if nr else 'no confident match'})"


def check_compact_endpoint() -> tuple:
    """POST /chat/compact via TestClient: project gate in agent mode, short-history
    refusal, and a mocked-summarizer run that rewrites the DB session on disk."""
    from fastapi import FastAPI
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")          # httpx2 deprecation notice
        from starlette.testclient import TestClient
    from routes import chat as chat_routes
    from core import db, deps

    app = FastAPI()

    def _eval_principal():
        # The endpoint enforces auth (Depends(get_current_user)), so an
        # unauthenticated TestClient call is refused with 401 at dependency
        # resolution and this check never reaches the project gate it exists to
        # make - it stood red in every offline run. Inject a principal so the
        # check tests the project gate, the same shape the rest of the suite uses
        # (tests/test_agent_limits_routes.py:30). Auth itself stays covered:
        # tests/test_eval_compact_auth.py pins that the same route still answers
        # 401 without this injection.
        from core.auth import Principal
        return Principal(id=1, username="eval", display_name="eval", is_super_admin=False,
                         must_change_password=False, role_names=["user"],
                         permission_keys={"chat.use"})

    app.dependency_overrides[deps.get_current_user] = _eval_principal
    app.include_router(chat_routes.router)
    client = TestClient(app)

    # 1) agent mode without an active project must be refused (project gate)
    r = client.post("/chat/compact", json={
        "messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
        "agent_mode": True})
    assert r.status_code == 400, f"agent mode without project must 400, got {r.status_code}"
    assert "project" in r.json().get("error", "").lower(), "gate error message unclear"

    # 2) too-short history is refused
    r = client.post("/chat/compact", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400, "single message must not compact"

    # This check writes to the real projects DB, so it owns its cleanup. The
    # long-standing 401 masked three defects in this block (db_create_project
    # needs owner_user_id AND an absolute workspace_dir, db_create_session needs
    # the owner, db_delete_project needs the owner) - each failure left rows and
    # a temp folder behind, because creation happened before the try. Creation
    # now happens inside the try, and a leftover project from an older run is
    # cleared first so the check is re-runnable.
    import shutil
    import tempfile
    for stale in [p for p in db.db_list_projects(1) if p["name"] == "__eval_compact__"]:
        try:
            db.db_delete_project(stale["id"], 1)
        except Exception as e:                      # a locked row must not block the check
            print(f"[eval] stale project {stale['id']} not removed: {e}", file=sys.stderr)
    ws_dir = tempfile.mkdtemp(prefix="eval_compact_ws_")
    proj = sid = None
    # Patch the DEFINING module, not the package: routes/chat/__init__.py:48
    # re-exports _summarize_history, so patching `chat_routes._summarize_history`
    # set an unused package attribute while compact.py:132 kept calling the real
    # one - which then blocked on a model server that is not running, and the
    # check hung instead of failing.
    from routes.chat import compact as compact_mod
    orig = compact_mod._summarize_history
    # input_guard.check_async runs semantic rules through the local classifier,
    # which auto-loads the executor model. This check is part of the OFFLINE
    # suite ("pure functions, no server needed") and the compact route's guard
    # behaviour is not what it tests, so the guard is stubbed here - without it a
    # fixed auth path makes CI reach for a model it does not have.
    from core import input_guard
    orig_guard = input_guard.check_async

    async def _no_guard(texts, user, any_cloud_lane):
        return None

    input_guard.check_async = _no_guard
    seen = []

    async def fake_summary(convo, instructions, use_executor, user_id=None):
        assert convo and convo[0]["role"] == "user", "conversation not passed to summarizer"
        seen.append(instructions)
        return "## 1. Primary Requests\nbuild the thing\n\n## 5. Next Steps\ncontinue"

    compact_mod._summarize_history = fake_summary
    try:
        proj = db.db_create_project("__eval_compact__", workspace_dir=ws_dir, owner_user_id=1)
        sid = db.db_create_session(proj["id"], "eval compact", owner_user_id=1)["id"]
        db.db_append_message(sid, "user", "please build the thing", owner_user_id=1)
        db.db_append_message(sid, "assistant", "ok " + "y" * 6000, {"ntok": 1500}, owner_user_id=1)
        db.db_append_message(sid, "user", "now the schema", owner_user_id=1)
        db.db_append_message(sid, "assistant", "here " + "z" * 3000, {"ntok": 800}, owner_user_id=1)
        r = client.post("/chat/compact", json={
            "session_id": sid, "instructions": "focus on the schema",
            "agent_mode": True, "project_id": proj["id"], "keep_last": 2})
        assert r.status_code == 200, f"compact failed: {r.status_code} {r.text[:200]}"
        j = r.json()
        assert j["summary"].startswith("## 1."), "summary not returned"
        assert j["after_tokens"] < j["before_tokens"], "compaction did not shrink context"
        assert j["reduction_pct"] > 0, "reduction not reported"
        # The endpoint is append-only (routes/chat/compact.py:154-169): it returns
        # a compact marker to insert and writes NOTHING to the transcript. This
        # check used to assert an archive_path, a rewritten `messages` list and a
        # replaced DB history - a design that no longer exists. Those assertions
        # never ran: the 401 above hid all of it.
        marker = j["compact_message"]
        assert marker["role"] == "system" and "[COMPACTED CONTEXT SUMMARY]" in marker["content"]
        assert marker["meta"]["compact"] is True and marker["meta"]["before_tokens"] == j["before_tokens"]
        assert marker["meta"]["after_tokens"] == j["after_tokens"]
        stored = db.db_load_messages(sid, owner_user_id=1)
        assert len(stored) == 5, f"append-only: 4 original + 1 marker expected, got {len(stored)}"
        assert stored[0]["role"] == "user", "original history must survive compaction"
        assert stored[-1]["role"] == "system", "compact marker must be the last row"
        assert stored[-1]["meta"].get("compact") is True, "marker meta not persisted"
        assert stored[3]["meta"] and stored[3]["meta"].get("ntok") == 800, \
            "pre-existing message meta lost"

        # 3) message-fallback path (no session) -- still compacts, writes nothing
        r2 = client.post("/chat/compact", json={"messages": [
            {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]})
        assert r2.status_code == 200, "message-fallback compaction failed"
        j2 = r2.json()
        assert j2["compact_message"]["role"] == "system", "fallback must still return a marker"
        assert len(db.db_load_messages(sid, owner_user_id=1)) == 5, \
            "a request without session_id must not touch any transcript"
        assert seen[0] == "focus on the schema", "extra instructions dropped"
        assert len(seen) == 2, f"summarizer called {len(seen)}x, expected 2"
    finally:
        compact_mod._summarize_history = orig
        input_guard.check_async = orig_guard
        if sid is not None:
            db.db_delete_session(sid, owner_user_id=1)
        if proj is not None:
            db.db_delete_project(proj["id"], 1)
        shutil.rmtree(ws_dir, ignore_errors=True)
    return True, f"compact endpoint ok ({j['before_tokens']} -> {j['after_tokens']} tokens, append-only)"


def check_escalation_policy() -> tuple:
    """A good plain executor answer must NOT be escalated to main (regression:
    routes/agent.py used the (bool, text) tuple from is_degeneration_or_loop as a
    bool, so every executor step was re-run on main)."""
    from core.agent_loop import is_degeneration_or_loop
    from core.router_policy import escalate_reason, classify_query, DEFAULTS
    cfg = dict(DEFAULTS)
    answer = "The config loader merges roles.json first, then app.json blocks on top."
    is_l, _ = is_degeneration_or_loop(answer)
    assert escalate_reason(step=0, content=answer, tool_calls=[], query="how does config loading work?",
                           is_loop=is_l, cfg=cfg) == "", "plain executor answer was escalated"
    assert escalate_reason(step=0, content="", tool_calls=[], query="create a page", is_loop=False,
                           cfg=cfg) == "creation_no_tool", "creation without a tool call not escalated"
    assert escalate_reason(step=0, content="```bash\nls\n```", tool_calls=[], query="run the tests",
                           is_loop=False, cfg=cfg) == "tutorial_code", "tutorial code not escalated"
    assert escalate_reason(step=2, content="ok", tool_calls=[{"x": 1}], query="create x", is_loop=False,
                           cfg=cfg) == "", "later step with tool calls escalated"
    assert escalate_reason(step=3, content="x", tool_calls=[], query="q", is_loop=True, cfg=cfg) == "loop"
    cases = {"hi": "greeting", "create a login page": "creation", "run the tests": "action",
             "why is login slow?": "question", "the payment flow": "other"}
    for q, want in cases.items():
        got = classify_query(q, cfg)
        assert got == want, f"classify_query({q!r}) = {got}, want {want}"
    return True, "escalation policy ok"


OFFLINE_CHECKS = [
    ("grammar", check_grammar),
    ("compaction", check_compaction),
    ("loop_detection", check_loop_detection),
    ("escalation_policy", check_escalation_policy),
    ("json_repair", check_repair),
    ("needle_router", check_needle),
    ("compact_endpoint", check_compact_endpoint),
]


# ---------------- live agent tasks ----------------

LIVE_TASKS = [
    {"name": "direct_tool_list", "prompt": "List the files in the workspace root.",
     "expect_tools": ["list_files"], "soft": False},
    {"name": "write_and_verify", "prompt": "Create a file eval_tmp/hello.py that contains print('hello eval'), then run it with run_python to prove it works.",
     "expect_tools": ["write_file", "run_python"], "soft": False},
    {"name": "compute_only", "prompt": "Use run_python to compute 137*29 and reply with just the number.",
     "expect_tools": ["run_python"], "forbid_tools": ["write_file"],
     "expect_final_contains": ["3973"], "soft": False},
    {"name": "memory_search", "prompt": "Search your memory for anything related to llama or vulkan and summarize what you find.",
     "expect_tools": ["search_memory"], "soft": True},
    {"name": "escalation_task", "prompt": "In eval_tmp/pkg there are no files yet. First create a.py, b.py and c.py there, each with one blocking requests.get call. Then convert all three to async httpx end-to-end, and write eval_tmp/MIGRATION.md describing the change.",
     "expect_escalation": True, "soft": True},
]


def _live_login(base: str, username: str, password: str, timeout_s: float = 60):
    """Session login for live runs (mirrors a real user; API tokens are fenced
    off admin perms). The eval account must NOT have MFA: a benchmark runner
    cannot answer a TOTP prompt, so it fails fast instead of hanging."""
    import httpx
    client = httpx.Client(timeout=httpx.Timeout(timeout_s))
    r = client.post(f"{base}/auth/login", json={"username": username, "password": password})
    data = r.json() if r.status_code == 200 else {}
    if r.status_code != 200 or "user" not in data:
        if data.get("mfa_required"):
            raise SystemExit("live eval user must not have MFA enabled (runner cannot answer TOTP)")
        raise SystemExit(f"live login failed: HTTP {r.status_code} {str(data)[:150]}")
    return client


def _live_env(base: str, client, mode: str, temperature) -> dict:
    """Best-effort environment record so runs weeks apart stay comparable."""
    env = {"mode": mode, "temperature": temperature, "ts": round(time.time())}
    try:
        import subprocess
        env["git_sha"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                        capture_output=True, text=True, timeout=15,
                                        cwd=str(BASE_DIR)).stdout.strip() or None
    except Exception:
        env["git_sha"] = None
    try:
        st = client.get(f"{base}/control/status", timeout=15).json() if client else {}
        for k in ("profile", "model", "model_path", "loaded", "degraded", "lane"):
            if isinstance(st, dict) and st.get(k) is not None:
                env[k] = st[k]
    except Exception:
        pass
    return env


def run_live_task(base: str, task: dict, mode: str, timeout_s: float = 600, client=None) -> dict:
    import httpx
    rec = {"name": task["name"], "tools": [], "lanes": [], "escalated": False,
           "final_len": 0, "final_text": "", "ok": False, "soft": task.get("soft", False),
           "error": None, "duration_s": None}
    payload = {"messages": [{"role": "user", "content": task["prompt"]}],
               "mode": mode, "max_steps": 12, "temperature": 0.3}
    t0 = time.time()
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=httpx.Timeout(timeout_s))
    try:
        with client.stream("POST", f"{base}/agent/run", json=payload) as resp:
            if resp.status_code == 401:
                rec["error"] = "HTTP 401 - sign in first (--live-user/--live-password)"
                return rec
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
                        rec["final_text"] += data.get("text") or ""
                    elif event == "done":
                        break
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["duration_s"] = round(time.time() - t0, 1)
        return rec
    finally:
        if own_client:
            client.close()
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
    # Answer grading, not just tool plumbing: compute_only passes only if the model
    # actually replied 3973, not for calling run_python and answering a haiku.
    final_text = rec.get("final_text") or ""
    for want in task.get("expect_final_contains", []):
        if want.lower() not in final_text.lower():
            problems.append(f"final answer missing {want!r} (got {final_text[:120]!r})")
    rec["problems"] = problems
    rec["ok"] = not problems
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="A770 agent eval harness")
    ap.add_argument("--live", action="store_true", help="run live agent tasks")
    ap.add_argument("--mock", action="store_true",
                    help="run the hermetic mock suite (scripted model, no server/GPU/network)")
    ap.add_argument("--repeats", type=int, default=3,
                    help="mock repeats per task (fresh env each; default 3)")
    ap.add_argument("--tasks", default=None,
                    help="comma-separated mock task names (default: all)")
    ap.add_argument("--no-retrieval", action="store_true", help="skip the retrieval slice")
    ap.add_argument("--no-code", action="store_true", help="skip the code-intel slice")
    ap.add_argument("--embedder", default="fake", choices=["fake", "real"],
                    help="retrieval slice vectors: fake BoW stand-in (hermetic, default) "
                         "or cached nomic vectors (tests/.eval_vec_cache.json, report-only "
                         "hybrid_real_* keys, GPU-free reruns)")
    ap.add_argument("--no-router", action="store_true", help="skip the router slice")
    ap.add_argument("--no-offline", action="store_true",
                    help="skip the offline pure-function suite (stages CI: unit vs loop)")
    ap.add_argument("--regression", action="store_true",
                    help="compare mock results against tests/eval_baseline.json, exit 1 on drop")
    ap.add_argument("--update-baseline", action="store_true",
                    help="write current mock results to tests/eval_baseline.json (review the diff!)")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="manager base URL")
    ap.add_argument("--mode", default="auto", choices=["auto", "main"], help="agent lane mode")
    ap.add_argument("--live-user", default=None,
                    help="username to sign in with for --live (dedicated eval account, no MFA)")
    ap.add_argument("--live-password", default=None, help="password for --live-user")
    args = ap.parse_args()

    results = {"ts": time.time(), "offline": [], "live": []}
    failed = 0

    if not args.live and not args.no_offline:
        print("=" * 60)
        print(" OFFLINE SUITE (pure functions, no server needed)")
        print("=" * 60)
        for name, fn in OFFLINE_CHECKS:
            try:
                ok, note = fn()
                status = "PASS" if ok else "SKIP"
                if ok is False and not args.regression:
                    # Under --regression the compare owns the verdict (it knows the
                    # baseline): a standing FAIL is recorded, not re-failed. Without it,
                    # a failure fails the run directly.
                    failed += 1
                print(f"  [{status}] {name:16s} {note}")
                results["offline"].append({"name": name, "status": status, "note": note})
            except AssertionError as e:
                if not args.regression:
                    failed += 1
                print(f"  [FAIL] {name:16s} {e}")
                results["offline"].append({"name": name, "status": "FAIL", "note": str(e)})
            except Exception as e:
                if not args.regression:
                    failed += 1
                print(f"  [FAIL] {name:16s} {type(e).__name__}: {e}")
                results["offline"].append({"name": name, "status": "FAIL", "note": f"{type(e).__name__}: {e}"})

    if args.live:
        print("=" * 60)
        print(f" LIVE SUITE (base={args.base}, mode={args.mode})")
        print("=" * 60)
        session = None
        if args.live_user:
            if not args.live_password:
                print("live eval needs --live-password with --live-user")
                return 2
            session = _live_login(args.base, args.live_user, args.live_password)
        env = _live_env(args.base, session, args.mode, 0.3)
        print(f"  env: {json.dumps(env)}")
        for task in LIVE_TASKS:
            rec = run_live_task(args.base, task, args.mode, client=session)
            rec["env"] = env
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
        if session is not None:
            session.close()

    if args.mock:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from eval_mock import (BASELINE_FILE, MOCK_TASKS, compare_baseline, run_mock_suite)
        task_filter = ([t.strip() for t in args.tasks.split(",") if t.strip()]
                       if args.tasks else None)
        if task_filter:
            unknown = [t for t in task_filter if t not in {m["name"] for m in MOCK_TASKS}]
            if unknown:
                print(f"unknown mock tasks: {unknown}")
                return 2
        print("=" * 60)
        print(f" MOCK SUITE ({len(MOCK_TASKS) if not task_filter else len(task_filter)} tasks x "
              f"{args.repeats}, hermetic: scripted model, no server/GPU/network)")
        print("=" * 60)
        t0 = time.time()
        suite = run_mock_suite(
            repeats=args.repeats, task_filter=task_filter,
            include_retrieval=not args.no_retrieval, include_router=not args.no_router,
            retrieval_embedder=args.embedder, include_code=not args.no_code,
            progress=lambda name, ok: print(f"  [{'PASS' if ok else 'FAIL'}] {name}"))
        print(f"  aggregate: {suite['aggregate']['pass_rate']} "
              f"({suite['aggregate']['passes']}/{suite['aggregate']['total']}) "
              f"ci95={suite['aggregate']['ci95']}")
        if suite.get("retrieval") is not None:
            r = suite["retrieval"]
            if r.get("recall_at_k") is not None:
                print(f"  retrieval: recall@6={r.get('recall_at_k')} "
                      f"(lexical {r.get('lexical_recall_at_k')}, lexonly "
                      f"{r.get('lexonly_recall_at_k')}, paraphrase "
                      f"{r.get('paraphrase_recall_at_k')}) rank~{r.get('mean_rank')}")
            if r.get("hybrid_real_recall_at_k") is not None:
                print(f"  retrieval-real: recall@6={r.get('hybrid_real_recall_at_k')} "
                      f"@1={r.get('hybrid_real_recall_at_1')} "
                      f"(lexical {r.get('hybrid_real_lexical_recall_at_k')}, paraphrase "
                      f"{r.get('hybrid_real_paraphrase_recall_at_k')}) "
                      f"rank~{r.get('hybrid_real_mean_rank')} "
                      f"fallout~{r.get('hybrid_real_fallout_at_k')}")
            if r.get("fallout_at_k") is not None:
                print(f"  retrieval fallout: hybrid~{r.get('fallout_at_k')} "
                      f"(lexical {r.get('lexical_fallout_at_k')}, paraphrase "
                      f"{r.get('paraphrase_fallout_at_k')}) "
                      f"lexonly~{r.get('lexonly_fallout_at_k')}")
            if r.get("problems"):
                for p in r["problems"][:3]:
                    print(f"  retrieval problem: {p}")
        if suite.get("router") is not None:
            print(f"  router: shortcut_on={suite['router']['shortcut_on']['ok']} "
                  f"shortcut_off={suite['router']['shortcut_off']['ok']}")
        if suite.get("code") is not None:
            c = suite["code"]
            print(f"  code: recall={c.get('recall')} "
                  f"p95={c.get('latency_ms_p95')}ms")
            if c.get("problems"):
                for p in c["problems"][:3]:
                    print(f"  code problem: {p}")
        print(f"  mock suite took {time.time() - t0:.1f}s")
        # live array carries harness-marked records: mock regressions today, real-server
        # runs tomorrow. Never conflate the two when reading this file back.
        for name, t in suite["tasks"].items():
            results["live"].append({
                "name": name, "harness": "mock", "ok": t["pass_rate"] == 1.0,
                "pass_rate": t["pass_rate"], "n": t["n"], "ci95": t["ci95"],
                "stable": t["stable"],
                "problems": [p for run in t["runs"] for p in run["problems"]][:5],
            })
        if suite.get("retrieval") is not None:
            results["retrieval"] = suite["retrieval"]
        if suite.get("router") is not None:
            results["router"] = {k: {"ok": v["ok"], "problems": v["problems"]}
                                 for k, v in suite["router"].items() if isinstance(v, dict)}
        if suite.get("code") is not None:
            results["code"] = suite["code"]
        if not args.regression:
            # Raw verdict (no baseline to compare against): any failing task, slice, or
            # a retrieval/router/code slice that reports not-ok fails the run directly.
            for name, t in suite["tasks"].items():
                if t["pass_rate"] < 1.0:
                    failed += 1
            if suite.get("retrieval") is not None and not suite["retrieval"].get("ok", True):
                failed += 1
            if suite.get("router") is not None and not suite["router"].get("ok", True):
                failed += 1
            if suite.get("code") is not None and not suite["code"].get("ok", True):
                failed += 1

    # Baseline update and regression compare run for BOTH modes. They used to sit
    # inside `if args.mock:`, which made two CI commands silently no-ops:
    #   * `--update-baseline` on its own wrote nothing (the documented way to
    #     re-baseline the offline suite), and
    #   * `--regression` on its own - CI's offline gate - counted no failures
    #     (the raw verdict is suppressed by --regression) and compared nothing,
    #     so it could not fail however badly the offline checks degraded. That is
    #     how compact_endpoint stayed FAIL in the baseline unnoticed.
    from eval_mock import BASELINE_FILE
    if args.update_baseline:
        from eval_mock import _summarize_suite
        summary = _summarize_suite(
            suite if args.mock else {}, results["offline"] or None)
        if BASELINE_FILE.is_file():
            # Merge, don't clobber: each section gates only what it measured,
            # and an overwrite would silently un-gate the other mode/section.
            # - retrieval: --embedder fake must keep hybrid_real keys and
            #   vice versa (they gate at different tolerances).
            # - code: --no-code must not erase the measured recall.
            # - offline: --no-offline must not erase the standing records
            #   (CI's offline gate compares PASS->FAIL against them; a null
            #   offline section makes the gate inert).
            prior = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
            merged_ret = dict((prior.get("retrieval") or {}))
            merged_ret.update(summary.get("retrieval") or {})
            summary["retrieval"] = merged_ret or None
            # tasks and the aggregate are mock-owned: an offline-only update
            # measures neither, and dropping them would silently un-gate the
            # mock suite until someone remembered to re-run it.
            merged_tasks = dict((prior.get("tasks") or {}))
            merged_tasks.update(summary.get("tasks") or {})
            summary["tasks"] = merged_tasks
            if summary.get("aggregate_rate") is None and prior.get("aggregate_rate") is not None:
                summary["aggregate_rate"] = prior["aggregate_rate"]
            if summary.get("code") is None and prior.get("code") is not None:
                summary["code"] = prior["code"]
            if summary.get("offline") is None and prior.get("offline") is not None:
                summary["offline"] = prior["offline"]
        BASELINE_FILE.write_text(json.dumps(summary, indent=2),
                                 encoding="utf-8")
        print(f"\nbaseline written -> {BASELINE_FILE} (review the diff before committing)")
    if args.regression:
        from eval_mock import compare_baseline as _compare
        if not BASELINE_FILE.is_file():
            print(f"\nno baseline at {BASELINE_FILE} - run --update-baseline first")
            failed += 1
        else:
            from eval_mock import _summarize_suite as _sum
            baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
            if args.mock:
                current = _sum(suite, results["offline"] or None)
            else:
                # offline-only invocation: compare just the offline section. Everything
                # else stays None, which compare_baseline reads as "not measured".
                current = {"tasks": {}, "aggregate_rate": None, "retrieval": None,
                           "router": None, "code": None,
                           "offline": ({r["name"]: r["status"] for r in results["offline"]}
                                      or None)}
            problems = _compare(current, baseline)
            if problems:
                failed += len(problems)
                print("\nREGRESSIONS vs baseline:")
                for p in problems:
                    print(f"  - {p}")
            else:
                print("\nno regressions vs baseline")

    RESULTS_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults saved -> {RESULTS_FILE}")
    print("SUITE:", "FAILED" if failed else "ALL OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
