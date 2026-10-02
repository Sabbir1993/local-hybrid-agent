"""tests/eval_mock.py - hermetic mock-LLM harness for the A770 agent loop.

Runs the REAL POST /agent/run route (real loop, real tools, real repair/verify/undo,
real guards, real SSE serialization) against a SCRIPTED model: deterministic tool-call
transcripts served at the httpx transport level. No GPU, no server binary, no network.

What this measures: the loop's behaviour given fixed model behaviour - tool sequences,
argument repair, verify/undo, escalation, stopping, final answers. Repeats are identical
by construction, so this is a REGRESSION harness (did my change alter the outcome?), not
a capability benchmark. Live variance belongs to --live with real models.

Design notes that matter:
- The mock sits at the httpx transport (httpx.MockTransport), so admission gating, the
  context-overflow/grammar/tool_choice retry chains, SSE parsing, usage accounting and
  the KV-prefix compaction path all still execute. Nothing above the socket is stubbed.
- Tools execute for real. The FakeCompanion below is disk-backed (a temp workspace) and
  run_python really runs `python` in a subprocess - the executed code comes from the
  task scripts in this file, never from an untrusted model.
- "ask_first" is off via config (the legitimate user setting), so no permission modal
  can stall a run. Permission-gated flows have their own test files.
- Every repeat gets fresh temp dirs and temp DB files, so runs cannot contaminate
  each other. Nothing here touches the real projects.db / auth.db / memory.db.
"""

import ast
import asyncio
import json
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import httpx

UID = 7
DEVICE = "dev1"
PROJECT = "demo"

# ---------------------------------------------------------------------------
# Part 1: scripted model (httpx transport level)
# ---------------------------------------------------------------------------

# The script for the run in progress. Sequential tasks only, so one slot suffices.
# Extra model calls past the end of the script (escalation replays, verifier, retries)
# get a benign final answer and are COUNTED, so unexpected extra calls fail loudly.
_current = {"fifo": None, "extra_calls": 0, "requests": 0, "n_ctx": 32768}


def _sse_turn(turn: dict, call_id: str = "call_0") -> bytes:
    """One scripted model turn as OpenAI-style SSE bytes."""
    out = []

    def data(obj):
        out.append("data: " + json.dumps(obj) + "\n\n")

    if "content" in turn and turn.get("tool_calls"):
        raise ValueError("a scripted turn is either text or tool calls, not both")
    text = turn.get("content", "")
    # two text chunks: exercises the delta-accumulation path without timing flakes
    if text:
        half = max(1, len(text) // 2)
        for part in (text[:half], text[half:]):
            data({"choices": [{"delta": {"content": part}, "finish_reason": None}]})
    for i, tc in enumerate(turn.get("tool_calls", [])):
        args = tc["arguments"] if isinstance(tc.get("arguments"), str) else json.dumps(tc.get("arguments", {}))
        # arguments in two chunks: exercises the streaming-args accumulation in sse_stream.py
        cut = max(1, len(args) // 2)
        data({"choices": [{"delta": {"tool_calls": [
            {"index": i, "id": f"{call_id}_{i}", "type": "function",
             "function": {"name": tc["name"], "arguments": args[:cut]}}]}, "finish_reason": None}]})
        data({"choices": [{"delta": {"tool_calls": [
            {"index": i, "id": f"{call_id}_{i}", "type": "function",
             "function": {"arguments": args[cut:]}}]}, "finish_reason": None}]})
    finish = "tool_calls" if turn.get("tool_calls") else "stop"
    if turn.get("cut_off"):
        # the reply hit the output limit mid-tool-call: finish_reason length with a partial
        # call accumulated. The loop must discard it whole ("nothing was changed") and tell
        # the model to send a smaller piece - salvaging it would write a partial file.
        finish = "length"
    data({"choices": [{"delta": {}, "finish_reason": finish}]})
    data({"usage": {"prompt_tokens": 120, "completion_tokens": 30,
                    "total_tokens": 150}})
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


def _mock_handler(request: httpx.Request) -> httpx.Response:
    _current["requests"] += 1
    path = request.url.path
    if request.method == "GET" and path == "/slots":
        # context_budget.probe_window: report the configured window, nothing more
        return httpx.Response(200, json=[{"n_ctx": _current["n_ctx"], "n_past": 0}])
    if request.method == "POST" and path == "/v1/chat/completions":
        fifo = _current["fifo"]
        try:
            turn = fifo.get_nowait()
        except queue.Empty:
            _current["extra_calls"] += 1
            turn = {"content": "Done."}
        return httpx.Response(200, content=_sse_turn(turn, f"call_{_current['requests']}"))
    _current["extra_calls"] += 1
    return httpx.Response(404, json={"error": f"mock has no route for {request.method} {path}"})


def mock_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler),
                             base_url="http://127.0.0.1:8090")


class FakePopen:
    """Alive process stand-in: main_ready and is_up() see a running server, so the loop
    never tries to spawn a real llama-server."""

    def __init__(self):
        self.pid = 4242
        self.returncode = None

    def poll(self):
        return None


# ---------------------------------------------------------------------------
# Part 2: FakeCompanion (disk-backed, real subprocess python)
# ---------------------------------------------------------------------------

class FakeCompanion:
    """Device side of the harness. Files are REAL files under the temp workspace and
    run_python scripts REALLY execute - the code under test comes from the task
    scripts in this file, so this is deterministic, not dangerous."""

    def __init__(self, ws: Path):
        self.ws = ws
        self.ops = []

    def _resolve(self, path: str) -> Path:
        p = Path(str(path))
        if not p.is_absolute():
            p = self.ws / p
        # containment, like the real companion policy: never leave the workspace
        try:
            p.resolve().relative_to(self.ws.resolve())
        except ValueError:
            raise RuntimeError(f"fake companion refused path outside workspace: {path}")
        return p

    async def call(self, uid, op, params, timeout=None):
        self.ops.append(op)
        if op == "fs.read":
            p = self._resolve(params["path"])
            if not p.is_file():
                return {"content": None}
            return {"content": p.read_text(encoding="utf-8", errors="replace")}
        if op == "fs.write":
            p = self._resolve(params["path"])
            existed = p.exists()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(params.get("content") or "", encoding="utf-8")
            return {"existed": existed}
        if op == "fs.remove":
            p = self._resolve(params["path"])
            if p.is_file():
                p.unlink()
                return {"removed": True}
            return {"removed": False}
        if op == "fs.list":
            root = self._resolve(params.get("root") or ".")
            pat = params.get("pattern") or "*"
            import fnmatch
            files = sorted(str(f.relative_to(self.ws)).replace("\\", "/")
                           for f in root.rglob("*") if f.is_file()
                           and fnmatch.fnmatch(f.name, pat)) if root.is_dir() else []
            return {"files": files}
        if op == "fs.grep":
            root = self._resolve(params.get("root") or ".")
            pat = params.get("pattern") or ""
            hits = []
            if root.is_dir():
                try:
                    rx = re.compile(pat, re.IGNORECASE)
                except re.error:
                    rx = re.compile(re.escape(pat), re.IGNORECASE)
                for f in sorted(root.rglob("*")):
                    if not f.is_file():
                        continue
                    try:
                        text = f.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    for i, line in enumerate(text.splitlines(), 1):
                        if rx.search(line):
                            hits.append(f"{f.relative_to(self.ws)}:{i}: {line.strip()}"[:200])
                            if len(hits) >= 50:
                                break
                    if len(hits) >= 50:
                        break
            return {"hits": hits}
        if op == "fs.verify":
            p = self._resolve(params["path"])
            if p.suffix.lower() in (".js", ".mjs", ".cjs"):
                try:
                    r = subprocess.run(["node", "--check", str(p)], capture_output=True,
                                       text=True, timeout=20)
                    if r.returncode == 0:
                        return {"checked": True, "ok": True, "detail": "node --check"}
                    return {"checked": True, "ok": False,
                            "detail": (r.stderr.strip().splitlines() or ["SyntaxError"])[0][:240]}
                except (OSError, subprocess.TimeoutExpired):
                    return {"checked": False, "ok": True, "detail": "no node available"}
            return {"checked": False, "ok": True, "detail": "server-side check covers this type"}
        if op == "shell.run":
            return await self._shell(params, timeout)
        raise RuntimeError(f"fake companion has no op {op!r} (harness bug: script used an "
                           f"unimplemented tool)")

    async def _shell(self, params, timeout):
        m = re.fullmatch(r'python "(_agent_run(?:_[0-9a-f]{8})?\.py)"', params.get("command") or "")
        if not m:
            return {"stdout": "", "stderr": "mock companion refuses non-python shell commands",
                    "exit_code": 126}
        cwd = self._resolve(params.get("cwd") or ".")
        script = cwd / m.group(1)
        if not script.is_file():
            return {"stdout": "", "stderr": f"{m.group(1)} not found", "exit_code": 127}
        t = params.get("timeout") or 60
        try:
            r = await asyncio.to_thread(
                subprocess.run, [sys.executable, str(script)], capture_output=True, text=True,
                timeout=min(int(t), 120), cwd=str(cwd))
            return {"stdout": r.stdout or "", "stderr": r.stderr or "",
                    "exit_code": r.returncode}
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": f"timed out after {t}s", "exit_code": 124}


# ---------------------------------------------------------------------------
# Part 3: fixture (temp dirs, temp DBs, patches, TestClient app)
# ---------------------------------------------------------------------------

def _principal():
    from core.auth import Principal
    return Principal(id=UID, username=f"u{UID}", display_name="t", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys={"chat.use"})


def _seed_project(tdb, ws: Path):
    import time
    tdb.execute(
        "INSERT INTO projects (name, created_at, workspace_dir, user_id, device_id, device_name)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (PROJECT, time.time(), str(ws), UID, DEVICE, "mock device"))
    tdb.commit()
    pid = tdb.execute("SELECT id FROM projects WHERE name = ?", (PROJECT,)).fetchone()[0]
    tdb.execute("INSERT INTO sessions (project_id, title, created_at, user_id) VALUES (?, ?, ?, ?)",
                (pid, "eval", time.time(), UID))
    tdb.commit()
    return tdb.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()[0]


class MockEvalEnv:
    """Everything a POST /agent/run needs, all temporary. Use as a context manager per run
    so repeats cannot contaminate each other."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval_mock_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.fc = FakeCompanion(self.ws)
        self._patches = []
        self._saved = {}

    def _start_new(self):
        """Start patchers appended since the last call (each exactly once)."""
        start_at = self._saved.get("_started", 0)
        for p in self._patches[start_at:]:
            p.start()
        self._saved["_started"] = len(self._patches)

    def __enter__(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from core import agent_tools, route_log
        from core import auth_db
        from core.auth_db import common as auth_common
        from core.db import common as db_common
        from core.db import sessions as db_sessions
        from core.db import usage as db_usage
        from core.db import projects as db_projects
        from core import db as db_pkg
        from core import memory as memory_pkg
        from core import request_context as rc
        from core import companion_bridge
        from core.registry import bootstrap_builtin_tools
        from core.small_model import APP_CONFIG, small_models
        from core.state import state
        from core import context_budget
        from routes.agent.base import router as agent_router
        from routes.agent import permissions as agent_perms
        from core.deps import get_current_user

        # --- projects DB (temp file: ThreadLocalDB gives each thread its own connection,
        # so :memory: would be a different empty database per thread) ---
        proj_file = self.tmp / "projects.db"
        for mod in (db_common, db_sessions, db_projects, db_pkg):
            if hasattr(mod, "PROJECTS_DB_FILE"):
                self._patches.append(mock.patch.object(mod, "PROJECTS_DB_FILE", proj_file))
        self._start_new()
        real_tdb = db_common._init_projects_db()
        for mod in (db_common, db_sessions, db_projects, db_pkg, agent_tools):
            if hasattr(mod, "_projects_db"):
                self._patches.append(mock.patch.object(mod, "_projects_db", real_tdb))
        self._start_new()
        self._tdb = real_tdb
        self.sid = _seed_project(real_tdb, self.ws)

        # --- usage DB, which route_log shares (route_log does `from .db import _usage_db`).
        # One temp FILE via ThreadLocalDB serves both: per-thread connections to the same file,
        # so TestClient portal threads never trip sqlite's thread check. A separate raw
        # sqlite3 connection here would shadow the real handle and fail every write.
        usage_file = self.tmp / "usage.db"
        self._patches.append(mock.patch.object(db_usage, "USAGE_DB_FILE", usage_file))
        self._start_new()
        real_udb = db_usage._init_usage_db()
        for mod in (db_usage, db_pkg, route_log):
            if hasattr(mod, "_usage_db"):
                self._patches.append(mock.patch.object(mod, "_usage_db", real_udb))
        self._start_new()
        route_log._init()

        # --- auth DB (audit_log, agent_memory) ---
        auth_file = self.tmp / "auth.db"
        self._patches.append(mock.patch.object(auth_db, "AUTH_DB_FILE", auth_file))
        self._patches.append(mock.patch.object(auth_common, "AUTH_DB_FILE", auth_file))
        auth_db._auth_db = None
        auth_common._auth_db = None
        adb = auth_common.db()
        adb.execute("INSERT OR IGNORE INTO users (id, username, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)", (UID, f"u{UID}", time.time(), time.time()))
        adb.commit()

        # --- memory chunks DB ---
        # Reset EVERYTHING memory-shaped: store._db() prefers the package
        # _conn but falls back to its own module global, and _cached_entries
        # keys on a gen counter that a fresh temp DB restarts from zero - so a
        # previous run's connection or cache otherwise survives into this env
        # (slices re-run in one process during tuning sweeps would measure the
        # previous run's chunks). Direct assignments, not patchers: __exit__
        # cannot restore what was never patched, so reset on both ends.
        self._patches.append(mock.patch.object(memory_pkg, "MEMORY_DB_FILE", self.tmp / "memory.db"))
        from core.memory import cache as _cache_mod
        from core.memory import store as _store_mod
        memory_pkg._conn = None
        _store_mod._conn = None
        _cache_mod._set_cache({"gen": None, "entries": [], "lower": [],
                               "dim": None, "mat": None, "pos": None})

        # --- workspace pointers + request identity ---
        self._patches.append(mock.patch.dict(agent_tools._active_project,
                                             {f"{UID}:{DEVICE}": PROJECT}, clear=True))
        for bucket in ("_ws_changes", "_file_diffs"):
            if hasattr(agent_tools, bucket):
                self._patches.append(mock.patch.dict(getattr(agent_tools, bucket), {}, clear=True))
        from core.agent_tools import file_state
        for bucket in ("_read_sets", "_undo", "_verify"):
            self._patches.append(mock.patch.dict(getattr(file_state, bucket), {}, clear=True))
        self._start_new()

        # --- companion + shell policy (ask_first off is the legitimate user setting;
        # permission-gated flows have their own test files) ---
        self._saved_shell = dict((APP_CONFIG.get("capabilities") or {}).get("shell") or {})
        APP_CONFIG.setdefault("capabilities", {}).setdefault("shell", {})["ask_first"] = False
        # run_shell only exists once registered, and registration needs enabled: True.
        # Skill tools likewise. Web/media/browser/device registrations need network,
        # servers or devices, so they stay out: the harness is hermetic.
        APP_CONFIG["capabilities"]["shell"]["enabled"] = True
        APP_CONFIG.setdefault("capabilities", {})["skills"] = True
        self._patches.append(mock.patch.object(companion_bridge, "is_connected",
                                               lambda uid: uid == UID))
        self._patches.append(mock.patch.object(companion_bridge, "is_available",
                                               lambda uid, grace=0: uid == UID))
        self._patches.append(mock.patch.object(companion_bridge, "call", self.fc.call))
        self._patches.append(mock.patch.object(companion_bridge, "connection_info",
                                               lambda uid: {"device_id": DEVICE} if uid == UID else None))
        self._start_new()

        # --- LLM: scripted transport on both lanes + fake alive processes ---
        self._saved_state_client = state.client
        self._saved_state_proc = state.process
        state.client = mock_client()
        state.process = FakePopen()
        ex_inst = small_models.instances["executor"]
        self._saved_ex_client = ex_inst.client
        self._saved_ex_proc = ex_inst.process
        ex_inst.client = mock_client()
        ex_inst.process = FakePopen()
        self._ex_inst = ex_inst

        # --- context-budget probe cache: reset so every task measures fresh ---
        try:
            context_budget._reset()
        except Exception:
            for attr in ("_STATE", "_CACHE", "_state", "_cache"):
                if hasattr(context_budget, attr):
                    try:
                        getattr(context_budget, attr).clear()
                    except Exception:
                        pass

        bootstrap_builtin_tools()
        # the same registrations prod lifespan performs, minus the ones needing network,
        # servers or devices (web/media/browser/device stay unregistered: hermetic)
        from core.file_tools.chunking import register_file_tools
        from core.shell_tools import register_shell_tools
        from core.skills import register_skill_tools
        register_file_tools()
        register_shell_tools()
        register_skill_tools()

        # --- app with auth override (the override also sets request identity, since the
        # real get_current_user does that from session + device headers). It MUST be async:
        # Starlette runs sync dependencies in a threadpool with a copied contextvars context,
        # so sets made there are discarded with the copy and never reach the endpoint task.
        from core import deps as deps_mod

        async def _mock_user():
            rc.set_current_user(UID)
            rc.set_current_device(DEVICE)
            return _principal()

        app = FastAPI()
        app.include_router(agent_router)
        app.dependency_overrides[deps_mod.get_current_user] = _mock_user
        from fastapi.testclient import TestClient as TC
        self.client = TC(app)
        return self

    def __exit__(self, *exc):
        from core.small_model import APP_CONFIG
        from core.state import state
        # patchers in reverse; some were started twice above - stop() tolerates that
        seen = set()
        for p in reversed(self._patches):
            if id(p) in seen:
                continue
            seen.add(id(p))
            try:
                p.stop()
            except Exception:
                pass
        # Direct-assignment globals (never patched, so never auto-restored):
        # drop connections to the deleted tmp files and empty the search
        # cache, or the next env in this process inherits them.
        try:
            from core.memory import cache as _cache_mod
            from core.memory import store as _store_mod
            from core import memory as _memory_pkg
            for _conn_holder in (_store_mod, _memory_pkg):
                try:
                    _c = getattr(_conn_holder, "_conn", None)
                    if _c is not None:
                        _c.close()
                except Exception:
                    pass
                try:
                    _conn_holder._conn = None
                except Exception:
                    pass
            _cache_mod._set_cache({"gen": None, "entries": [], "lower": [],
                                   "dim": None, "mat": None, "pos": None})
        except Exception:
            pass
        try:
            APP_CONFIG["capabilities"]["shell"].update(self._saved_shell)
        except Exception:
            pass
        state.client = self._saved_state_client
        state.process = self._saved_state_proc
        try:
            self._ex_inst.client = self._saved_ex_client
            self._ex_inst.process = self._saved_ex_proc
        except Exception:
            pass
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)
        return False


# ---------------------------------------------------------------------------
# Part 4: runner (POST /agent/run, parse SSE, apply graders)
# ---------------------------------------------------------------------------

def _parse_sse(resp) -> dict:
    """The events the loop emits, in order. Mirrors eval_agent.run_live_task's parser."""
    rec = {"lanes": [], "tools": [], "results": {}, "texts": [], "thoughts": [],
           "done": None, "permission_requests": 0, "queued": 0}
    event = None
    for line in resp.iter_lines():
        line = line.strip() if isinstance(line, str) else line.decode("utf-8", "replace").strip()
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: ") and event:
            try:
                data = json.loads(line[6:])
            except Exception:
                continue
            if event == "lane":
                # lane events carry {"lane": ...} or model info; normalize both
                rec["lanes"].append(data.get("lane") or data.get("model") or "?")
            elif event == "tool_call":
                rec["tools"].append({"id": data.get("id"), "name": data.get("name"),
                                     "args": data.get("args") or {}})
            elif event == "tool_result":
                rec["results"][data.get("id")] = data
            elif event == "delta":
                rec["texts"].append(data.get("text") or "")
            elif event == "thought_delta":
                rec["thoughts"].append(data.get("delta") or data.get("text") or "")
            elif event == "permission_request":
                rec["permission_requests"] += 1
            elif event == "queued":
                rec["queued"] += 1
            elif event == "done":
                rec["done"] = data
    rec["final_text"] = "".join(rec["texts"])
    return rec


def _check(graded: dict, rec: dict, env: MockEvalEnv, task: dict) -> list:
    """Apply graders. Returns a list of problem strings (empty = pass)."""
    problems = []
    final = rec["final_text"]
    tool_names = [t["name"] for t in rec["tools"]]

    for want in graded.get("final_contains", []):
        if want.lower() not in final.lower():
            problems.append(f"final answer missing {want!r} (got {final[:160]!r})")
    for ban in graded.get("final_not_contains", []):
        if ban.lower() in final.lower():
            problems.append(f"final answer contains forbidden {ban!r}")
    for rel, needle in graded.get("file_contains", []):
        p = env.ws / rel
        if not p.is_file():
            problems.append(f"expected file {rel} was never created")
        elif needle.lower() not in p.read_text(encoding="utf-8", errors="replace").lower():
            problems.append(f"{rel} missing {needle!r}")
    for rel in graded.get("file_absent", []):
        if (env.ws / rel).exists():
            problems.append(f"{rel} should not exist")
    if "exact_tools" in graded:
        if tool_names != list(graded["exact_tools"]):
            problems.append(f"tool sequence {tool_names} != expected {list(graded['exact_tools'])}")
    for want in graded.get("expect_tools", []):
        if want not in tool_names:
            problems.append(f"missing tool: {want}")
    # ordered subsequence: each expected tool appears after the previous match
    if graded.get("tools_in_order"):
        pos, seq = 0, list(graded["tools_in_order"])
        for name in tool_names:
            if pos < len(seq) and name == seq[pos]:
                pos += 1
        if pos < len(seq):
            problems.append(f"tools {tool_names} do not contain {seq} in order")
    for ban in graded.get("forbid_tools", []):
        if ban in tool_names:
            problems.append(f"forbidden tool used: {ban}")
    # any tool_result event mentioning the text: proves an error/repair path executed
    # (truncated-args refusal, unknown-tool hint, validation message) and reached the model
    if graded.get("any_result_contains"):
        blobs = [json.dumps(r) for r in rec["results"].values()]
        for needle in graded["any_result_contains"]:
            if not any(needle.lower() in b.lower() for b in blobs):
                problems.append(f"no tool result mentioned {needle!r} "
                                f"(got {len(blobs)} result(s))")
    if graded.get("expect_escalation"):
        lanes = [str(l).lower() for l in rec["lanes"]]
        has_exec = any("exec" in l for l in lanes)
        has_main = any("main" in l for l in lanes)
        ordered = has_exec and has_main and (
            max(i for i, l in enumerate(lanes) if "main" in l) >
            min(i for i, l in enumerate(lanes) if "exec" in l))
        if not (has_main and (ordered or not has_exec)):
            problems.append(f"expected escalation to main lane, lanes={rec['lanes']}")
    if graded.get("expect_no_escalation"):
        if any("main" in str(l).lower() for l in rec["lanes"]):
            problems.append(f"unexpected main-lane use, lanes={rec['lanes']}")
    if "max_steps" in graded:
        if len(rec["tools"]) > graded["max_steps"]:
            problems.append(f"used {len(rec['tools'])} tool calls, budget was {graded['max_steps']}")
    if graded.get("no_permission_requests", True):
        if rec["permission_requests"]:
            problems.append(f"{rec['permission_requests']} permission modal(s) raised "
                            "(ask_first is off in the fixture; this should not happen)")
    if graded.get("no_pan_leak", True):
        from core import pan as _pan
        blob = final + "\n".join(
            str((env.ws / rel).read_text(encoding="utf-8", errors="replace"))
            for rel in graded.get("file_contains", []) for rel in [rel[0]]
            if (env.ws / rel).is_file())
        if _pan.contains_pan(blob):
            problems.append("card number leaked into answer or created files unmasked")
    if "expect_done_state" in graded:
        got = (rec["done"] or {}).get("state")
        if got != graded["expect_done_state"]:
            problems.append(f"done state {got!r} != {graded['expect_done_state']!r}")
    for custom in graded.get("custom", []):
        try:
            problem = custom(rec, env)
        except Exception as e:
            problem = f"custom grader raised {type(e).__name__}: {e}"
        if problem:
            problems.append(problem)
    return problems


def run_mock_task(task: dict, timeout_s: float = 120) -> dict:
    """One task, one fresh hermetic env. Returns a record dict."""
    rec = {"name": task["name"], "harness": "mock", "tools": [], "lanes": [],
           "final_len": 0, "ok": False, "soft": task.get("soft", False),
           "error": None, "duration_s": None, "problems": [], "repeats": 1,
           "extra_llm_calls": 0, "llm_requests": 0}
    script = task["script"]
    payload = {"messages": [{"role": "user", "content": task["prompt"]}],
               "mode": task.get("mode", "all-local"),
               "max_steps": task.get("max_steps", 12),
               "temperature": 0,
               "session_id": None}
    if task.get("plan"):
        payload["plan"] = True
    t0 = time.time()
    try:
        with MockEvalEnv() as env:
            _current["fifo"] = queue.Queue()
            for turn in script:
                _current["fifo"].put(dict(turn))
            _current["extra_calls"] = 0
            _current["requests"] = 0
            if task.get("seed_files"):
                for rel, content in task["seed_files"].items():
                    p = env.ws / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
            payload["session_id"] = env.sid
            headers = {"User-Agent": "A770NativeApp/1.0", "X-Device-Id": DEVICE}
            with env.client.stream("POST", "/agent/run", json=payload,
                                   headers=headers, timeout=timeout_s) as resp:
                if resp.status_code != 200:
                    rec["error"] = f"HTTP {resp.status_code}: {resp.read().decode()[:200]}"
                    return rec
                parsed = _parse_sse(resp)
            rec["extra_llm_calls"] = _current["extra_calls"]
            rec["llm_requests"] = _current["requests"]
            rec["tools"] = [t["name"] for t in parsed["tools"]]
            rec["lanes"] = parsed["lanes"]
            rec["final_len"] = len(parsed["final_text"])
            rec["done_state"] = (parsed["done"] or {}).get("state")
            rec["done_reason"] = (parsed["done"] or {}).get("reason")
            rec["problems"] = _check(task.get("grade", {}), parsed, env, task)
            rec["_final_text"] = parsed["final_text"]
            rec["ok"] = not rec["problems"]
    except Exception as e:
        import traceback
        rec["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=8)}"
    rec["duration_s"] = round(time.time() - t0, 1)
    return rec


def _plan_all_done(env) -> "str | None":
    """Custom grader: every plan item for the run's session must be done."""
    from core.db import db_get_plan_items
    try:
        items = db_get_plan_items(env.sid)
    except Exception as e:
        return f"could not read plan items: {type(e).__name__}: {e}"
    if not items:
        return "no plan items found for the session"
    pending = [i for i in items if (i.get("status") or "").lower() != "done"]
    if pending:
        return f"{len(pending)}/{len(items)} plan items not done: " + \
               ", ".join(str(i.get("text") or i.get("ord"))[:40] for i in pending)
    return None


# ---------------------------------------------------------------------------
# Part 5: task set (smoke first; more batches follow once green)
# ---------------------------------------------------------------------------

def T(tool_calls=None, content=None):
    if tool_calls is not None and content is not None:
        raise ValueError("turn is either text or tool calls")
    if tool_calls is not None:
        return {"tool_calls": tool_calls}
    return {"content": content}


def TC(name, **args):
    return {"name": name, "arguments": args}


MOCK_TASKS = [
    # smoke: one-list, one-answer. Calibrates the whole fixture before anything else.
    {"name": "smoke_list_files",
     "prompt": "List the files in the workspace root.",
     "script": [
         T(tool_calls=[TC("list_files", path=".")]),
         T(content="The workspace contains: notes.md."),
     ],
     "grade": {"expect_tools": ["list_files"], "final_contains": ["notes.md"]},
     "max_steps": 6,
     "seed_files": {"notes.md": "# notes\nhello\n"},
     "soft": False},

    # --- batch 1: core tool mechanics -------------------------------------
    {"name": "write_and_verify",
     "prompt": "Create eval_tmp/hello.py that prints 'hello eval', then run it to prove it works.",
     "script": [
         T(tool_calls=[TC("create_plan",
                           items=["write eval_tmp/hello.py", "run it to prove it works"])]),
         T(tool_calls=[TC("write_file", path="eval_tmp/hello.py",
                           content="print('hello eval')\n")]),
         T(tool_calls=[TC("update_plan_item", item=1, status="done")]),
         T(tool_calls=[TC("run_python", code="print('hello eval')\n")]),
         T(tool_calls=[TC("update_plan_item", item=2, status="done")]),
         T(content="It works: the script printed hello eval."),
     ],
     "grade": {"tools_in_order": ["create_plan", "write_file", "run_python"],
               "file_contains": [("eval_tmp/hello.py", "hello eval")],
               "final_contains": ["hello eval"]},
     "max_steps": 10,
     "soft": False},
    {"name": "compute_only",
     "prompt": "Use run_python to compute 137*29 and reply with just the number.",
     "script": [
         T(tool_calls=[TC("run_python", code="print(137*29)")]),
         T(content="3973"),
     ],
     "grade": {"expect_tools": ["run_python"], "forbid_tools": ["write_file"],
               "final_contains": ["3973"]},
     "max_steps": 6,
     "soft": False},
    {"name": "read_after_write",
     "prompt": "Write clinic.txt with the intake hours, then read it back and confirm.",
     "script": [
         T(tool_calls=[TC("write_file", path="clinic.txt",
                           content="Intake hours: Mon-Fri 9-5\n")]),
         T(tool_calls=[TC("read_file", path="clinic.txt")]),
         T(content="Confirmed - clinic.txt says intake hours are Mon-Fri 9-5."),
     ],
     "grade": {"tools_in_order": ["write_file", "read_file"],
               "final_contains": ["Mon-Fri 9-5"]},
     "max_steps": 8,
     "soft": False},
    {"name": "edit_flow",
     "prompt": "In app.py change the greeting from hi to hello.",
     "script": [
         T(tool_calls=[TC("read_file", path="app.py")]),
         T(tool_calls=[TC("edit_file", path="app.py",
                           old_string='greeting = "hi"',
                           new_string='greeting = "hello"')]),
         T(content="Done - greeting is now hello."),
     ],
     "grade": {"tools_in_order": ["read_file", "edit_file"],
               "file_contains": [("app.py", 'greeting = "hello"')],
               "final_contains": ["hello"]},
     "max_steps": 8,
     "seed_files": {"app.py": 'greeting = "hi"\nprint(greeting)\n'},
     "soft": False},
    {"name": "append_build",
     "prompt": "Create log.md with a header, then add two dated entries.",
     "script": [
         T(tool_calls=[TC("create_plan",
                           items=["write the header", "append both entries"])]),
         T(tool_calls=[TC("write_file", path="log.md", content="# Log\n")]),
         T(tool_calls=[TC("append_file", path="log.md", content="## 2026-01-01\nFirst.\n")]),
         T(tool_calls=[TC("append_file", path="log.md", content="## 2026-01-02\nSecond.\n")]),
         T(content="Log has the header and both entries."),
     ],
     "grade": {"tools_in_order": ["create_plan", "write_file", "append_file", "append_file"],
               "file_contains": [("log.md", "First."), ("log.md", "Second.")],
               "final_contains": ["both entries"]},
     "max_steps": 10,
     "soft": False},
    {"name": "grep_search",
     "prompt": "Which file defines the retry helper?",
     "script": [
         T(tool_calls=[TC("grep", pattern="def retry", path=".")]),
         T(content="The retry helper is defined in net.py."),
     ],
     "grade": {"expect_tools": ["grep"], "final_contains": ["net.py"]},
     "max_steps": 6,
     "seed_files": {"net.py": "def retry(fn, n=3):\n    return fn()\n",
                    "ui.py": "def draw():\n    pass\n",
                    "db.py": "def save(row):\n    pass\n"},
     "soft": False},
    {"name": "json_repair_truncated",
     "prompt": "Save a tiny config file r.py with x = 1 in it.",
     "script": [
         # arguments cut off mid-JSON: the whole-call rule refuses to run it
         {"tool_calls": [{"name": "write_file",
                           "arguments": '{"path": "r.py", "content": "x = 1'}]},
         T(tool_calls=[TC("write_file", path="r.py", content="x = 1\n")]),
         T(content="Saved r.py after resending the call in full."),
     ],
     "grade": {"file_contains": [("r.py", "x = 1")],
               "any_result_contains": ["Nothing was changed"],
               "final_contains": ["r.py"]},
     "max_steps": 8,
     "soft": False},
    {"name": "unknown_tool_graceful",
     "prompt": "List the workspace files.",
     "script": [
         T(tool_calls=[TC("frobnicate", path=".")]),
         T(tool_calls=[TC("list_files", path=".")]),
         T(content="Listed via list_files after the bad call was refused."),
     ],
     "grade": {"expect_tools": ["list_files"],
               "any_result_contains": ["unknown tool"],
               "final_contains": ["list_files"]},
     "max_steps": 8,
     "soft": False},
    {"name": "missing_arg_recovery",
     "prompt": "Read m.txt and tell me what it says.",
     "script": [
         # the model omits path entirely; the validator recovers it from the user's
         # question (the query-hint path in repair.py) - the grounded, legitimate guess
         T(tool_calls=[TC("read_file")]),
         T(content="m.txt says: done."),
     ],
     "grade": {"expect_tools": ["read_file"],
               "final_contains": ["done"]},
     "max_steps": 8,
     "seed_files": {"m.txt": "done\n"},
     "soft": False},
    {"name": "missing_arg_hard_error",
     "prompt": "Read the latest log and summarize it.",
     "script": [
         # neither the model nor the user names a file: no hint to recover from, so this
         # is the genuine hard error (read_file has no content-shape guess branch)
         T(tool_calls=[TC("read_file")]),
         T(tool_calls=[TC("read_file", path="latest.log")]),
         T(content="The log says: all systems nominal."),
     ],
     "grade": {"any_result_contains": ["path required"],
               "expect_tools": ["read_file"],
               "final_contains": ["nominal"]},
     "max_steps": 8,
     "seed_files": {"latest.log": "all systems nominal\n"},
     "soft": False},

    # --- batch 2: loop behaviour ------------------------------------------
    {"name": "escalation_on_repeat",
     "prompt": "Check the status in status.txt.",
     "script": [
         # loop_thrash shows the repeat guard moves to main around the 6th identical call.
         # A different call (the write below) breaks the streak, so the repeats must come
         # first and uninterrupted; the write then has to land on the main lane.
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("write_file", path="out.txt", content="status seen\n")]),
         T(content="Saw the status and recorded it."),
     ],
     "grade": {"expect_escalation": True,
               "file_contains": [("out.txt", "status seen")],
               "final_contains": ["recorded"]},
     "max_steps": 14,
     "seed_files": {"status.txt": "ok\n"},
     "soft": False},
    {"name": "loop_thrash_uses_budget",
     "prompt": "Keep checking status.txt until you are sure.",
     "script": [
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(tool_calls=[TC("read_file", path="status.txt")]),
         T(content="Checked repeatedly; it still says ok."),
     ],
     "grade": {"expect_tools": ["read_file"],
               "final_contains": ["ok"],
               "expect_done_state": "completed"},
     "max_steps": 8,
     "seed_files": {"status.txt": "ok\n"},
     "soft": False},
    {"name": "refusal_graceful",
     "prompt": "Delete everything in the workspace.",
     "script": [
         # a refusal ends the run on the spot with the refusal as the answer: no tools run,
         # nothing is touched, the second scripted turn is never consumed
         T(content="I cannot do that."),
         T(content="Understood - no files were touched."),
     ],
     "grade": {"forbid_tools": ["run_shell", "run_python", "write_file", "edit_file",
                                "append_file", "revert"],
               "final_contains": ["cannot"]},
     "max_steps": 6,
     "soft": False},
    {"name": "parallel_reads",
     "prompt": "Read a.txt and b.txt and tell me both values.",
     "script": [
         T(tool_calls=[TC("read_file", path="a.txt"), TC("read_file", path="b.txt")]),
         T(content="a is 1 and b is 2."),
     ],
     "grade": {"expect_tools": ["read_file"],
               "final_contains": ["1", "2"]},
     "max_steps": 6,
     "seed_files": {"a.txt": "1\n", "b.txt": "2\n"},
     "soft": False},
    {"name": "cutoff_write_recovery",
     "prompt": "Save c.py with x = 1 in it.",
     "script": [
         # cut off mid-arguments: must be discarded whole, never salvaged into a file
         {"tool_calls": [{"name": "write_file",
                           "arguments": '{"path": "c.py", "content": "x = 1, y = '}],
          "cut_off": True},
         T(tool_calls=[TC("write_file", path="c.py", content="x = 1\n")]),
         T(content="Saved c.py in full on the second try."),
     ],
     "grade": {"file_contains": [("c.py", "x = 1")],
               "final_contains": ["second try"]},
     "max_steps": 8,
     "soft": False},
    {"name": "insert_flow",
     "prompt": "Insert a middle line into poem.txt after line 1.",
     "script": [
         # insert_at_line refuses unless the file was read first in the session: line
         # numbers must come from read_file, not from the model's imagination
         T(tool_calls=[TC("read_file", path="poem.txt")]),
         T(tool_calls=[TC("insert_at_line", path="poem.txt", line=2,
                           text="middle\n")]),
         T(content="Inserted the middle line."),
     ],
     "grade": {"tools_in_order": ["read_file", "insert_at_line"],
               "file_contains": [("poem.txt", "middle")],
               "final_contains": ["Inserted"]},
     "max_steps": 8,
     "seed_files": {"poem.txt": "first\nlast\n"},
     "soft": False},
    {"name": "revert_flow",
     "prompt": "Change v.txt to version 2, then change your mind and undo it.",
     "script": [
         T(tool_calls=[TC("edit_file", path="v.txt",
                           old_string="version 1", new_string="version 2")]),
         T(tool_calls=[TC("revert", path="v.txt")]),
         T(content="Undone - v.txt is back at version 1."),
     ],
     "grade": {"tools_in_order": ["edit_file", "revert"],
               "file_contains": [("v.txt", "version 1")],
               "final_contains": ["version 1"]},
     "max_steps": 8,
     "seed_files": {"v.txt": "version 1\n"},
     "soft": False},

    # --- batch 3: safety ----------------------------------------------------
    {"name": "pan_masked_output",
     "prompt": "What card is on file for the customer?",
     "script": [
         T(content="The card on file is 4111 1111 1111 1111."),
     ],
     "grade": {"final_contains": ["[card ****1111]"],
               "final_not_contains": ["4111 1111 1111 1111"]},
     "max_steps": 6,
     "soft": False},
    {"name": "traversal_blocked",
     "prompt": "Save evil.py one level above the workspace.",
     "script": [
         T(tool_calls=[TC("write_file", path="../evil.py", content="x = 1\n")]),
         T(content="Refused - that path leaves the workspace."),
     ],
     "grade": {"any_result_contains": ["traverse", "outside", "workspace"],
               "final_contains": ["Refused"],
               "custom": [lambda rec, env: (
                   "evil.py escaped the workspace"
                   if (env.tmp / "evil.py").exists() else None)]},
     "max_steps": 6,
     "soft": False},
    {"name": "forbidden_name_blocked",
     "prompt": "Create a file called server_manager.py with a hello in it.",
     "script": [
         T(tool_calls=[TC("create_plan", items=["try the reserved name", "fall back"])]),
         T(tool_calls=[TC("write_file", path="server_manager.py",
                           content="print('hello')\n")]),
         T(tool_calls=[TC("write_file", path="hello.py", content="print('hello')\n")]),
         T(content="Used hello.py instead - that name is reserved."),
     ],
     "grade": {"any_result_contains": ["sandbox", "forbidden"],
               "file_contains": [("hello.py", "hello")],
               "file_absent": ["server_manager.py"],
               "final_contains": ["reserved"]},
     "max_steps": 10,
     "soft": False},
    {"name": "shell_refused_cleanly",
     "prompt": "Run echo hi in the shell.",
     "script": [
         T(tool_calls=[TC("run_shell", command="echo hi")]),
         T(content="The shell call was refused by the test double."),
     ],
     "grade": {"any_result_contains": ["refuses", "126"],
               "final_contains": ["refused"]},
     "max_steps": 6,
     "soft": False},

    # --- batch 4: memory, plans, sub-agents ---------------------------------
    {"name": "memory_write_read",
     "prompt": "Remember that I like short answers, then read it back to me.",
     "script": [
         T(tool_calls=[TC("memory_write", path="preferences.md",
                           content="- short answers\n")]),
         T(tool_calls=[TC("memory_read", path="preferences.md")]),
         T(content="You like short answers."),
     ],
     "grade": {"tools_in_order": ["memory_write", "memory_read"],
               "final_contains": ["short answers"]},
     "max_steps": 8,
     "soft": False},
    {"name": "memory_delete_allowed",
     "prompt": "Forget the editor fact I told you about.",
     "script": [
         T(tool_calls=[TC("memory_delete", path="facts.md")]),
         T(content="Forgot it."),
     ],
     "grade": {"expect_tools": ["memory_delete"],
               "final_contains": ["Forgot"]},
     "max_steps": 6,
     "soft": False},
    {"name": "memory_delete_refused",
     "prompt": "Tell me what you remember about the editor.",
     "script": [
         T(tool_calls=[TC("memory_delete", path="facts.md")]),
         T(tool_calls=[TC("memory_read", path="facts.md")]),
         T(content="I still remember: you use vim."),
     ],
     "grade": {"tools_in_order": ["memory_delete", "memory_read"],
               "final_contains": ["vim"]},
     "max_steps": 8,
     "soft": False},
    {"name": "subagent_delegate",
     "prompt": "Have a sub-agent read sub.txt and report back.",
     "script": [
         T(tool_calls=[TC("spawn_agent", task="read sub.txt and reply with its content")]),
         # the child runs its own loop consuming from the same script: its tool call,
         # then its answer, then the parent's final summary. The child's tool calls never
         # appear as parent-level tool_call events - only the spawn and its result do.
         T(tool_calls=[TC("read_file", path="sub.txt")]),
         T(content="sub.txt says: delegated ok."),
         T(content="The sub-agent reports: delegated ok."),
     ],
     "grade": {"expect_tools": ["spawn_agent"],
               "any_result_contains": ["sub-agent", "delegated ok"],
               "final_contains": ["delegated ok"]},
     "max_steps": 12,
     "seed_files": {"sub.txt": "delegated ok\n"},
     "soft": False},
    {"name": "plan_complete",
     "prompt": "Do the three setup steps and report when each is done.",
     "plan": False,
     "script": [
         T(tool_calls=[TC("create_plan", items=["step one", "step two", "step three"])]),
         T(tool_calls=[TC("write_file", path="one.txt", content="1\n")]),
         T(tool_calls=[TC("update_plan_item", item=1, status="done")]),
         T(tool_calls=[TC("write_file", path="two.txt", content="2\n")]),
         T(tool_calls=[TC("update_plan_item", item=2, status="done")]),
         T(tool_calls=[TC("write_file", path="three.txt", content="3\n")]),
         T(tool_calls=[TC("update_plan_item", item=3, status="done")]),
         T(content="All three steps are done."),
     ],
     "grade": {"tools_in_order": ["create_plan", "update_plan_item",
                                  "update_plan_item", "update_plan_item"],
               "file_contains": [("one.txt", "1"), ("two.txt", "2"), ("three.txt", "3")],
               "final_contains": ["three steps"],
               "custom": [lambda rec, env: _plan_all_done(env)]},
     "max_steps": 16,
     "soft": False},
    {"name": "plan_guard_violation",
     "prompt": "Work the two-step plan out of order.",
     "plan": True,
     "script": [
         T(tool_calls=[TC("create_plan", items=["first thing", "second thing"])]),
         T(tool_calls=[TC("update_plan_item", item=2, status="done")]),
         T(tool_calls=[TC("update_plan_item", item=1, status="done")]),
         T(tool_calls=[TC("update_plan_item", item=2, status="done")]),
         T(content="Finished in order after the guard complained."),
     ],
     "grade": {"any_result_contains": ["error"],
               "final_contains": ["in order"]},
     "max_steps": 12,
     "soft": False},
]


# ---------------------------------------------------------------------------
# Part 6: router slice (wiring only, not classifier quality)
# ---------------------------------------------------------------------------

def run_router_scenario(shortcut_on: bool) -> dict:
    """One run with a stubbed router. Asserts the tool_shortcut flag actually gates the
    shortcut path: with it on, the router's hit executes WITHOUT a main-loop LLM call for
    step 0; with it off (the shipped default), the main loop handles the turn.

    This measures wiring, not judgement - whether Needle/Laya picks well is eval_classifier's
    job. The question this answers for C7: does the shortcut path even work end to end.
    """
    from unittest import mock

    import routes.agent.run as runmod
    from core.small_model import APP_CONFIG

    rec = {"shortcut_on": shortcut_on, "ok": False, "problems": [],
           "tools": [], "lanes": [], "router_lane_seen": False, "llm_posts": 0}
    saved_router = dict(APP_CONFIG.get("router") or {})
    APP_CONFIG.setdefault("router", {})["tool_shortcut"] = shortcut_on
    router_calls = []

    def fake_route(query, tools):
        router_calls.append(query)
        return {"name": "list_files", "args": {"path": "."}, "confidence": 0.9,
                "reasoning": "listing requested"}

    task = {"name": "router_probe",
            "prompt": "What files are in the workspace?",
            "script": [T(content="The workspace contains notes.md.")],
            "grade": {}, "max_steps": 6,
            "seed_files": {"notes.md": "# notes\nhello\n"}}
    try:
        with MockEvalEnv() as env:
            _current["fifo"] = queue.Queue()
            for turn in task["script"]:
                _current["fifo"].put(dict(turn))
            _current["extra_calls"] = 0
            _current["requests"] = 0
            for rel, content in (task.get("seed_files") or {}).items():
                p = env.ws / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            payload = {"messages": [{"role": "user", "content": task["prompt"]}],
                       "mode": "all-local", "max_steps": 6, "temperature": 0,
                       "session_id": env.sid}
            with mock.patch.object(runmod, "router_available", lambda: True), \
                 mock.patch.object(runmod, "router_route", fake_route):
                with env.client.stream(
                        "POST", "/agent/run", json=payload,
                        headers={"User-Agent": "A770NativeApp/1.0",
                                 "X-Device-Id": DEVICE}, timeout=120) as resp:
                    if resp.status_code != 200:
                        rec["problems"].append(f"HTTP {resp.status_code}")
                        return rec
                    parsed = _parse_sse(resp)
            rec["tools"] = [t["name"] for t in parsed["tools"]]
            rec["lanes"] = parsed["lanes"]
            rec["router_lane_seen"] = any("needle" in str(l).lower()
                                          or "router" in str(l).lower()
                                          for l in parsed["lanes"])
            rec["final_text"] = parsed["final_text"]
            rec["llm_posts"] = _current["requests"]
            rec["router_calls"] = len(router_calls)
            if shortcut_on:
                if not router_calls:
                    rec["problems"].append("router was never consulted despite tool_shortcut=true")
                if "list_files" not in rec["tools"]:
                    rec["problems"].append("router hit never executed")
                if not rec["router_lane_seen"]:
                    rec["problems"].append("no router lane event in the stream")
                if "notes.md" not in rec["final_text"]:
                    rec["problems"].append("final answer missing notes.md")
            else:
                if router_calls:
                    rec["problems"].append("router consulted despite tool_shortcut=false")
                if rec["router_lane_seen"]:
                    rec["problems"].append("router lane event with the flag off")
            rec["ok"] = not rec["problems"]
    except Exception as e:
        import traceback
        rec["problems"].append(f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=6)}")
    finally:
        APP_CONFIG["router"] = saved_router
    return rec


# ---------------------------------------------------------------------------
# Part 6b: code-intel slice (R9 symbol search over a staged fixture repo)
# ---------------------------------------------------------------------------

CODE_FIXTURE = BASE_DIR / "tests" / "fixtures" / "code_repo"

# (kind, name, kwargs, expected_files). expected_files is the SET that must be
# covered: definitions return every match, callers every site. The fixture is
# fixed, the index is exact - so the gate is recall == 1.0, not a tripwire.
CODE_QUESTIONS = [
    ("def", "authenticate", {"path_hint": "auth.py"}, {"auth.py"}),
    ("def", "authenticate", {}, {"auth.py", "legacy_auth.py"}),
    ("def", "login", {}, {"auth.py"}),
    ("def", "lookupRecord", {}, {"helpers.js"}),
    ("def", "nonexistent_xyz", {}, set()),
    ("callers", "get_user", {}, {"routes.py", "admin.py"}),
    ("callers", "fetch_record", {}, {"db.py", "routes.py"}),
    ("outline", "auth.py", {}, {"authenticate", "AuthManager", "login", "logout"}),
]


def run_code_slice() -> dict:
    """Symbol lookup over the staged fixture: recall + latency, no server.

    Measures the index, not judgement: definitions (with path-hint narrowing),
    caller sets, file outlines. Deterministic on a fixed fixture, so recall
    below 1.0 is a regression, not noise. Latency is report-only (machine
    variance must never gate CI); the <50ms acceptance is verified from the
    printed p95 plus the scale probe below, not from the gate.
    """
    import time

    rec = {"n_questions": len(CODE_QUESTIONS), "recall": 0.0,
           "latency_ms_p95": None, "ok": False, "problems": []}
    if not CODE_FIXTURE.is_dir():
        rec["problems"].append(f"fixture missing: {CODE_FIXTURE}")
        return rec
    try:
        from core.code_intel import ast_index as idx
    except Exception as e:
        rec["problems"].append(f"code_intel unavailable: {type(e).__name__}: {e}")
        return rec
    lat, hits = [], 0
    try:
        for kind, name, kw, want in CODE_QUESTIONS:
            t0 = time.perf_counter()
            try:
                if kind == "def":
                    got = {h["file"] for h in idx.find_symbol_definition(
                        name, CODE_FIXTURE, **kw)}
                elif kind == "callers":
                    got = {c["file"] for c in idx.find_symbol_callers(
                        name, CODE_FIXTURE)}
                else:
                    got = {e["name"] for e in idx.get_file_outline(
                        CODE_FIXTURE / name)}
            except Exception as e:
                rec["problems"].append(f"{kind} {name} raised {type(e).__name__}: {e}")
                continue
            finally:
                lat.append((time.perf_counter() - t0) * 1000.0)
            if want <= got:
                hits += 1
            else:
                rec["problems"].append(
                    f"{kind} {name}: want {sorted(want)} got {sorted(got)}")
    except Exception as e:
        rec["problems"].append(f"slice failed: {type(e).__name__}: {e}")
        return rec
    lat.sort()
    rec["latency_ms_p95"] = round(lat[max(0, int(len(lat) * 0.95) - 1)], 2) if lat else None
    rec["recall"] = round(hits / len(CODE_QUESTIONS), 3)
    rec["ok"] = not rec["problems"] and rec["recall"] == 1.0
    if rec["recall"] < 1.0:
        rec["problems"].append(f"code recall = {rec['recall']} < 1.0")
    return rec


# ---------------------------------------------------------------------------
# Part 7: retrieval slice (fake embedder, real hybrid path) - next
# ---------------------------------------------------------------------------

import math as _math


def _bow_vocab(texts: list) -> list:
    toks = set()
    for t in texts:
        toks.update(re.findall(r"[a-z]{3,}", (t or "").lower()))
    return sorted(toks)


def _bow_vec(text: str, vocab: list, index: dict) -> list:
    v = [0.0] * len(vocab)
    for w in re.findall(r"[a-z]{3,}", (text or "").lower()):
        i = index.get(w)
        if i is not None:
            v[i] += 1.0
    n = _math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


RETRIEVAL_DOCS = [
    ("vacation-policy",
     "Vacation policy. Full-time employees accrue fifteen days of paid time off per year. "
     "Requests must be submitted at least two weeks ahead. Unused days roll over up to five."),
    ("refund-policy",
     "Refund policy. Returns are accepted within thirty days with a receipt. Final sale "
     "items cannot be refunded. Refunds go back to the original payment method."),
    ("deploy-runbook",
     "Deploy runbook. Deploy to staging first, then production. Run the database migration "
     "command before switching traffic. The rollback flag reverts to the previous release."),
    ("oncall-rotation",
     "On-call rotation. The primary and secondary rotate every one week. Escalate by paging "
     "the secondary after fifteen minutes without acknowledgement."),
    # Distractors: share vocabulary with a core doc but never contain any question's
    # answer. They turn the slice from a gating check into a ranking check: with
    # only 4 docs and k=6 everything fits, so ranking was unmeasured.
    ("sick-leave-policy",
     "Sick leave policy. Full-time employees receive ten sick days per year. Notify your "
     "manager before your shift starts. Unused sick days do not roll over and have no cash value."),
    ("shipping-policy",
     "Shipping policy. Orders ship within two business days. Returns are accepted within "
     "thirty days with a receipt. Shipping fees are refunded only for damaged items."),
    ("staging-checklist",
     "Staging checklist. Deploy to staging and run smoke tests first. Verify the database "
     "migration output before promoting. Never switch traffic on a Friday."),
    ("escalation-matrix",
     "Escalation matrix. Page the duty officer after five minutes. The secondary contacts "
     "the director if the primary stays silent. Incident reviews happen every Monday."),
    ("pto-cashout",
     "PTO cashout. Unused vacation days convert to pay at year end. Cashout requests need "
     "finance approval. Rollover days are excluded from cashout."),
]

# (question, expected_doc_title, needs_semantics). needs_semantics=True means the question
# can only be answered with real embedding quality (synonyms, stemming, inference): a
# bag-of-words stand-in cannot bridge "PTO" -> "paid time off", so those questions measure
# the FAKE, not the system. They are reported but never gated. The lexical subset measures
# what this slice owns: chunking, gating and ranking.
RETRIEVAL_QUESTIONS = [
    ("how many vacation days do I get per year?", "vacation-policy", False),
    ("when must time off requests be submitted?", "vacation-policy", False),
    ("do unused PTO days roll over?", "vacation-policy", True),
    ("how long do I have to return something?", "refund-policy", True),
    ("can I refund a final sale item?", "refund-policy", False),
    ("where does refund money go?", "refund-policy", False),
    ("what runs before switching traffic?", "deploy-runbook", False),
    ("how do I undo a bad deploy?", "deploy-runbook", False),
    ("which environment deploys first?", "deploy-runbook", True),
    ("how often does on-call rotate?", "oncall-rotation", False),
    ("who do I page when the primary is silent?", "oncall-rotation", True),
    ("how long before escalating to secondary?", "oncall-rotation", True),
    # Lexical neighbours of the distractors: each shares words with a core doc,
    # so only ranking (not gating) separates them. "receipts" is deliberately
    # plural: the corpus writes "receipt", and plural handling is lexical work
    # no embedder is needed to judge.
    ("how many paid days off do full-time employees accrue?", "vacation-policy", False),
    ("are receipts needed for returns?", "refund-policy", False),
    ("where do we deploy before production?", "deploy-runbook", False),
    ("how many minutes without acknowledgement before paging secondary?", "oncall-rotation", False),
    ("do sick days roll over?", "sick-leave-policy", False),
    ("how fast do orders ship?", "shipping-policy", False),
]


async def run_retrieval_slice(k: int = 6, embedder: str = "fake",
                            scoring: "dict | None" = None) -> dict:
    """Recall@k over the real chunk -> embed -> hybrid-search path, in two modes.

    Hybrid mode uses a deterministic bag-of-words stand-in embedder: it measures
    chunking, gating and ranking - NOT nomic quality. Lexical-only mode forces
    the embedder off, so the lexical scorer (word matching, title boosts, the
    min_lex gate) is measured with no embedding confound at all. The gate is on
    the lexical-only subset: deterministic, honest, and exactly what a code
    change to the scorer can break. Swapping in the real embedder later must
    only change the vectors, never the plumbing, which is what this pins.

    embedder="real" runs a third pass with cached nomic vectors
    (tests/.eval_vec_cache.json, written by scripts/embed_eval_corpus.py): the
    same chunks, the same search path, only the vectors change. Reported as
    hybrid_real_* (report-only, never gated: model quality is not a code
    property CI can hold). A stale cache (chunk params moved on, texts missing)
    fails the pass loudly instead of measuring the wrong vectors.

    scoring passes dev-time knob overrides through to search_knowledge_hybrid
    (tuning experiments only - winners get hardcoded, never CLI flags).
    """
    from unittest import mock

    from core import memory as memory_pkg
    from core.memory import indexing as indexing_mod
    from core.memory import search as search_mod
    from core.auth_db import knowledge_acl

    rec = {"k": k, "n_questions": len(RETRIEVAL_QUESTIONS), "hits": [],
           "recall_at_k": 0.0, "recall_at_1": 0.0, "mean_rank": None,
           "lexical_recall_at_k": 0.0, "paraphrase_recall_at_k": 0.0,
           "lexonly_recall_at_k": 0.0, "lexonly_hits": [],
           "hybrid_real_recall_at_k": None, "hybrid_real_recall_at_1": None,
           "hybrid_real_mean_rank": None,
           "hybrid_real_lexical_recall_at_k": None,
           "hybrid_real_paraphrase_recall_at_k": None,
           "fallout_at_k": 0.0, "lexical_fallout_at_k": 0.0,
           "paraphrase_fallout_at_k": 0.0, "lexonly_fallout_at_k": 0.0,
           "hybrid_real_fallout_at_k": None,
           "hybrid_real_lexical_fallout_at_k": None,
           "hybrid_real_paraphrase_fallout_at_k": None,
           "ok": False, "problems": []}

    def _load_real_vecs():
        """Cached nomic vectors, or a problem string. Never falls back."""
        import hashlib as _hl
        cache_file = BASE_DIR / "tests" / ".eval_vec_cache.json"
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception as e:
            return None, f"real-embedder cache unreadable ({cache_file.name}): {e}"
        from core.memory.constants import CHUNK_CHARS, CHUNK_OVERLAP, CHUNK_SNAP, _chunk_text
        if (cache.get("chunk_chars") != CHUNK_CHARS
                or cache.get("chunk_overlap") != CHUNK_OVERLAP
                or cache.get("chunk_snap") != CHUNK_SNAP):
            return None, ("real-embedder cache is stale (chunk params moved on: "
                           "re-run scripts/embed_eval_corpus.py)")
        want = set()
        for _, body in RETRIEVAL_DOCS:
            want.update(_chunk_text(body))
        want.update(q for q, _, _ in RETRIEVAL_QUESTIONS)
        vecs = cache.get("vecs") or {}
        missing = [t[:40] for t in want if _hl.sha1(t.encode()).hexdigest() not in vecs]
        if missing:
            return None, (f"real-embedder cache missing {len(missing)} texts "
                           f"(e.g. {missing[0]!r}): re-run scripts/embed_eval_corpus.py")
        return ({_hl.sha1(t.encode()).hexdigest(): vecs[_hl.sha1(t.encode()).hexdigest()]
                 for t in want}, None)

    async def ask_all(tag):
        ranks, lex_ranks, par_ranks = [], [], []
        fallouts, lex_fo, par_fo = [], [], []
        skw = dict(scoring or {})
        for qi, (q, want_title, para) in enumerate(RETRIEVAL_QUESTIONS):
            try:
                hits = await search_mod.search_knowledge_hybrid(
                    q, k=k, allowed_knowledge_source_ids=allowed, **skw)
            except Exception as e:
                rec["problems"].append(f"{tag} q{qi} raised {type(e).__name__}: {e}")
                continue
            rank = None
            for i, h in enumerate(hits):
                if (h.get("title") or "") == want_title:
                    rank = i + 1
                    break
            # Fallout@k: retrieved non-relevant / retrieved. Empty hits score
            # 0.0 (nothing wrong came back; recall already punishes the miss).
            # This is the counterweight to recall: without it every tuning step
            # can "win" by loosening gates until the context is all noise.
            wrong = sum(1 for h in hits if (h.get("title") or "") != want_title)
            fo = round(wrong / len(hits), 3) if hits else 0.0
            ranks.append(rank)
            fallouts.append(fo)
            if para:
                par_ranks.append(rank)
                par_fo.append(fo)
            else:
                lex_ranks.append(rank)
                lex_fo.append(fo)
            rec["hits"].append({"mode": tag, "q": q, "want": want_title, "paraphrase": para,
                                "got": [h.get("title") for h in hits],
                                "rank": rank, "fallout": fo})
        return ranks, lex_ranks, par_ranks, fallouts, lex_fo, par_fo

    def _mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else 0.0

    with MockEvalEnv():
        texts = [d for _, d in RETRIEVAL_DOCS] + [q for q, _, _ in RETRIEVAL_QUESTIONS]
        vocab = _bow_vocab(texts)
        index = {w: i for i, w in enumerate(vocab)}

        async def fake_embed(texts_in):
            return [_bow_vec(t, vocab, index) for t in texts_in]

        async def no_embed(texts_in):
            return None

        real_vecs, real_problem = (None, None)
        if embedder == "real":
            real_vecs, real_problem = _load_real_vecs()
            if real_problem:
                rec["problems"].append(real_problem)

        async def real_embed(texts_in):
            import hashlib as _hl2
            return [real_vecs[_hl2.sha1(t.encode()).hexdigest()] for t in texts_in]

        id_by_title = {}
        for title, body in RETRIEVAL_DOCS:
            sid = knowledge_acl.create_knowledge_source(title, "text", "eval")
            knowledge_acl.update_knowledge_source_status(sid, "ready")
            id_by_title[title] = sid
        allowed = set(id_by_title.values())

        # The index phase uses the mode's embedder: fake BoW for the hermetic
        # pass, cached nomic for the real pass. Same chunks, same search path -
        # only the vectors differ, which is the whole point of the exercise.
        index_embed = real_embed if embedder == "real" and real_vecs else fake_embed
        with mock.patch.object(memory_pkg, "_embed_texts", index_embed), \
             mock.patch.object(indexing_mod, "_embed_texts", index_embed), \
             mock.patch.object(search_mod, "_embed_texts", index_embed):
            for title, body in RETRIEVAL_DOCS:
                n = await indexing_mod.index_knowledge_source(id_by_title[title], body)
                if n < 1:
                    rec["problems"].append(f"{title} produced no chunks")
            if embedder == "real" and real_vecs:
                tag = "real"
            else:
                tag = "hybrid"
            ranks, lex_ranks, par_ranks, fallouts, lex_fo, par_fo = await ask_all(tag)
        answered = [r for r in ranks if r is not None]
        lex_ok = [r for r in lex_ranks if r is not None]
        par_ok = [r for r in par_ranks if r is not None]
        if tag == "real":
            rec["hybrid_real_recall_at_k"] = round(len(answered) / len(RETRIEVAL_QUESTIONS), 3)
            rec["hybrid_real_recall_at_1"] = round(
                sum(1 for r in answered if r == 1) / len(RETRIEVAL_QUESTIONS), 3)
            rec["hybrid_real_mean_rank"] = round(sum(answered) / len(answered), 2) if answered else None
            rec["hybrid_real_lexical_recall_at_k"] = round(len(lex_ok) / max(1, len(lex_ranks)), 3)
            rec["hybrid_real_paraphrase_recall_at_k"] = round(len(par_ok) / max(1, len(par_ranks)), 3)
            rec["hybrid_real_fallout_at_k"] = _mean(fallouts)
            rec["hybrid_real_lexical_fallout_at_k"] = _mean(lex_fo)
            rec["hybrid_real_paraphrase_fallout_at_k"] = _mean(par_fo)
            # Backfill the legacy keys from the fake pass? No - they measure the
            # stand-in. In real mode they stay 0.0-shaped-but-unset: keep them
            # None-shaped via the rec defaults above (0.0) is wrong too. The
            # honest shape: hybrid keys describe the fake pass, which did not
            # run. Reset them to None so summaries/baselines never compare them.
            rec["recall_at_k"] = None
            rec["recall_at_1"] = None
            rec["mean_rank"] = None
            rec["lexical_recall_at_k"] = None
            rec["paraphrase_recall_at_k"] = None
            rec["fallout_at_k"] = None
            rec["lexical_fallout_at_k"] = None
            rec["paraphrase_fallout_at_k"] = None
        else:
            rec["recall_at_k"] = round(len(answered) / len(RETRIEVAL_QUESTIONS), 3)
            rec["recall_at_1"] = round(sum(1 for r in answered if r == 1) / len(RETRIEVAL_QUESTIONS), 3)
            rec["mean_rank"] = round(sum(answered) / len(answered), 2) if answered else None
            rec["lexical_recall_at_k"] = round(len(lex_ok) / max(1, len(lex_ranks)), 3)
            rec["paraphrase_recall_at_k"] = round(len(par_ok) / max(1, len(par_ranks)), 3)
            rec["fallout_at_k"] = _mean(fallouts)
            rec["lexical_fallout_at_k"] = _mean(lex_fo)
            rec["paraphrase_fallout_at_k"] = _mean(par_fo)

        # Lexical-only pass over the same indexed chunks: embedder forced off,
        # so min_lex gating + word matching + title boosts stand alone.
        with mock.patch.object(memory_pkg, "_embed_texts", no_embed), \
             mock.patch.object(indexing_mod, "_embed_texts", no_embed), \
             mock.patch.object(search_mod, "_embed_texts", no_embed):
            lx_ranks, lx_lex, _lx_par, _lx_fo, lx_fo_lex, _lx_fo_par = await ask_all("lexonly")
        lx_ok = [r for r in lx_lex if r is not None]
        rec["lexonly_recall_at_k"] = round(len(lx_ok) / max(1, len(lx_lex)), 3)
        rec["lexonly_fallout_at_k"] = _mean(lx_fo_lex)
        rec["lexonly_hits"] = [
            {"q": q, "want": want, "rank": r}
            for (q, want, para), r in zip(RETRIEVAL_QUESTIONS, lx_ranks) if not para]
        # Gate on the lexical-only subset: it is what this slice can honestly
        # measure. 0.6 is a tripwire, not a target - deterministic harness, so
        # any code change that breaks chunking/gating/ranking drops this sharply.
        # The paraphrase subset is reported for the day the real embedder plugs
        # in; gating on it now would test the stand-in, not the system.
        rec["ok"] = not rec["problems"] and rec["lexonly_recall_at_k"] >= 0.6
        if rec["lexonly_recall_at_k"] < 0.6:
            rec["problems"].append(
                f"lexical-only recall@{k} = {rec['lexonly_recall_at_k']} < 0.6")
    return rec


# ---------------------------------------------------------------------------
# Part 8: repeats, Wilson CI, baseline
# ---------------------------------------------------------------------------

def wilson_ci(passes: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score interval for a pass rate. No scipy dependency.

    With deterministic scripts every task scores 1.0 or 0.0 and the interval is degenerate -
    which is correct: repeats here measure the LOOP's determinism given fixed model behaviour
    (did my change alter the outcome?), not model capability variance. Live variance belongs
    to --live with real models."""
    if n <= 0:
        return (0.0, 0.0)
    p = passes / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    spread = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (round(max(0.0, (center - spread) / denom), 3),
            round(min(1.0, (center + spread) / denom), 3))


def run_mock_suite(repeats: int = 3, task_filter: "list | None" = None,
                   include_retrieval: bool = True, include_router: bool = True,
                   retrieval_embedder: str = "fake", include_code: bool = True,
                   progress=None) -> dict:
    """The whole mock suite. Every repeat gets a fresh hermetic env."""
    tasks = [t for t in MOCK_TASKS if task_filter is None or t["name"] in task_filter]
    suite = {"harness": "mock", "repeats": repeats, "tasks": {}, "aggregate": {},
             "retrieval": None, "router": None, "code": None}
    passes = 0
    for task in tasks:
        runs = []
        for _ in range(max(1, repeats)):
            rec = run_mock_task(task)
            runs.append({"ok": rec["ok"], "problems": rec["problems"],
                         "tools": rec["tools"], "lanes": rec["lanes"],
                         "duration_s": rec["duration_s"], "error": rec["error"],
                         "done_state": rec.get("done_state"),
                         "extra_llm_calls": rec["extra_llm_calls"]})
            if progress:
                progress(task["name"], rec["ok"])
        n_ok = sum(1 for r in runs if r["ok"])
        lo, hi = wilson_ci(n_ok, len(runs))
        suite["tasks"][task["name"]] = {
            "pass_rate": round(n_ok / len(runs), 3), "n": len(runs),
            "ci95": [lo, hi], "runs": runs,
            "stable": n_ok in (0, len(runs)),
        }
        passes += n_ok
    total = len(tasks) * max(1, repeats)
    suite["aggregate"] = {"pass_rate": round(passes / total, 3) if total else 0.0,
                          "passes": passes, "total": total,
                          "ci95": list(wilson_ci(passes, total)) if total else [0.0, 0.0]}
    if include_retrieval:
        import asyncio
        suite["retrieval"] = asyncio.run(
            run_retrieval_slice(embedder=retrieval_embedder))
    if include_router:
        on = run_router_scenario(True)
        off = run_router_scenario(False)
        suite["router"] = {
            "shortcut_on": {"ok": on["ok"], "problems": on["problems"],
                            "tools": on["tools"], "router_calls": on.get("router_calls")},
            "shortcut_off": {"ok": off["ok"], "problems": off["problems"],
                             "tools": off["tools"], "router_calls": off.get("router_calls")},
            "ok": on["ok"] and off["ok"],
        }
    if include_code:
        suite["code"] = run_code_slice()
    return suite


BASELINE_FILE = BASE_DIR / "tests" / "eval_baseline.json"


def _summarize_suite(suite: dict, offline: "list | None" = None) -> dict:
    """The comparable core: per-task pass rates + retrieval + router + aggregate.

    Skipped slices stay None (never {"ok": None}): compare_baseline treats absence as
    "not measured", and materializing a dict would read as a measured failure.

    `offline` is the eval_agent offline-check records ([{name, status}]) when that suite
    ran in the same invocation. A pre-existing FAIL (e.g. compact_endpoint) is recorded
    as-is: the gate fires only on PASS -> FAIL transitions, never on a standing failure.
    """
    out = {
        "tasks": {name: {"pass_rate": t["pass_rate"], "n": t["n"]}
                  for name, t in suite.get("tasks", {}).items()},
        "aggregate_rate": (suite.get("aggregate") or {}).get("pass_rate"),
        "retrieval": None,
        "router": None,
        "code": None,
    }
    if suite.get("retrieval") is not None:
        out["retrieval"] = {k: suite["retrieval"].get(k)
                            for k in ("lexical_recall_at_k", "paraphrase_recall_at_k",
                                      "lexonly_recall_at_k",
                                      "recall_at_k", "recall_at_1", "mean_rank",
                                      "fallout_at_k", "lexical_fallout_at_k",
                                      "paraphrase_fallout_at_k",
                                      "lexonly_fallout_at_k",
                                      "hybrid_real_recall_at_k", "hybrid_real_recall_at_1",
                                      "hybrid_real_mean_rank",
                                      "hybrid_real_lexical_recall_at_k",
                                      "hybrid_real_paraphrase_recall_at_k",
                                      "hybrid_real_fallout_at_k",
                                      "hybrid_real_lexical_fallout_at_k",
                                      "hybrid_real_paraphrase_fallout_at_k")
                            if suite["retrieval"].get(k) is not None}
    if suite.get("router") is not None:
        out["router"] = {"ok": suite["router"].get("ok")}
    out["code"] = None
    if suite.get("code") is not None:
        out["code"] = {"recall": suite["code"].get("recall")}
    out["offline"] = None
    if offline is not None:
        out["offline"] = {r["name"]: r["status"] for r in offline}
    return out


def compare_baseline(current: dict, baseline: dict, tolerance: float = 0.001) -> list:
    """Diff current results against the committed baseline. Returns problem strings.

    `current` is a _summarize_suite() summary, NOT a raw suite - summarizing twice drops
    the aggregate and offline sections (they live under different keys post-summarize),
    which used to disable those two checks silently. Callers pass the summary.
    Fails on: any task going pass->fail (pass_rate drop beyond tolerance), retrieval
    lexical-only or hybrid-real recall dropping, retrieval fallout RISING (more
    noise per hit than the baseline tolerated), the router slice breaking, the
    code slice recall dropping below 1.0, aggregate drop beyond tolerance.
    New tasks absent from the baseline are informational only - they cannot regress
    something that was never measured.
    """
    problems = []
    cur, base = current, baseline
    cur_tasks = cur.get("tasks") or {}
    for name, t in cur_tasks.items():
        if name not in base.get("tasks", {}):
            continue
        was = base["tasks"][name]["pass_rate"]
        if t["pass_rate"] < was - tolerance:
            problems.append(f"REGRESSION {name}: {was} -> {t['pass_rate']}")
    for key in ("lexonly_recall_at_k",
                "hybrid_real_recall_at_k", "hybrid_real_recall_at_1",
                "hybrid_real_lexical_recall_at_k",
                "hybrid_real_paraphrase_recall_at_k"):
        if cur.get("retrieval") is None:
            continue  # slice skipped (--no-retrieval): absence is not a regression
        was, now = (base.get("retrieval") or {}).get(key), (cur.get("retrieval") or {}).get(key)
        tol = tolerance if not key.startswith("hybrid_real_") else max(tolerance, 0.01)
        if was is not None and now is not None and now < was - tol:
            problems.append(f"REGRESSION retrieval.{key}: {was} -> {now}")
    for key in ("fallout_at_k", "lexical_fallout_at_k", "paraphrase_fallout_at_k",
                "lexonly_fallout_at_k",
                "hybrid_real_fallout_at_k",
                "hybrid_real_lexical_fallout_at_k",
                "hybrid_real_paraphrase_fallout_at_k"):
        if cur.get("retrieval") is None:
            continue
        was, now = (base.get("retrieval") or {}).get(key), (cur.get("retrieval") or {}).get(key)
        # Real-vector keys carry embedding wobble: same texts re-embedded by a
        # fresh server process measured ~0.005 of aggregate drift (R5), while
        # same-process repeats are bit-identical. 0.01 covers the wobble with
        # margin and still catches real degradation; fake/lexonly stay at the
        # strict tolerance because they are fully deterministic.
        tol = tolerance if not key.startswith("hybrid_real_") else max(tolerance, 0.01)
        if was is not None and now is not None and now > was + tol:
            problems.append(f"REGRESSION retrieval.{key}: {was} -> {now}")
    if cur.get("router") is None:
        pass  # slice skipped (--no-router): absence is not a regression
    elif (base.get("router") or {}).get("ok") and not (cur.get("router") or {}).get("ok", True):
        problems.append("REGRESSION router slice: was ok, now failing")
    if cur.get("code") is not None:
        # Deterministic fixture, exact index: recall below 1.0 is breakage.
        # Latency is report-only (machine variance must never gate CI).
        was, now = (base.get("code") or {}).get("recall"), (cur.get("code") or {}).get("recall")
        if was is not None and now is not None and now < was - tolerance:
            problems.append(f"REGRESSION code.recall: {was} -> {now}")
    for name, was in (base.get("offline") or {}).items():
        now = (cur.get("offline") or {}).get(name)
        if now is None or was == "SKIP" or now == "SKIP":
            continue  # not measured this time, or environmental either time
        if was == "PASS" and now == "FAIL":
            problems.append(f"REGRESSION offline check {name}: PASS -> FAIL")
    was_agg, now_agg = base.get("aggregate_rate"), cur.get("aggregate_rate")
    if was_agg is not None and now_agg is not None and now_agg < was_agg - 0.05:
        problems.append(f"REGRESSION aggregate: {was_agg} -> {now_agg}")
    return problems
