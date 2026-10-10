"""Granular allow/ask/deny rules for agent tool calls, per tool and per path or command glob.

Rules come from the admin config (`permissions.rules` in config/app.json), so a user cannot loosen them:

    {"id": "no-keys", "tool": "read_file|write_file|edit_file", "path": "**/*.pem",
     "action": "deny", "reason": "private keys are off limits"}
    {"id": "push", "tool": "run_shell", "command": "git push*", "action": "ask"}

  tool     tool names, `|` separated or a list; globs allowed ("mcp__*", "*"). Default "*".
  path     glob on the file paths in the arguments (path, file, paths, src, dst ...). `**` crosses folders, `*` does
           not. A pattern with no "/" also matches the file name anywhere (gitignore style). For shell tools every
           word of the command is tried as a path, which is best effort: a determined script can still build a
           path at run time, so deny rules are one layer next to the companion's own path policy.
  command  glob on the shell command line (run_shell, run_tests, git tools).
  unless   a path glob that exempts a match (".env.example" next to a ".env*" rule).
  action   "deny" (the call never runs, even in bypass mode) or "ask" (an approval card; bypass mode skips it).

A deny anywhere wins over an ask. Matching is case-insensitive and slash-normalised. Pure functions, no I/O.
"""

import fnmatch
import re
from typing import Any, Optional

PATH_KEYS = ("path", "file", "file_path", "filename", "src", "dst", "source", "destination", "from", "to",
             "target", "cwd", "directory", "dir", "root")
LIST_PATH_KEYS = ("paths", "files")
SHELL_TOOLS = frozenset({"run_shell", "run_tests"})
ACTIONS = ("deny", "ask")
_WORD_RX = re.compile(r"""[^\s"'<>|;&()]+""")
_GLOB_CACHE: dict = {}


def _norm(path: str) -> str:
    p = str(path or "").replace("\\", "/").strip().lower()
    while p.startswith("./"):
        p = p[2:]
    return p


def glob_regex(pattern: str):
    """Compile a path glob: `**/` any folders, `**` anything, `*` within one name, `?` one character."""
    pat = _norm(pattern)
    rx = _GLOB_CACHE.get(pat)
    if rx is not None:
        return rx
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    rx = re.compile("^" + "".join(out) + "$")
    if len(_GLOB_CACHE) < 512:
        _GLOB_CACHE[pat] = rx
    return rx


def expand_braces(pattern: str, _depth: int = 0) -> list:
    """`a.{x,y}` -> [`a.x`, `a.y`] (one level of nesting is enough for rules; deeper is left as is)."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m or _depth > 3:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out.extend(expand_braces(pattern[:m.start()] + alt + pattern[m.end():], _depth + 1))
    return out[:64]


def path_matches(path: str, pattern: str) -> bool:
    p = _norm(path)
    if not p or not pattern:
        return False
    for pat in expand_braces(str(pattern)):
        rx = glob_regex(pat)
        if rx.match(p):
            return True
        if "/" not in _norm(pat) and rx.match(p.rsplit("/", 1)[-1]):
            return True                                    # "*.pem" means a .pem file in any folder
    return False


def _tools_of(rule: dict) -> list:
    raw = rule.get("tool", "*")
    if isinstance(raw, str):
        raw = raw.split("|")
    return [str(t).strip().lower() for t in (raw or ["*"]) if str(t).strip()]


def tool_matches(rule: dict, name: str) -> bool:
    n = str(name or "").lower()
    return any(fnmatch.fnmatchcase(n, t) for t in _tools_of(rule))


def paths_in(name: str, args: Any, command: Optional[str] = None) -> list:
    """Every path a call touches, as far as its arguments say."""
    out: list = []
    if isinstance(args, dict):
        for k in PATH_KEYS:
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v)
        for k in LIST_PATH_KEYS:
            v = args.get(k)
            if isinstance(v, (list, tuple)):
                out.extend(str(x) for x in v if isinstance(x, str) and x.strip())
    if command and (name in SHELL_TOOLS or name.startswith("git_")):
        out.extend(w for w in _WORD_RX.findall(command) if len(w) <= 260)
    return out


def valid_rules(raw: Any) -> list:
    """The usable rules out of config: dicts with a known action. A malformed rule is ignored, never raised."""
    rules = []
    if not isinstance(raw, (list, tuple)):
        return rules
    for r in raw:
        if isinstance(r, dict) and str(r.get("action", "")).lower() in ACTIONS and (r.get("path") or r.get("command")
                                                                                   or r.get("tool")):
            rules.append(r)
    return rules


def _rule_hits(rule: dict, name: str, paths: list, command: Optional[str]) -> bool:
    if not tool_matches(rule, name):
        return False
    pat, cmd_pat, unless = rule.get("path"), rule.get("command"), rule.get("unless")
    if cmd_pat:
        if not command or not fnmatch.fnmatchcase(command.strip().lower(), str(cmd_pat).strip().lower()):
            return False
    if pat:
        hit = [p for p in paths if path_matches(p, str(pat))]
        if unless:
            hit = [p for p in hit if not path_matches(p, str(unless))]
        if not hit:
            return False
    return True


def evaluate(name: str, args: Any, rules: Any, command: Optional[str] = None) -> Optional[dict]:
    """The rule that decides this call, or None. {"action", "id", "reason"}; deny beats ask."""
    rules = valid_rules(rules)
    if not rules:
        return None
    if command is None and name in SHELL_TOOLS and isinstance(args, dict):
        command = str(args.get("command") or args.get("cmd") or "")
    paths = paths_in(name, args, command)
    asked = None
    for r in rules:
        if not _rule_hits(r, name, paths, command):
            continue
        verdict = {"action": str(r["action"]).lower(), "id": str(r.get("id") or "rule"),
                   "reason": str(r.get("reason") or "")[:200]}
        if verdict["action"] == "deny":
            return verdict
        asked = asked or verdict
    return asked


def refusal_text(verdict: dict) -> str:
    why = f": {verdict['reason']}" if verdict.get("reason") else ""
    return (f"error: blocked by policy rule '{verdict['id']}'{why}. This is an administrator rule, not a mistake "
            "to work around: do not try another tool or path to do the same thing; tell the user what you could not do.")
