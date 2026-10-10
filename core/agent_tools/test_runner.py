"""run_tests: run the project's test suite and hand back a short failure summary.

Detection reads the project's root on the user's device (fs.list / fs.read through the companion) and picks
pytest, unittest, npm test, go test or cargo test. The command is a pure function of that detection plus an
optional `path`, so the agent loop can compute it BEFORE the call, show it in the same approval card as a
run_shell command, and approve exactly that line; the tool then runs it through run_shell's own gate, which
refuses anything that does not match what the loop approved.

The summary is the point: a raw pytest run is thousands of tokens of progress dots and tracebacks. The model
gets the counts, the failing test ids, and one error line plus a location per failure.
"""

import json
import re
import time
from typing import Callable, Optional

from .. import companion_bridge

MAX_FAILURES = 8
MAX_LINE = 220
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 900
_CACHE_TTL_S = 60

# characters allowed in the `path` argument: file/dir paths and test node ids (path::Class::test[param]).
# No spaces, quotes or shell metacharacters, so it can be appended to a command line verbatim.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./\\:\[\]-]+$")

_NPM_PLACEHOLDER = re.compile(r"no test specified", re.I)
_PY_MARKERS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "requirements.txt",
               "requirements-dev.txt", "conftest.py")
MANIFESTS = ("package.json",) + _PY_MARKERS


class NoRunner(Exception):
    """No test runner could be detected, or the request cannot be turned into a command."""


# ---------------------------------------------------------------- detection

def _safe_path(path: str) -> str:
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return ""
    if not _SAFE_PATH.match(p) or p.startswith("-") or ".." in p.split("/") or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise NoRunner("path must be a relative test file, folder or test id inside the workspace "
                       "(letters, digits, . _ / : [ ] -)")
    return p


def detect(files: set, texts: dict) -> Optional[dict]:
    """Pick a runner from the root listing and manifest texts. {runner, base} or None."""
    pkg = texts.get("package.json")
    py_text = "\n".join(texts.get(m, "") for m in _PY_MARKERS)
    has_py_tests = (any(f.startswith(("tests/", "test/")) and f.endswith(".py") for f in files)
                    or any(re.match(r"^test_.*\.py$|^.*_test\.py$", f) for f in files))
    pytest_ok = ("pytest.ini" in files or "conftest.py" in files or "[tool.pytest" in py_text
                 or re.search(r"(?im)^\s*pytest\b|^\[pytest\]|^\[tool:pytest\]", py_text) is not None)
    if pytest_ok:
        return {"runner": "pytest", "base": "python -m pytest -q --tb=short -rf --maxfail=15"}

    script = ""
    if pkg:
        try:
            script = str((json.loads(pkg).get("scripts") or {}).get("test") or "")
        except (ValueError, AttributeError):
            script = ""
    if script and not _NPM_PLACEHOLDER.search(script):
        if re.search(r"\bvitest\b", script) and not re.search(r"\b(run|--run)\b", script):
            return {"runner": "vitest", "base": "npx --no-install vitest run"}      # bare vitest would watch
        return {"runner": "npm", "base": "npm test --silent"}
    if has_py_tests:
        has_dir = any(f.startswith("tests/") for f in files)
        return {"runner": "unittest",
                "base": "python -m unittest discover -s tests" if has_dir else "python -m unittest discover"}
    if "go.mod" in files:
        return {"runner": "go", "base": "go test"}
    if "Cargo.toml" in files:
        return {"runner": "cargo", "base": "cargo test"}
    return None


def build_command(found: dict, path: str = "") -> str:
    """The exact command line for a detected runner and an optional (already validated) path."""
    base, runner = found["base"], found["runner"]
    if not path:
        return base + (" ./..." if runner == "go" else "")
    if runner == "unittest":
        if path.endswith(".py"):
            return f"python -m unittest {path[:-3].replace('/', '.')}"
        return f"python -m unittest discover -s {path}"
    if runner == "npm":
        return f"{base} -- {path}"
    if runner == "vitest":
        return f"{base} {path}"
    if runner == "go":
        if path.endswith(".go"):
            folder = path.rsplit("/", 1)[0] if "/" in path else "."
            return f"{base} ./{folder}"
        return f"{base} ./{path.rstrip('/')}/..."
    if runner == "cargo":
        return f"{base} {path}"
    return f"{base} {path}"


# --------------------------------------------------------------- the device

_detect_cache: dict = {}


def _cb():
    import sys
    return getattr(sys.modules.get("core.agent_tools"), "companion_bridge", companion_bridge)


async def detect_for_workspace(uid: int, root: str) -> Optional[dict]:
    """Detection for the user's workspace, cached briefly so the loop's pre-approval pass and the tool agree."""
    key = (uid, root)
    hit = _detect_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    cb = _cb()
    files: set = set()
    for pat in ("*", "tests/*", "test/*"):
        try:
            got = await cb.call(uid, "fs.list", {"root": root, "pattern": pat})
        except Exception:
            continue
        files.update(got.get("files") or [])
    texts: dict = {}
    base = root.rstrip("/").rstrip("\\")
    for name in MANIFESTS:
        if name in files:
            try:
                data = await cb.call(uid, "fs.read", {"path": base + "/" + name})
            except Exception:
                continue
            if isinstance(data.get("content"), str):
                texts[name] = data["content"]
    found = detect(files, texts)
    _detect_cache[key] = (now, found)
    return found


def clear_cache() -> None:
    _detect_cache.clear()


async def command_for(args: dict) -> Optional[str]:
    """The command run_tests would run for these args, or None when there is none (the tool then explains why).
    The agent loop calls this to put the command through the same approval as run_shell."""
    from . import active_workspace, _remote_uid
    try:
        path = _safe_path(str((args or {}).get("path") or ""))
        found = await detect_for_workspace(_remote_uid(), str(active_workspace()))
    except Exception:
        return None
    return build_command(found, path) if found else None


# ------------------------------------------------------------------ parsing

def split_result(text: str) -> tuple:
    """(exit_code, stdout, stderr) from run_shell's formatted result; exit_code None when unparseable."""
    m = re.match(r"exit code (-?\d+|None)", text or "")
    code = int(m.group(1)) if m and m.group(1) != "None" else None
    out = err = ""
    if "--- stdout ---\n" in text:
        rest = text.split("--- stdout ---\n", 1)[1]
        out, _, err = rest.partition("\n--- stderr ---\n")
    elif "--- stderr ---\n" in text:
        err = text.split("--- stderr ---\n", 1)[1]
    return code, out, err


def _trim(s: str, n: int = MAX_LINE) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


_PYTEST_SUMMARY = re.compile(r"((?:\d+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?)(?:, )?)+) in [\d.]+s")


def _parse_pytest(text: str) -> tuple:
    summary = ""
    for line in text.splitlines():
        m = _PYTEST_SUMMARY.search(line)
        if m:
            summary = m.group(0)
    fails = []
    for line in text.splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            fails.append(_trim(line))
    # short tracebacks: header ____ name ____, then "E   message" lines and a "file.py:NN:" location
    detail: dict = {}
    name = None
    for line in text.splitlines():
        h = re.match(r"^_{3,} (.+?) _{3,}$", line)
        if h:
            name = h.group(1)
            detail[name] = {"e": [], "loc": ""}
        elif name:
            if line.startswith("E ") and len(detail[name]["e"]) < 2:
                detail[name]["e"].append(line[1:].strip())
            m = re.match(r"^(\S+\.py):(\d+):", line)
            if m:
                detail[name]["loc"] = f"{m.group(1)}:{m.group(2)}"
    out = []
    for f in fails:
        base = f.split(" - ", 1)[0].split("::")[-1].split("[")[0]
        d = next((v for k, v in detail.items() if k.endswith(base)), None)
        extra = f" ({d['loc']})" if d and d["loc"] else ""
        out.append(f + extra)
    return summary, out


def _parse_unittest(text: str) -> tuple:
    ran = re.search(r"^Ran (\d+) tests? in ([\d.]+s)", text, re.M)
    res = re.search(r"^(OK[^\n]*|FAILED[^\n]*)$", text, re.M)
    summary = (f"{ran.group(0)}; {res.group(1)}" if ran and res else (ran.group(0) if ran else ""))
    out = []
    blocks = re.split(r"^={50,}\n", text, flags=re.M)[1:]
    for b in blocks:
        b = re.split(r"\n-{50,}\nRan \d+ tests?", b)[0]      # the last block runs on into the result summary
        head = re.match(r"(FAIL|ERROR): (.+)", b)
        if not head:
            continue
        lines = [ln for ln in b.splitlines() if ln.strip()]
        loc = ""
        for ln in lines:
            m = re.search(r'File "(.+?)", line (\d+)', ln)
            if m:
                loc = f"{m.group(1).replace(chr(92), '/').split('/')[-1]}:{m.group(2)}"
        last = lines[-1].strip() if lines else ""
        out.append(_trim(f"{head.group(1)} {head.group(2)} - {last}") + (f" ({loc})" if loc else ""))
    return summary, out


def _parse_js(text: str) -> tuple:
    summary = ""
    for line in text.splitlines():
        if re.match(r"^\s*(Tests?|Test Files):?\s", line):
            summary += (" " if summary else "") + _trim(line, 120)
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*(FAIL|●|✗|×)\s", line) and not line.strip().startswith("● Console"):
            msg = next((m.strip() for m in lines[i + 1:i + 6] if m.strip() and not m.strip().startswith(("at ", "|", ">"))), "")
            out.append(_trim(line.strip() + (f" - {msg}" if msg else "")))
    return summary, out


def _parse_go(text: str) -> tuple:
    out = [_trim(ln.strip()) for ln in text.splitlines() if ln.strip().startswith("--- FAIL")]
    pk = [ln.strip() for ln in text.splitlines() if re.match(r"^(ok|FAIL)\s+\S+", ln)]
    return _trim("; ".join(pk[:6]), 200), out


def _parse_cargo(text: str) -> tuple:
    out = [_trim(ln.strip()) for ln in text.splitlines() if ln.strip().startswith("test ") and ln.rstrip().endswith("FAILED")]
    m = re.findall(r"^test result:.*$", text, re.M)
    return _trim(m[-1] if m else "", 200), out


PARSERS: dict = {"pytest": _parse_pytest, "unittest": _parse_unittest, "npm": _parse_js, "vitest": _parse_js,
                 "go": _parse_go, "cargo": _parse_cargo}


def summarize(runner: str, cmd: str, result: str) -> str:
    """Short, model-facing summary of a finished run. `result` is run_shell's formatted output."""
    if result.startswith("error:"):
        return result
    code, out, err = split_result(result)
    text = out + ("\n" + err if err else "")
    summary, failures = PARSERS.get(runner, lambda t: ("", []))(text)
    head = f"run_tests [{runner}] `{cmd}` -> "
    if runner == "pytest" and code == 5:
        return head + "no tests were collected (exit 5). Check the path or the test file names."
    if code == 0:
        return head + "all passed" + (f": {summary}" if summary else "") + "."
    if "ModuleNotFoundError" in text or "No module named" in text:
        m = re.search(r"No module named '?([\w.]+)", text)
        return (head + f"cannot run: missing module '{m.group(1) if m else '?'}' in the environment that runs the "
                "tests (the dependency is not installed there). This is not a test failure.")
    if "command not found" in text.lower() or "is not recognized" in text.lower():
        return head + "cannot run: the test command is not installed or not on PATH for the companion. Not a test failure."
    lines = [head + f"FAILED (exit {code})" + (f": {summary}" if summary else "")]
    if failures:
        lines.append("Failures:")
        for i, f in enumerate(failures[:MAX_FAILURES], 1):
            lines.append(f" {i}. {f}")
        if len(failures) > MAX_FAILURES:
            lines.append(f" ... {len(failures) - MAX_FAILURES} more. Fix these, then re-run (or pass path= to focus).")
    else:
        tail = [ln for ln in text.splitlines() if ln.strip()][-25:]
        lines.append("No structured failures found; last output:")
        lines.extend("  " + _trim(ln) for ln in tail)
    return "\n".join(lines)


# -------------------------------------------------------------------- tool

async def tool_run_tests(args: dict) -> str:
    from .. import shell_tools
    from . import active_workspace, _remote_uid
    try:
        path = _safe_path(str(args.get("path") or args.get("file") or ""))
    except NoRunner as e:
        return f"error: {e}"
    found = await detect_for_workspace(_remote_uid(), str(active_workspace()))
    if not found:
        return ("error: no test runner detected (looked for pytest/unittest, npm test, go.mod, Cargo.toml in the "
                "workspace root). Run the project's own test command with run_shell.")
    cmd = build_command(found, path)
    cfg_cap = int(shell_tools.shell_cfg().get("tests_timeout_s", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S)
    token = shell_tools._exec_timeout.set(max(30, min(cfg_cap, MAX_TIMEOUT_S)))
    try:
        result = await shell_tools.tool_run_shell({"command": cmd})
    finally:
        shell_tools._exec_timeout.reset(token)
    return summarize(found["runner"], cmd, result)


RUN_TESTS_SCHEMA = {"type": "function", "function": {
    "name": "run_tests",
    "description": ("Run the project's tests (auto-detected: pytest, unittest, npm test, vitest, go test, cargo test) "
                    "and get a short summary: counts, failing test ids, one error line and location each. "
                    "Pass path to run one test file, folder or test id. Use it after changing code."),
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string",
                 "description": "optional test file, folder or node id, e.g. tests/test_auth.py or tests/test_auth.py::test_login"}},
        "required": []}}}
