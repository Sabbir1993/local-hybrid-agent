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

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
RESULTS_FILE = Path(__file__).resolve().parent / "eval_results.json"
# Live runs write their own file. They used to share eval_results.json with the mock suite,
# so a 30-minute GPU run silently replaced the mock artifact that CI's gate reads - and the
# committed file then carried "live: harness mock" records from a run that never happened.
LIVE_RESULTS_FILE = Path(__file__).resolve().parent / "eval_results_live.json"
LIVE_BASELINE_FILE = Path(__file__).resolve().parent / "eval_live_baseline.json"

import eval_stats   # noqa: E402
import eval_tasks   # noqa: E402


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

# Kept for backwards compatibility: the historical five, now expressed in the eval_tasks
# vocabulary. The real set is tests/eval_tasks.py TASKS - larger, categorised, and graded by
# the same deterministic checkers.
LIVE_TASKS = [
    {"name": "direct_tool_list", "prompt": "List the files in the workspace root.",
     "expect_tools": ["list_files"], "soft": False, "category": "files"},
    {"name": "write_and_verify", "prompt": "Create a file eval_tmp/hello.py that contains print('hello eval'), then run it with run_python to prove it works.",
     "expect_tools": ["write_file", "run_python"], "soft": False, "category": "files"},
    {"name": "compute_only", "prompt": "Use run_python to compute 137*29 and reply with just the number.",
     "expect_tools": ["run_python"], "forbid_tools": ["write_file"],
     "expect_final_contains": ["3973"], "soft": False, "category": "compute"},
    {"name": "memory_search", "prompt": "Search your memory for anything related to llama or vulkan and summarize what you find.",
     "expect_tools": ["search_memory"], "soft": True, "category": "retrieval"},
    {"name": "escalation_task", "prompt": "In eval_tmp/pkg there are no files yet. First create a.py, b.py and c.py there, each with one blocking requests.get call. Then convert all three to async httpx end-to-end, and write eval_tmp/MIGRATION.md describing the change.",
     "expect_escalation": True, "soft": True, "category": "planning"},
]

# Local workspace root, when set. Only needed for expect_files checks; a remote server
# cannot be checked from here and the omission is reported per task rather than counted
# as a pass.
LIVE_WORKSPACE = None


def _abort(msg: str) -> None:
    """Print and exit 2. Exit 2 means 'you invoked this wrong'; exit 1 is reserved for 'the
    agent failed a task'. Those are different problems and should not look the same."""
    print("\n" + msg, file=sys.stderr)
    raise SystemExit(2)


def _live_preflight(base: str, timeout_s: float = 10) -> None:
    """Fail in under a second if the server is not reachable at all.

    Reachability ONLY. This deliberately does not treat 401 as a problem: an anonymous probe
    against an auth-required server returns 401 whether or not you went on to supply valid
    credentials, so gating on it here rejected every correctly-configured run. Credentials are
    the login step's business, and a wrong guess there is reported against the account, not
    the server.

    Before this existed, a server that was not listening raised an unhandled
    httpx.ConnectError traceback out of _live_login, and a server that was listening but
    rejecting the session made all 22 tasks x 5 repeats fail one 45s timeout at a time -
    roughly 90 minutes to learn you were not signed in.
    """
    import httpx
    try:
        httpx.get(f"{base}/control/status", timeout=timeout_s)
    except httpx.ConnectError:
        _abort(f"live eval: nothing is listening on {base}.\n"
               "  Start the app first, e.g.  python server_manager.py --port 8000\n"
               "  then re-run with --base <url> if it is on a different port.")
    except httpx.HTTPError as e:
        _abort(f"live eval: could not reach {base} ({type(e).__name__}: {e})")
    # Any HTTP status means the server answered. 401/403 here is expected and fine.


def _arm_csrf(client, base: str) -> None:
    """Echo the CSRF cookie as the X-CSRF-Token header, exactly the way the frontend does.

    core/csrf.py rejects every mutating request that carries a session cookie without a
    matching header (403 "csrf token missing or invalid"). A benchmark harness that logs in
    and then POSTs without it measures nothing - every task fails identically before the
    agent loop ever sees the prompt. This was the actual cause of the first live smoke test
    reading 0/3: MFA worked, the session was valid, and every task still 403'd.

    Set as a client-level default header so client.stream() inherits it.
    """
    csrf = client.cookies.get("a770_csrf")
    if csrf:
        client.headers["X-CSRF-Token"] = csrf
    else:
        _abort(f"live eval: login to {base} returned 200 but set no CSRF cookie. "
               "The session is incomplete - this is a server bug, not an eval bug.")


def _live_ensure_project(base: str, client, name: str, workspace_dir: str) -> int:
    """Create (or reuse) and activate the eval project for the logged-in user.

    The agent loop refuses every run with 403 agent_workspace_unavailable when no
    project is selected on the device (core/agent_tools/workspace.py:
    require_device_workspace). The harness must do what the UI does after login:
    create a project pointing at the workspace, then activate it. If activation
    fails, every task would 403 identically - the exact 0/3 result this fix
    replaces - so each step aborts with the server's own reason instead.
    """
    import httpx
    r = client.get(f"{base}/control/projects")
    if r.status_code != 200:
        _abort(f"live eval: could not list projects: HTTP {r.status_code}")
    data = r.json()
    proj = next((p for p in data.get("projects", []) if p.get("name") == name), None)
    if proj is None:
        r = client.post(f"{base}/control/projects",
                        json={"name": name, "workspace_dir": workspace_dir})
        if r.status_code not in (200, 201):
            _abort(f"live eval: project create failed: HTTP {r.status_code} {r.text[:200]!r}")
        proj = r.json().get("project") or {}
        pid = proj.get("id")
        if not pid:
            _abort(f"live eval: project create returned no id: {str(r.json())[:200]}")
        print(f"  project: created '{name}' (id={pid}, workspace={workspace_dir})")
    else:
        pid = proj.get("id")
        need_workspace = not proj.get("workspace_dir")
        if workspace_dir and need_workspace:
            r = client.patch(f"{base}/control/projects/{pid}/workspace",
                             json={"workspace_dir": workspace_dir})
            if r.status_code != 200:
                _abort(f"live eval: project workspace update failed: "
                       f"HTTP {r.status_code} {r.text[:200]!r}")
        print(f"  project: reusing '{name}' (id={pid})")
    r = client.post(f"{base}/control/projects/{pid}/activate")
    if r.status_code != 200:
        _abort(f"live eval: project activation failed: HTTP {r.status_code} {r.text[:200]!r}")
    print(f"  project: activated '{name}'")
    return pid


def _live_preflight_companion(base: str, client, username: str) -> None:
    """Fail fast when the user's Companion app is not connected.

    require_device_workspace gates on companion_bridge.is_available after the
    project check, and every file tool (read/write/grep/edit) routes through the
    companion websocket. Without it each task 403s or errors per-tool - a whole
    suite of indistinguishable failures. Naming it before any task runs turns
    90 minutes of noise into one line.
    """
    r = client.get(f"{base}/control/companion/status")
    if r.status_code != 200:
        print(f"  WARNING: companion status unavailable (HTTP {r.status_code}) - "
              "file tools may fail per-task; expected on a headless server.")
        return
    info = r.json() or {}
    if not info.get("connected"):
        _abort("live eval: the A770 Companion app is not connected for user "
               f"'{username}'.\n"
               "  File tools run on the user's machine through the Companion, so the "
               "agent loop refuses runs (or fails every file tool) without it.\n"
               "  Open the Companion app, pair/approve this user, then re-run.")
    print(f"  companion: connected ({info.get('device_name') or 'device'})")


def _totp_code(cmd: str) -> str:
    """Run a user-supplied command and take its stdout as the current TOTP code.

    Deliberately NOT a --live-totp-secret option that reads users.totp_secret out of auth.db.
    That is the same move as the db_explorer hole this repo just closed: the seed turns into
    a valid code for the account forever, and a benchmark harness is exactly where you do not
    want a credential-persistence path. This keeps the secret inside whatever the user already
    trusts (oathtool, a password-manager CLI, an authenticator bridge).
    """
    import subprocess
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    except Exception as e:
        _abort(f"live eval: --live-totp-cmd failed to run ({type(e).__name__}: {e})")
    digits = "".join(ch for ch in (out.stdout or "") if ch.isdigit())
    if not digits:
        _abort(f"live eval: --live-totp-cmd printed no digits (stdout={out.stdout!r}, "
               f"stderr={(out.stderr or '')[:80]!r})")
    return digits[:6].rjust(6, "0")


def _live_login(base: str, username: str, password: str, totp: str = None,
                timeout_s: float = 60):
    """Session login for live runs (mirrors a real user; API tokens are fenced
    off admin perms).

    MFA is supported through the real two-step flow the UI uses: the password buys a
    short-lived, single-use, IP-bound ticket (never a session), and /auth/mfa/verify trades
    ticket + code for the session cookies. A static `totp` works because login happens once
    per run, not once per task.
    """
    import httpx
    client = httpx.Client(timeout=httpx.Timeout(timeout_s))
    try:
        r = client.post(f"{base}/auth/login", json={"username": username, "password": password})
    except httpx.ConnectError:
        client.close()
        _abort(f"live eval: nothing is listening on {base} (connection refused during login).\n"
               "  Start the app first, e.g.  python server_manager.py --port 8000")
    except httpx.HTTPError as e:
        client.close()
        _abort(f"live eval: login transport error: {type(e).__name__}: {e}")
    data = r.json() if r.status_code == 200 else {}
    if r.status_code != 200:
        client.close()
        _abort(f"live login failed: HTTP {r.status_code} {str(data)[:150]}")

    if data.get("mfa_required"):
        if data.get("enroll_required"):
            client.close()
            _abort("live eval: this account is required to enrol MFA but has not. Enrol in the "
                   "UI first, or point --live-user at a dedicated non-MFA eval account.")
        if not totp:
            client.close()
            _abort("live eval: this account has TOTP enabled and the harness cannot answer a "
                   "prompt.\n"
                   "  Either pass --live-totp <6-digit code>, or --live-totp-cmd '<command that "
                   "prints it>' (e.g. 'oathtool --totp -b <secret>'),\n"
                   "  or --live-user a dedicated account with MFA off. A dedicated account is "
                   "recommended: this suite writes files into the workspace.")
        ticket = data.get("ticket") or ""
        try:
            v = client.post(f"{base}/auth/mfa/verify", json={"ticket": ticket, "code": totp})
        except httpx.HTTPError as e:
            client.close()
            _abort(f"live eval: MFA verify transport error: {type(e).__name__}: {e}")
        if v.status_code != 200:
            client.close()
            _abort(f"live MFA verify failed: HTTP {v.status_code} {v.text[:150]}\n"
                   "  A wrong code burns one of 5 guesses on the ticket and the ticket then "
                   "expires after 5 minutes, so re-fetch a fresh code and log in again.")
        _arm_csrf(client, base)
        return client

    if "user" not in data:
        client.close()
        _abort(f"live login returned no user and no MFA challenge: {str(data)[:150]}")
    _arm_csrf(client, base)
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


def _finish_early(task: dict, rec: dict, t0: float) -> dict:
    """Shared tail for every run_live_task exit that happens before the stream is consumed.

    These exits used to `return rec` directly, which skipped both duration_s and the problems
    computation - so an HTTP error surfaced as `run 1/1 Nones` with no explanation attached,
    and the operator had to read eval_results_live.json to learn it was a 403. A failed run
    must always carry its duration and its reasons.
    """
    rec["duration_s"] = round(time.time() - t0, 1)
    rec["problems"] = eval_tasks.check_record(task, rec, workspace=LIVE_WORKSPACE or None)
    rec["ok"] = not rec["problems"]
    return rec


def run_live_task(base: str, task: dict, mode: str, timeout_s: float = 600, client=None) -> dict:
    import httpx
    rec = {"name": task["name"], "tools": [], "lanes": [], "escalated": False,
           "final_len": 0, "final_text": "", "ok": False, "soft": task.get("soft", False),
           "error": None, "duration_s": None, "events_seen": [], "done_state": None,
           "guard_hits": [], "kb_blocked": None}
    payload = {"messages": [{"role": "user", "content": task["prompt"]}],
               "mode": mode, "max_steps": 12, "temperature": 0.3}
    t0 = time.time()
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=httpx.Timeout(timeout_s))
    try:
        with client.stream("POST", f"{base}/agent/run", json=payload) as resp:
            if resp.status_code == 401:
                rec["error"] = ("HTTP 401 - session missing or expired. If you logged in, the "
                                "CSRF header may be missing: the harness now mirrors the "
                                "frontend's X-CSRF-Token echo, so report this as an eval bug.")
                return _finish_early(task, rec, t0)
            if resp.status_code in (403, 400, 422):
                # The body names WHICH server-side gate fired: agent_native_only (browser
                # User-Agent), agent_requires_companion (Companion app not connected for this
                # user), agent_workspace_unavailable (no device workspace), or a validation
                # error. Without it every one of those reads as an identical bare 403.
                body = ""
                try:
                    body = resp.read().decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                detail = f" body={body[:300]!r}" if body.strip() else " (empty body)"
                if resp.status_code == 403:
                    rec["error"] = (f"HTTP 403{detail}. The session is valid; the app refused "
                                    "the run itself.")
                else:
                    rec["error"] = f"HTTP {resp.status_code}{detail}"
                return _finish_early(task, rec, t0)
            if resp.status_code != 200:
                rec["error"] = f"HTTP {resp.status_code}"
                return _finish_early(task, rec, t0)
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
                    if event not in rec["events_seen"]:
                        rec["events_seen"].append(event)
                    if event == "lane":
                        rec["lanes"].append(data.get("lane"))
                    elif event == "tool_call":
                        rec["tools"].append(data.get("name"))
                    elif event == "tool_result":
                        pass    # counted via tool_call; kept here so it is not "unknown"
                    elif event == "delta":
                        rec["final_len"] += len(data.get("text") or "")
                        rec["final_text"] += data.get("text") or ""
                    elif event == "guard":
                        rec["guard_hits"].append(data.get("rule") or data.get("message"))
                    elif event == "kb_blocked":
                        rec["kb_blocked"] = data.get("message")
                    elif event == "done":
                        # The payload names the outcome: completed, stopped (with a reason
                        # like max_steps / loop / budget), failed (with a reason kind), or
                        # filtered. Dropping it is how a run that died in setup looked
                        # identical to a run where the model said nothing.
                        rec["done_state"] = data.get("state")
                        if data.get("state") == "failed":
                            rec["error"] = (f"agent run failed: "
                                            f"{data.get('reason') or data}")
                        elif data.get("note") == "response filtered by policy":
                            rec["error"] = "response filtered by policy"
                        break
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["duration_s"] = round(time.time() - t0, 1)
        rec["problems"] = eval_tasks.check_record(task, rec, workspace=LIVE_WORKSPACE or None)
        rec["ok"] = not rec["problems"]
        return rec
    finally:
        if own_client:
            client.close()
    rec["duration_s"] = round(time.time() - t0, 1)
    lanes = rec["lanes"]
    rec["escalated"] = ("executor" in lanes and "main" in lanes
                        and (lanes.index("main") > lanes.index("executor")
                             or lanes[-1] == "main"))
    # Grading lives in tests/eval_tasks.check_record so the checker vocabulary is defined in
    # one place and reviewable on its own. It grades the ANSWER, not just tool plumbing:
    # compute_only passes only if the model actually replied 3973, not for calling run_python
    # and answering a haiku. expect_final_lacks is the negative half - without it a task
    # passes on a model that blurts a value it should have withheld.
    rec["problems"] = eval_tasks.check_record(task, rec, workspace=LIVE_WORKSPACE or None)
    rec["ok"] = not rec["problems"]
    return rec


def run_live_suite(base: str, tasks, mode: str, repeats: int, session=None,
                   timeout_s: float = 600) -> dict:
    """Run every task `repeats` times and summarize with Wilson intervals.

    Repeats are the whole point: a single run cannot tell "this works" from "this worked
    once", and cannot tell you a task is flaky, which is usually a prompt or tool-schema
    problem rather than a capability limit. Setup prompts are re-run per repeat and are not
    scored, so a repeat never inherits the previous repeat's files.
    """
    records = []
    t_all = time.time()
    for task in tasks:
        runs = []
        for i in range(max(1, repeats)):
            for s in task.get("setup", []) or []:
                bare = {"name": task["name"] + "#setup", "prompt": s, "soft": True}
                run_live_task(base, bare, mode, timeout_s=timeout_s, client=session)
            rec = run_live_task(base, task, mode, timeout_s=timeout_s, client=session)
            runs.append(rec)
            why = ""
            if not rec["ok"]:
                bits = list((rec.get("problems") or [])[:2])
                if not bits and rec.get("done_state") not in (None, "completed"):
                    bits = [f"done={rec['done_state']}"]
                if not bits and rec.get("events_seen"):
                    bits = [f"events={','.join(rec['events_seen'])}"]
                why = "  " + ";".join(bits) if bits else ""
            print(f"    [{'PASS' if rec['ok'] else 'FAIL'}] {task['name']} "
                  f"run {i + 1}/{repeats} {rec['duration_s']}s{why}")
        records.append(eval_stats.summarize_task(
            task["name"], runs, category=task.get("category", "misc"),
            soft=task.get("soft", False)))
        r = records[-1]
        print(f"  [{'PASS' if r['n_pass'] == r['n'] else ('FLAKY' if r['flaky'] else 'FAIL')}]"
              f" {r['name']:26s} {r['n_pass']}/{r['n']} "
              f"ci95[{r['ci95'][0]:.2f},{r['ci95'][1]:.2f}]")
    summary = eval_stats.rollup(records)
    summary["wall_clock_s"] = round(time.time() - t_all, 1)
    return {"records": records, "summary": summary}


def _live_compare_summary(records: list) -> dict:
    """Per-task {pass_rate, n} from live records, shaped for compare_baseline."""
    return {"tasks": {r["name"]: {"pass_rate": r["pass_rate"], "n": r["n"]}
                      for r in records},
            "aggregate_rate": None, "retrieval": None, "router": None, "code": None}


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
    ap.add_argument("--live-regression", action="store_true",
                    help="compare live results against tests/eval_live_baseline.json, exit 1 on "
                         "pass-rate drop (report-only until a committed baseline exists)")
    ap.add_argument("--update-live-baseline", action="store_true",
                    help="write current live results to tests/eval_live_baseline.json "
                         "(review the diff!)")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="manager base URL")
    ap.add_argument("--mode", default="auto", choices=["auto", "main"], help="agent lane mode")
    ap.add_argument("--live-user", default=None,
                    help="username to sign in with for --live (dedicated eval account, no MFA)")
    ap.add_argument("--live-password", default=None, help="password for --live-user")
    ap.add_argument("--live-repeats", type=int, default=5,
                    help="runs per live task (default 5). Repeatability and Wilson intervals "
                         "are the point of the live suite; 1 gives you no information "
                         "beyond 'it worked once'")
    ap.add_argument("--live-tasks", default=None,
                    help="comma-separated live task names (default: all of eval_tasks.TASKS)")
    ap.add_argument("--live-categories", default=None,
                    help="comma-separated categories to run, e.g. files,compute,docs,guard")
    ap.add_argument("--live-workspace", default=None,
                    help="local workspace root, enabling expect_files checks. Omit when the "
                         "server is remote; file checks are then reported as skipped")
    ap.add_argument("--live-project", default="__eval__",
                    help="project name to create/activate on the server for --live "
                         "(default '__eval__'). The agent loop refuses runs without an "
                         "active project.")
    ap.add_argument("--legacy-tasks", action="store_true",
                    help="run the historical five LIVE_TASKS instead of eval_tasks.TASKS")
    ap.add_argument("--no-soft", action="store_true",
                    help="drop tasks marked soft, so every task in the run gates")
    ap.add_argument("--live-timeout", type=float, default=600.0,
                    help="per-run timeout in seconds (default 600)")
    ap.add_argument("--live-totp", default=None,
                    help="6-digit TOTP code for an MFA-enabled account. Only needed once per "
                         "run (login happens before the first task), not once per task")
    ap.add_argument("--live-totp-cmd", default=None,
                    help="command that prints the current TOTP code, e.g. "
                         "'oathtool --totp -b <secret>'. Preferred over --live-totp for a "
                         "long run. The harness deliberately has no option to read "
                         "users.totp_secret out of auth.db")
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
        global LIVE_WORKSPACE
        LIVE_WORKSPACE = args.live_workspace
        tasks = LIVE_TASKS if args.legacy_tasks else eval_tasks.select(
            (args.live_tasks.split(",") if args.live_tasks else None),
            (args.live_categories.split(",") if args.live_categories else None),
            include_soft=not args.no_soft)
        if not tasks:
            print("no live tasks selected")
            return 2
        gating = sum(1 for t in tasks if not t.get("soft"))
        print("=" * 60)
        print(f" LIVE SUITE ({len(tasks)} tasks x {args.live_repeats} repeats, {gating} gating)")
        print(f" base={args.base} mode={args.mode} workspace={args.live_workspace or '(remote)'}")
        print("=" * 60)
        # Reachability + auth first, before any task runs. Both failure modes used to surface
        # as per-task timeouts across the whole suite, which is a 90-minute way to learn you
        # forgot to start the server.
        _live_preflight(args.base)
        session = None
        totp = args.live_totp
        if args.live_totp_cmd:
            totp = _totp_code(args.live_totp_cmd)
        if args.live_user:
            if not args.live_password:
                print("live eval needs --live-password with --live-user")
                return 2
            session = _live_login(args.base, args.live_user, args.live_password, totp=totp)
            if totp:
                print("  MFA: second factor supplied")
            _live_preflight_companion(args.base, session, args.live_user)
            # require_device_workspace() runs unconditionally in agent setup
            # (routes/agent/setup.py) and needs a project folder on the user's
            # machine, so the loop refuses every run without a real workspace.
            # The old guidance "omit --live-workspace for a remote server" only
            # affected file GRADING; the loop itself 403'd after the project
            # gate and produced exactly the 0/3 this bootstrap replaces.
            ws_dir = args.live_workspace
            if not ws_dir:
                print("live eval needs --live-workspace <absolute path> so the agent loop "
                      "has a project folder (require_device_workspace is unconditional).")
                return 2
            if not Path(ws_dir).is_absolute():
                print(f"--live-workspace must be an absolute path, got: {ws_dir!r}")
                return 2
            _live_ensure_project(args.base, session, args.live_project, ws_dir)
        else:
            print("  NOTE: no --live-user given. If the server requires a session every task "
                  "will fail with HTTP 401 - that is an auth problem, not an agent result.")
        env = _live_env(args.base, session, args.mode, 0.3)
        print(f"  env: {json.dumps(env)}")
        if not args.live_workspace and any(t.get("expect_files") for t in tasks):
            print("  NOTE: no --live-workspace, so expect_files checks will report as skipped, "
                  "not passed - the files/docs categories measure little without it.")
        # Safety notice, not a disclaimer. Two tasks deliberately ask the agent to do
        # destructive or overreaching things (delete every file in the workspace; echo a
        # secret back). They exist to prove the guard/refusal behaviour. If the agent gets
        # one wrong AND --live-workspace is your real working folder, the eval is the thing
        # that deletes it. A scratch directory is the cheap insurance.
        destructive = [t["name"] for t in tasks
                       if t.get("category") in ("guard", "restraint")]
        if destructive:
            print(f"  NOTE: {len(destructive)} task(s) probe refusal behaviour "
                  f"({', '.join(destructive[:4])}{'...' if len(destructive) > 4 else ''}). "
                  "Run --live-repeats against a scratch workspace first.")
        if args.live_workspace:
            from pathlib import Path as _P
            ws = _P(args.live_workspace)
            if not ws.is_dir():
                print(f"  WARNING: --live-workspace {ws} is not a directory; file checks will "
                      "report as skipped.")
            else:
                print(f"  workspace: {ws.resolve()}")
        suite = run_live_suite(args.base, tasks, args.mode, args.live_repeats,
                               session=session, timeout_s=args.live_timeout)
        if session is not None:
            session.close()
        print()
        print(eval_stats.format_report(suite["summary"], suite["records"]))
        results["live_summary"] = suite["summary"]
        results["live_records"] = suite["records"]
        results["harness"] = "live"
        results["env"] = env
        # Only gating tasks with every run passing fail the run. A flaky task is reported as
        # flaky rather than failed: at temperature 0.3 a flaky task means the prompt or the
        # tool schema is underspecified, which is a different fix from "the model cannot".
        for r in suite["records"]:
            if r["soft"]:
                continue
            if r["n_pass"] == 0 or r["flaky"]:
                failed += 1
        live_out = {"ts": time.time(), "env": env, "repeats": args.live_repeats,
                    "tasks": args.legacy_tasks and "legacy" or "eval_tasks",
                    "summary": suite["summary"], "records": suite["records"]}
        LIVE_RESULTS_FILE.write_text(json.dumps(live_out, indent=2), encoding="utf-8")
        print(f"\nlive results -> {LIVE_RESULTS_FILE}")

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
        results["harness"] = "mock"
        # Make the mislabelling impossible to read past: the "live" key holds mock records,
        # which is confusing enough on its own. If a real live suite also ran, this run's
        # live numbers went to eval_results_live.json and the summary is here.
        if "live_summary" not in results:
            results["live_note"] = ("these records are from the MOCK harness: a scripted model "
                                    "replaying canned SSE bytes, not the agent. Use --live "
                                    "against a running server for a real measurement")
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

    # Live baseline (mirror of the mock one, for pass-rate drops only). A live
    # baseline is a measurement of one model on one machine on one day, so it is
    # report-only until someone runs --update-live-baseline deliberately and
    # reviews the diff. Soft and flaky tasks are compared the same way the mock
    # suite compares them: a drop is a drop, and the record shows the CI.
    if args.live:
        if args.update_live_baseline:
            summary = _live_compare_summary(results["live_records"])
            if LIVE_BASELINE_FILE.is_file():
                prior = json.loads(LIVE_BASELINE_FILE.read_text(encoding="utf-8"))
                merged_tasks = dict((prior.get("tasks") or {}))
                merged_tasks.update(summary.get("tasks") or {})
                summary["tasks"] = merged_tasks
            LIVE_BASELINE_FILE.write_text(json.dumps(summary, indent=2),
                                          encoding="utf-8")
            print(f"\nlive baseline written -> {LIVE_BASELINE_FILE} "
                  "(review the diff before committing)")
        if args.live_regression:
            from eval_mock import compare_baseline as _live_compare
            if not LIVE_BASELINE_FILE.is_file():
                print(f"\nno live baseline at {LIVE_BASELINE_FILE} - "
                      "run --update-live-baseline after a good live run first")
                failed += 1
            else:
                live_base = json.loads(LIVE_BASELINE_FILE.read_text(encoding="utf-8"))
                problems = _live_compare(_live_compare_summary(results["live_records"]),
                                         live_base)
                if problems:
                    failed += len(problems)
                    print("\nLIVE REGRESSIONS vs baseline:")
                    for p in problems:
                        print(f"  - {p}")
                else:
                    print("\nno live regressions vs baseline")

    out_file = LIVE_RESULTS_FILE if (args.live and not args.mock) else RESULTS_FILE
    out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults saved -> {out_file}")
    print("SUITE:", "FAILED" if failed else "ALL OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
