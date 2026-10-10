"""Git for the agent: git_inspect (read-only), git_commit and git_branch.

Everything runs through core/git_tools, i.e. on the user's machine via the companion, whose own guard still
refuses reset / clean / force / update-ref / branch deletion. What the agent gets on top is deliberately narrow:

  * git_inspect  - status, diff, log, branches. Read-only, so it is allowed in plan mode and in parallel.
  * git_commit   - commits ONLY the paths it names (or tracked changes with all_tracked), so what a user staged
                   elsewhere is not swept in. It refuses files that look like secrets (.env, private keys,
                   credentials), directory-wide staging ("."), and card numbers in the message.
  * git_branch   - create+switch or switch. Never forced, never deletes.

Pushing and opening a PR are NOT agent tools: they publish work outside the user's machine, so they stay behind
the Git panel's explicit button and the git.push permission. The agent commits, then tells the user.

git_commit and git_branch are gated like a shell command: the loop asks for approval using `command_for`'s text,
so the user sees exactly what will be committed, and saved allow rules apply.
"""

import re
from typing import Optional

from .. import pan


def _gt():
    """core.git_tools imports from this package, so it is imported on use, not at load."""
    from .. import git_tools
    return git_tools

MAX_MESSAGE = 2000
MAX_PATHS = 100
MAX_STATUS_FILES = 60

_TEMPLATE_ENV = {".env.example", ".env.sample", ".env.template", ".env.dist"}
_SECRET_RX = re.compile(
    r"(^|/)\.env(\.[^/]*)?$"
    r"|\.(pem|key|pfx|p12|keystore|jks|kdbx|ovpn)$"
    r"|(^|/)id_(rsa|dsa|ecdsa|ed25519)$"
    r"|(^|/)(credentials|secrets?)(\.[a-z0-9]+)?$"
    r"|(^|/)\.(npmrc|netrc|pypirc|git-credentials)$"
    r"|(^|/)\.aws/|(^|/)\.ssh/", re.I)


def is_secret_path(path: str) -> bool:
    """Does this path look like a credential file that must not be committed by an agent?"""
    p = str(path or "").replace("\\", "/").strip().lower()
    return p.rsplit("/", 1)[-1] not in _TEMPLATE_ENV and bool(_SECRET_RX.search(p))


def clean_paths(raw) -> tuple:
    """(paths, error). Pathspecs only: nothing option-like, absolute, parent-relative, magic (':...') or broad
    enough to stage the whole tree behind the secret check's back."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not raw:
        return [], None
    if len(raw) > MAX_PATHS:
        return [], f"too many paths (max {MAX_PATHS}); commit in smaller groups"
    out = []
    for item in raw:
        p = str(item or "").strip().replace("\\", "/")
        segs = [s for s in p.split("/") if s]
        if (not p or "\n" in p or p.startswith(("-", ":", "/")) or re.match(r"^[A-Za-z]:", p)
                or ".." in segs or p in (".", "./", "*", "**", "*.*") or not segs):
            return [], (f"path {item!r} is not allowed: give file or folder paths inside the workspace, "
                        "not '.', '*', absolute or '..' paths")
        out.append(p)
    return out, None


def _message(args: dict) -> tuple:
    msg = str(args.get("message") or "").strip()
    if not msg:
        return "", "commit message is required"
    if len(msg) > MAX_MESSAGE:
        msg = msg[:MAX_MESSAGE]
    if pan.contains_pan(msg):
        return "", "the commit message contains what looks like a payment card number; remove it"
    return msg, None


def command_for(name: str, args: dict) -> Optional[str]:
    """The text the approval card shows for a side-effecting git tool, or None when the call is invalid (the tool
    then returns its own error, no card needed) or has no side effect."""
    args = args or {}
    if name == "git_commit":
        msg, err = _message(args)
        paths, perr = clean_paths(args.get("paths") or args.get("path"))
        if err or perr or (not paths and not args.get("all_tracked")):
            return None
        first = msg.splitlines()[0][:120]
        return f'git commit -m "{first}"' + (" -a" if args.get("all_tracked") and not paths else " -- " + " ".join(paths))
    if name == "git_branch":
        action = str(args.get("action") or "list").lower()
        branch = str(args.get("name") or "")
        if action in ("create", "switch") and _gt().valid_branch_name(branch):
            return f"git switch {'-c ' if action == 'create' else ''}{branch}"
    return None


def _fmt_status(st: dict) -> str:
    head = f"branch {st['branch']}" + (" (detached)" if st.get("detached") else "")
    if st.get("ahead") or st.get("behind"):
        head += f", ahead {st.get('ahead', 0)} / behind {st.get('behind', 0)}"
    files = st.get("files") or []
    if not files:
        return head + "; working tree clean"
    lines = [f"{head}; {len(files)} changed file(s):"]
    for f in files[:MAX_STATUS_FILES]:
        lines.append(f"  {f['index_status']}{f['worktree_status']} {f['path']}")
    if len(files) > MAX_STATUS_FILES:
        lines.append(f"  ... {len(files) - MAX_STATUS_FILES} more")
    lines.append("(first column = staged, second = unstaged; '?' = untracked)")
    return "\n".join(lines)


async def tool_git_inspect(args: dict) -> str:
    action = str(args.get("action") or "status").strip().lower()
    if action == "status":
        res = await _gt().git_status()
        return f"error: {res['error']}" if "error" in res else _fmt_status(res)
    if action in ("diff", "log"):
        paths, perr = clean_paths(args.get("path"))
        if perr:
            return f"error: {perr}"
        path = paths[0] if paths else None
        if action == "diff":
            res = await _gt().git_diff(path, bool(args.get("staged")), bool(args.get("untracked")))
            if "error" in res:
                return f"error: {res['error']}"
            return res["diff"] or ("(no staged changes)" if args.get("staged") else "(no unstaged changes)")
        try:
            count = int(args.get("count") or 10)
        except (TypeError, ValueError):
            count = 10
        res = await _gt().git_log(count, path)
        return f"error: {res['error']}" if "error" in res else (res["log"] or "(no commits)")
    if action == "branches":
        res = await _gt().git_branches()
        if "error" in res:
            return f"error: {res['error']}"
        return "branches: " + ", ".join(res["branches"]) + (f" (default: {res['default']})" if res.get("default") else "")
    return "error: action must be one of: status, diff, log, branches"


async def tool_git_commit(args: dict) -> str:
    msg, err = _message(args)
    if err:
        return f"error: {err}"
    paths, perr = clean_paths(args.get("paths") or args.get("path"))
    if perr:
        return f"error: {perr}"
    all_tracked = bool(args.get("all_tracked"))
    if not paths and not all_tracked:
        return ("error: say what to commit: paths=[...] (files or folders you changed) or all_tracked=true for every "
                "modified tracked file. Check git_inspect(status) first.")
    cwd = _gt()._cwd()
    if paths:
        res = await _gt().git_stage(paths)
        if "error" in res:
            return f"error: staging failed: {res['error']}"
        staged = await _gt().git_staged_names(paths)
    else:
        code, _o, e = await _gt()._run(["add", "-u"], cwd)
        if code != 0:
            return f"error: staging failed: {(e or '').strip()[:200]}"
        staged = await _gt().git_staged_names()
    if not staged:
        return "nothing to commit: none of that has changes (check git_inspect(status))."
    secrets = [p for p in staged if is_secret_path(p)]
    if secrets:
        if paths:
            await _gt().git_unstage(secrets)       # we staged these; put them back
        return (f"error: refusing to commit {', '.join(secrets[:5])}: it looks like a credential file. Nothing was "
                "committed. Add it to .gitignore, or ask the user to commit it themselves.")
    res = await _gt().git_commit_paths(msg, paths) if paths else await _gt().git_commit(msg)
    if "error" in res:
        return f"error: {res['error']}"
    first = (res.get("output") or "").splitlines()[0] if res.get("output") else "committed"
    return f"committed {len(staged)} file(s): {first}. Pushing is done by the user from the Git panel."


async def tool_git_branch(args: dict) -> str:
    action = str(args.get("action") or "list").strip().lower()
    if action == "list":
        return await tool_git_inspect({"action": "branches"})
    if action not in ("create", "switch"):
        return "error: action must be one of: list, create, switch"
    name = str(args.get("name") or "").strip()
    res = await _gt().git_switch(name, create=(action == "create"), start=str(args.get("from") or "").strip() or None)
    if "error" in res:
        return f"error: {res['error']}"
    return f"{'created and switched to' if action == 'create' else 'switched to'} branch {name}"


GIT_SCHEMAS = [
    {"type": "function", "function": {
        "name": "git_inspect",
        "description": "Read-only git: status (changed files), diff (path, staged), log (count, path), branches.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["status", "diff", "log", "branches"]},
            "path": {"type": "string", "description": "optional file or folder for diff/log"},
            "staged": {"type": "boolean", "description": "diff: staged changes instead of unstaged"},
            "count": {"type": "integer", "description": "log: number of commits (default 10)"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "git_commit",
        "description": ("Commit your work locally. Name the files you changed in paths (it commits only those), or "
                        "set all_tracked for every modified tracked file. Refuses credential files. Does not push."),
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "description": "commit message: short summary line, then optional detail"},
            "paths": {"type": "array", "items": {"type": "string"}, "description": "files or folders to commit"},
            "all_tracked": {"type": "boolean", "description": "commit all modified tracked files instead of paths"}},
            "required": ["message"]}}},
    {"type": "function", "function": {
        "name": "git_branch",
        "description": "List branches, create and switch to a new branch, or switch to an existing one. Never deletes.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "create", "switch"]},
            "name": {"type": "string", "description": "branch name, e.g. agent/fix-login"},
            "from": {"type": "string", "description": "create: start from this branch (default: current)"}},
            "required": ["action"]}}},
]

GIT_IMPLS = {"git_inspect": tool_git_inspect, "git_commit": tool_git_commit, "git_branch": tool_git_branch}
GIT_GATED_TOOLS = frozenset({"git_commit", "git_branch"})
