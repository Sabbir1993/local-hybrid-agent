"""core/agent_loop/patch_verifier.py — SWE-Bench grade patch verification (Phase 3).

Three capabilities the stock agent loop lacks that cause SWE-Bench failures:

1. FAULT LOCALIZATION
   Given a failing test's stderr/stdout, narrow the root cause to specific
   (file, line) candidates before touching code.  Uses two signals:
     - traceback parsing: exact file + line from the exception chain
     - error fingerprinting: normalized error token → likely source files

2. PATCH VALIDATION SYNTAX CHECK
   After write_file / edit_file, verify the written file parses cleanly
   (Python: compile(); JS: regex guard).  Catches SyntaxErrors, IndentationErrors,
   and missing-import errors before they reach the test runner.

3. SELF-HEALING VERIFICATION RECEIPT
   `VerificationReceipt` tracks the full edit→test→result cycle:
     - what was edited
     - what test was run
     - whether it passed
     - if it failed: the fault-localization delta from the new failure
   `check_verification_loop` inspects the receipt and decides:
     OK        all tests pass
     RETRY     test still fails, new fault identified — return localized signal
     ABANDON   same error 3 times — give up and report root cause

All functions are pure (no I/O) except `validate_file_syntax()` which only
reads the named file (no subprocess), keeping this unit-testable without
a running server.

Design constraints:
  - No model calls, no subprocess, no network — < 2ms per invocation
  - No imports that pull in the DB or server state (pure stdlib)
  - Integrates with check_execution_receipt() via VerificationReceipt.receipt_for()
"""

import ast
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# 1.  Fault Localization
# ---------------------------------------------------------------------------

# Traceback line: "  File "/path/to/file.py", line 42, in func"
_TB_FILE_RX = re.compile(
    r'File "([^"]+)", line (\d+)(?:, in (\S+))?'
)

# Common error class names and which source pattern they implicate
_ERROR_HINTS: dict = {
    "importerror":     ("import", "missing dependency or circular import"),
    "modulenotfounderror": ("import", "module not installed or wrong path"),
    "attributeerror":  ("attribute", "object missing the referenced attribute"),
    "typeerror":       ("signature", "function called with wrong argument types"),
    "valueerror":      ("validation", "unexpected value — check input parsing"),
    "keyerror":        ("dict_access", "missing key in dict — check data shape"),
    "assertionerror":  ("test_assertion", "test assertion failed"),
    "syntaxerror":     ("syntax", "Python syntax error in the written file"),
    "indentationerror": ("syntax", "indentation error in the written file"),
    "nameerror":       ("name", "undefined variable or missing import"),
    "zerodivisionerror": ("arithmetic", "division by zero in calculation"),
    "runtimeerror":    ("runtime", "runtime error during execution"),
    "oserror":         ("filesystem", "file not found or permission denied"),
    "ioerror":         ("filesystem", "file not found or permission denied"),
}


@dataclass
class FaultSignal:
    """Structured localization of a test failure."""
    # Traceback-derived candidates: [(file, line, function)]
    traceback_sites: list = field(default_factory=list)
    # Error class (e.g. "AssertionError", "ImportError")
    error_class: str = ""
    # Normalized error message
    error_message: str = ""
    # High-level category hint from _ERROR_HINTS
    category: str = ""
    category_note: str = ""
    # Fingerprint of this failure (for repeat detection)
    fingerprint: str = ""
    # Human-readable root cause summary
    summary: str = ""

    def is_empty(self) -> bool:
        return not self.traceback_sites and not self.error_class

    @property
    def primary_file(self) -> Optional[str]:
        """The most likely source file to fix (last non-test-framework frame)."""
        for site in reversed(self.traceback_sites):
            f = site.get("file", "")
            # skip pytest/unittest internals
            if any(skip in f for skip in ("_pytest", "unittest", "pluggy", "site-packages")):
                continue
            return f
        return self.traceback_sites[-1]["file"] if self.traceback_sites else None

    @property
    def primary_line(self) -> Optional[int]:
        """Line number in primary_file."""
        pf = self.primary_file
        for site in reversed(self.traceback_sites):
            if site.get("file") == pf:
                return site.get("line")
        return None


def localize_fault(output: str) -> FaultSignal:
    """Parse test output / stderr and return a structured FaultSignal.

    Args:
        output: raw stdout+stderr from a failing test run (e.g. from
                `python -m unittest` or `pytest`).
    Returns:
        FaultSignal with traceback sites, error class, and category.
        If no traceback is found, returns an empty FaultSignal.
    """
    if not output:
        return FaultSignal()

    # 1. Extract all traceback sites
    sites = []
    for m in _TB_FILE_RX.finditer(output):
        sites.append({
            "file": m.group(1),
            "line": int(m.group(2)),
            "function": m.group(3) or "",
        })

    # 2. Find the error class and message (last "ErrorClass: message" line)
    error_class = ""
    error_message = ""
    # Look for standard "ExceptionClass: message" pattern at start of a line
    err_m = re.search(
        r'^([A-Z][a-zA-Z]+(?:Error|Exception|Warning|Failure)[^:]*):[ \t]*(.*)',
        output, re.MULTILINE
    )
    if err_m:
        error_class = err_m.group(1).strip()
        error_message = err_m.group(2).strip()[:200]

    # 3. Category from error class
    category = ""
    category_note = ""
    ec_lower = error_class.lower()
    for key, (cat, note) in _ERROR_HINTS.items():
        if key in ec_lower:
            category = cat
            category_note = note
            break

    # 4. Fingerprint: sha1 of (error_class, last site file:line)
    fp_parts = error_class
    if sites:
        last = sites[-1]
        fp_parts += f":{last['file']}:{last['line']}"
    fingerprint = hashlib.sha1(fp_parts.encode("utf-8", "replace")).hexdigest()[:12]

    # 5. Summary
    parts = []
    if error_class:
        parts.append(f"{error_class}: {error_message}" if error_message else error_class)
    if sites:
        last = sites[-1]
        parts.append(f"at {last['file']}:{last['line']}" + (f" in {last['function']}" if last['function'] else ""))
    summary = " | ".join(parts) or "unknown failure"

    return FaultSignal(
        traceback_sites=sites,
        error_class=error_class,
        error_message=error_message,
        category=category,
        category_note=category_note,
        fingerprint=fingerprint,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# 2. Patch Validation — Syntax Check
# ---------------------------------------------------------------------------

@dataclass
class SyntaxCheckResult:
    ok: bool = True
    error: str = ""
    line: Optional[int] = None
    col: Optional[int] = None
    file: str = ""

    def as_receipt_error(self) -> str:
        loc = ""
        if self.line:
            loc = f" at line {self.line}"
            if self.col:
                loc += f", col {self.col}"
        return f"syntax error in {self.file}{loc}: {self.error}"


def validate_file_syntax(path: str, source: Optional[str] = None) -> SyntaxCheckResult:
    """Check that a Python or JS file parses without errors.

    For Python: uses `compile()` — stdlib, no subprocess.
    For JS/TS: uses a lightweight regex guard (no heavy parser).

    Args:
        path: file path (used to determine language; must end in .py/.js/.ts/.jsx/.tsx)
        source: optional source text. If None, the file is read from disk.
    Returns:
        SyntaxCheckResult(ok=True) when clean, otherwise the error details.
    """
    p = Path(path)
    suffix = p.suffix.lower()

    try:
        if source is None:
            try:
                source = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return SyntaxCheckResult(ok=False, error=f"cannot read file: {e}", file=str(path))

        if suffix == ".py":
            return _check_python_syntax(source, str(path))
        elif suffix in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
            return _check_js_syntax_heuristic(source, str(path))
        else:
            # Unsupported language: pass (no false negatives)
            return SyntaxCheckResult(ok=True, file=str(path))

    except Exception as e:
        return SyntaxCheckResult(ok=False, error=f"validator error: {e}", file=str(path))


def _check_python_syntax(source: str, path: str) -> SyntaxCheckResult:
    """Python compile() syntax check — catches SyntaxError and IndentationError."""
    try:
        compile(source, path, "exec", ast.PyCF_ONLY_AST)
        return SyntaxCheckResult(ok=True, file=path)
    except SyntaxError as e:
        return SyntaxCheckResult(
            ok=False,
            error=e.msg or str(e),
            line=e.lineno,
            col=e.offset,
            file=path,
        )
    except Exception as e:
        return SyntaxCheckResult(ok=False, error=str(e), file=path)


def _check_js_syntax_heuristic(source: str, path: str) -> SyntaxCheckResult:
    """Lightweight JS/TS guard: catches obviously unbalanced braces/brackets and
    common copy-paste errors that create clearly broken files.

    This is NOT a full parser — it's a fast triage to catch the 80 % of write_file
    calls that produce a structurally broken JS file before the test runner sees it.
    """
    # Count unbalanced brackets (ignoring strings and comments is complex; this
    # is intentionally conservative: only report obvious mismatches)
    depth = {"(": 0, "[": 0, "{": 0}
    close = {")": "(", "]": "[", "}": "{"}
    in_str_single = False
    in_str_double = False
    in_template = False
    i = 0
    lines = source.split("\n")
    for line_no, line in enumerate(lines, 1):
        # Skip single-line comments
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        for ch in line:
            if ch == "'" and not in_str_double and not in_template:
                in_str_single = not in_str_single
            elif ch == '"' and not in_str_single and not in_template:
                in_str_double = not in_str_double
            elif ch == '`' and not in_str_single and not in_str_double:
                in_template = not in_template
            elif not in_str_single and not in_str_double and not in_template:
                if ch in depth:
                    depth[ch] += 1
                elif ch in close:
                    k = close[ch]
                    depth[k] -= 1
                    if depth[k] < 0:
                        return SyntaxCheckResult(
                            ok=False,
                            error=f"unmatched '{ch}'",
                            line=line_no,
                            file=path,
                        )
    # Check final balance (don't report for template literals: they span lines)
    for opener, cnt in depth.items():
        if cnt > 5:  # threshold: small imbalances may be multi-line templates
            return SyntaxCheckResult(
                ok=False,
                error=f"unmatched '{opener}' ({cnt} unclosed)",
                file=path,
            )
    return SyntaxCheckResult(ok=True, file=path)


def check_import_guard(source: str, path: str) -> SyntaxCheckResult:
    """Check that all `from X import Y` and `import X` names at the top of a
    Python file can be resolved from the current sys.path.

    Only checks stdlib and installed packages; project-local imports are skipped
    (they may not be importable in the test environment).

    Returns SyntaxCheckResult(ok=True) when all checked imports resolve.
    """
    if not path.endswith(".py"):
        return SyntaxCheckResult(ok=True, file=path)
    import importlib.util
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return SyntaxCheckResult(ok=True, file=path)  # syntax check already caught this

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            else:
                if node.level and node.level > 0:
                    continue  # relative imports: skip
                names = [node.module.split(".")[0]] if node.module else []
            for name in names:
                if importlib.util.find_spec(name) is None:
                    # Only report if it looks like a stdlib/installed package, not a
                    # project-local module (no dots, not a path-like name)
                    if "." not in name and not name.startswith("_"):
                        return SyntaxCheckResult(
                            ok=False,
                            error=f"import '{name}' not found — missing dependency or wrong name",
                            line=getattr(node, "lineno", None),
                            file=path,
                        )
    return SyntaxCheckResult(ok=True, file=path)


# ---------------------------------------------------------------------------
# 3. Verification Receipt & Self-Healing Loop
# ---------------------------------------------------------------------------

OK = "ok"
RETRY = "retry"
ABANDON = "abandon"

MAX_RETRY_SAME_ERROR = 3   # give up after the same fingerprint appears 3× times


@dataclass
class VerificationReceipt:
    """Tracks the full edit → test → result cycle for one plan step.

    The agent_loop uses this to decide whether an edit "counts" as verified
    and to generate the localized RETRY signal when a test still fails.

    Attributes:
        edited_files: files mutated by write_file / edit_file / append_file
        test_commands: test commands or module names run after the edit
        passed: True when all tests passed after the last edit
        failure_output: raw output from the last failing test run
        fault: localized FaultSignal from the last failure
        attempt_count: how many times this step has been retried
        fingerprint_history: list of failure fingerprints seen so far
    """
    edited_files: list = field(default_factory=list)
    test_commands: list = field(default_factory=list)
    passed: bool = False
    failure_output: str = ""
    fault: Optional[FaultSignal] = None
    attempt_count: int = 0
    fingerprint_history: list = field(default_factory=list)

    def record_edit(self, path: str) -> None:
        if path and path not in self.edited_files:
            self.edited_files.append(path)

    def record_test(self, command: str) -> None:
        if command and command not in self.test_commands:
            self.test_commands.append(command)

    def record_result(self, passed: bool, output: str = "") -> None:
        self.passed = passed
        self.failure_output = output if not passed else ""
        self.attempt_count += 1
        if not passed and output:
            self.fault = localize_fault(output)
            fp = self.fault.fingerprint
            if fp:
                self.fingerprint_history.append(fp)

    @property
    def same_error_count(self) -> int:
        """How many consecutive times the same error fingerprint has appeared."""
        if not self.fingerprint_history:
            return 0
        last = self.fingerprint_history[-1]
        count = 0
        for fp in reversed(self.fingerprint_history):
            if fp == last:
                count += 1
            else:
                break
        return count

    def receipt_for(self, item_ord: int) -> str:
        """Human-readable verification summary for the plan step note field."""
        if self.passed:
            tests = ", ".join(self.test_commands[-3:]) or "test run"
            files = ", ".join(self.edited_files[-3:]) or "files"
            return (f"step #{item_ord}: VERIFIED ✓ — edited {files}; "
                    f"{tests} passed after {self.attempt_count} attempt(s)")
        fault_str = self.fault.summary if self.fault else "unknown failure"
        return (f"step #{item_ord}: UNVERIFIED — {self.attempt_count} attempt(s); "
                f"last failure: {fault_str}")


def check_verification_loop(receipt: VerificationReceipt,
                             require_test: bool = True) -> tuple:
    """Decide what the agent loop should do after recording a test result.

    Args:
        receipt: the current VerificationReceipt for this plan step.
        require_test: if False, a step without any test run is still OK
                      (for pure-write steps like "add a docstring").

    Returns:
        (status, message) where status ∈ {OK, RETRY, ABANDON}
        and message is the directive injected into the next step.
    """
    # Success path
    if receipt.passed:
        return OK, ""

    # No tests run yet and we require one
    if require_test and not receipt.test_commands:
        return RETRY, (
            "[verify] You edited files but did not run any tests. "
            "Run the relevant test suite (e.g. `python -m unittest <test_module>`) "
            "and report the result before marking this step done."
        )

    # Detect abandon condition: same error repeating
    if receipt.same_error_count >= MAX_RETRY_SAME_ERROR:
        fault = receipt.fault
        summary = fault.summary if fault else "unknown error"
        pf = fault.primary_file if fault else None
        pl = fault.primary_line if fault else None
        location = f" ({pf}:{pl})" if pf else ""
        return ABANDON, (
            f"[verify] The same failure has appeared {receipt.same_error_count} times: "
            f"{summary}{location}. "
            "This error cannot be resolved by further edits alone. "
            "Report the root cause to the user and stop."
        )

    # Retry with localized fault
    fault = receipt.fault
    if fault and not fault.is_empty():
        pf = fault.primary_file
        pl = fault.primary_line
        location_hint = (
            f"Focus on {pf}" + (f" near line {pl}" if pl else "") + "."
            if pf else "Check the traceback above."
        )
        cat_hint = f" ({fault.category_note})" if fault.category_note else ""
        return RETRY, (
            f"[verify] Test still failing after edit — {fault.error_class}{cat_hint}: "
            f"{fault.error_message[:120]}. {location_hint} "
            f"Re-read the file, fix only the root cause, and re-run the test."
        )

    # Generic retry (no traceback parsed)
    return RETRY, (
        "[verify] Test still failing. Read the error output above carefully, "
        "identify the root cause in the code, fix it, and re-run the test."
    )


# ---------------------------------------------------------------------------
# 4. Batch syntax check for actions list (integration point)
# ---------------------------------------------------------------------------

def syntax_check_actions(actions: list) -> list:
    """Run validate_file_syntax on every write_file / edit_file in `actions`.

    `actions` is the agent_loop's action-receipt list:
      [{"name": "write_file", "args": {"path": "...", "content": "..."}, "ok": True}, ...]

    Returns a list of SyntaxCheckResult (only failures; empty list = all clean).
    Silently skips non-file-write actions and files whose syntax checker is not
    supported.
    """
    failures = []
    for act in (actions or []):
        if not act.get("ok"):
            continue
        name = act.get("name", "")
        if name not in ("write_file", "edit_file", "append_file"):
            continue
        args = act.get("args") or {}
        path = args.get("path") or args.get("file") or ""
        if not path:
            continue
        source = args.get("content") or args.get("new_string") or None
        result = validate_file_syntax(path, source=source)
        if not result.ok:
            failures.append(result)
    return failures
