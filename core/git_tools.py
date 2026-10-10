"""Local git operations (status/diff/stage/commit/push) against the active project workspace.

No destructive operations are exposed here (no reset --hard, no force-push, no branch delete) -
this is a review/commit/push surface for the UI, not a full git client.
"""

import re
from pathlib import Path
from typing import Optional

from .agent_tools import MAX_TOOL_OUTPUT
from .agent_tools import require_device_workspace
from .request_context import get_current_user_id

from . import companion_bridge

GIT_TIMEOUT_S = 30

# Server-side mirror of companion/gitops.js BLOCKED_ARG_RX: refuse destructive
# git args before spending an RPC. The companion re-checks; this is fail-fast.
_BLOCKED_ARGS = (
    "--hard", "reset", "-f", "--force", "clean", "filter-branch", "--delete",
)


def _cwd() -> Path:
    """The active project folder on the USER's machine (via require_device_workspace).

    git never runs on the server: the server would operate on its own disk
    whenever the path happened to exist there. The companion executes git on
    the user's machine inside the approved workspace (companion/gitops.js) and
    returns stdout/stderr over the bridge - the argument/ref policy below is
    enforced before a single RPC is sent, and the companion re-checks it.
    """
    _uid, ws = require_device_workspace()
    return ws


async def _run(args: list, cwd: Path) -> tuple[int, str, str]:
    """One git->companion RPC: returns (exit_code, stdout, stderr)."""
    try:
        bad = [a for a in (args or []) if a in _BLOCKED_ARGS]
        if bad:
            return 1, "", f"refused by server: destructive git arg {bad[0]}"
        uid = get_current_user_id()
        data = await companion_bridge.call(
            uid, "git.run", {"args": list(args), "cwd": str(cwd)}, timeout=GIT_TIMEOUT_S)
        return int(data.get("exit_code", 1)), str(data.get("stdout", "")), str(data.get("stderr", ""))
    except Exception as e:
        return 1, "", f"git unavailable: {type(e).__name__}: {e}"


async def _is_repo(cwd: Path) -> bool:
    code, out, _ = await _run(["rev-parse", "--is-inside-work-tree"], cwd)
    return code == 0 and out.strip() == "true"


async def git_status() -> dict:
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    code, out, err = await _run(["status", "--porcelain=v1", "-b"], cwd)
    if code != 0:
        return {"error": err.strip() or "git status failed"}
    lines = out.splitlines()
    ahead, behind = 0, 0
    files = []
    if lines and lines[0].startswith("##"):
        header = lines[0][2:].strip()
        lines = lines[1:]
        if "[ahead " in header:
            ahead = int(header.split("[ahead ")[1].split(",")[0].split("]")[0])
        if "behind " in header:
            behind = int(header.split("behind ")[1].split("]")[0].split(",")[0])

    # Parsing the porcelain header for the branch name breaks on "No commits yet on
    # <branch>" (fresh repo) and "HEAD (no branch)" (detached) - ask git directly instead.
    _, branch_out, _ = await _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    branch = branch_out.strip() or None
    detached = branch == "HEAD"
    if detached:
        _, sha_out, _ = await _run(["rev-parse", "--short", "HEAD"], cwd)
        branch = f"detached@{sha_out.strip()}" if sha_out.strip() else "detached HEAD"
    for ln in lines:
        if len(ln) < 4:
            continue
        index_status, worktree_status, path = ln[0], ln[1], ln[3:]
        files.append({"path": path, "index_status": index_status, "worktree_status": worktree_status})
    return {"branch": branch, "detached": detached, "ahead": ahead, "behind": behind, "files": files}


async def git_diff(path: Optional[str] = None, staged: bool = False, untracked: bool = False) -> dict:
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    if untracked and path:
        # a new file is not in the index, so a plain diff is empty: compare it against nothing instead
        args = ["diff", "--no-index", "--", "/dev/null", path]
    else:
        args = ["diff"]
        if staged:
            args.append("--cached")
        if path:
            args += ["--", path]
    code, out, err = await _run(args, cwd)
    # --no-index exits 1 when the files differ, which is the normal case for a new file
    if code != 0 and not (untracked and code == 1 and out):
        return {"error": err.strip() or "git diff failed"}
    if len(out) > MAX_TOOL_OUTPUT:
        out = out[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(out)} chars total)"
    return {"diff": out}


async def git_stage(paths: list) -> dict:
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    if not paths:
        return {"error": "no paths given"}
    code, _, err = await _run(["add", "--"] + list(paths), cwd)
    if code != 0:
        return {"error": err.strip() or "git add failed"}
    return {"ok": True}


async def git_unstage(paths: list) -> dict:
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    if not paths:
        return {"error": "no paths given"}
    code, _, err = await _run(["restore", "--staged", "--"] + list(paths), cwd)
    if code != 0:
        return {"error": err.strip() or "git restore --staged failed"}
    return {"ok": True}


async def git_commit(message: str) -> dict:
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    if not message or not message.strip():
        return {"error": "commit message is required"}
    code, out, err = await _run(["commit", "-m", message.strip()], cwd)
    if code != 0:
        return {"error": (err or out).strip() or "git commit failed"}
    return {"ok": True, "output": out.strip()}


def _bad_ref(*names) -> Optional[str]:
    """Caller-supplied remote/branch names must not be parsed as git options
    (--receive-pack=..., --output=... run programs / write files)."""
    for n in names:
        if n and str(n).lstrip().startswith("-"):
            return f"invalid git ref or remote name: {n!r}"
    return None


async def git_push(remote: str = "origin", branch: Optional[str] = None) -> dict:
    if _bad_ref(remote, branch):
        return {"error": _bad_ref(remote, branch)}
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    args = ["push", remote]
    if branch:
        args.append(branch)
    code, out, err = await _run(args, cwd)
    if code != 0:
        return {"error": (err or out).strip() or "git push failed"}
    return {"ok": True, "output": (out + err).strip()}


async def git_branches() -> dict:
    """Local branch names for the PR-base picker, with the repo's default branch
    (origin/HEAD, if known) flagged so the UI can preselect it."""
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    code, out, err = await _run(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], cwd)
    if code != 0:
        return {"error": err.strip() or "git for-each-ref failed"}
    branches = [b for b in out.splitlines() if b.strip()]

    default_branch = None
    _, head_out, _ = await _run(["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], cwd)
    if head_out.strip():
        default_branch = head_out.strip().split("/", 1)[-1]
    if not default_branch:
        for candidate in ("main", "master"):
            if candidate in branches:
                default_branch = candidate
                break
    return {"branches": branches, "default": default_branch}


async def git_diff_range(base: str) -> dict:
    """Diff of everything the current branch has that `base` doesn't - i.e. what a PR
    against `base` would contain. Used to auto-generate a PR title/description."""
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    if not base or not base.strip():
        return {"error": "base branch is required"}
    if _bad_ref(base):
        return {"error": _bad_ref(base)}
    code, out, err = await _run(["diff", f"{base}...HEAD"], cwd)
    if code != 0:
        return {"error": err.strip() or f"git diff {base}...HEAD failed"}
    if len(out) > MAX_TOOL_OUTPUT:
        out = out[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(out)} chars total)"
    return {"diff": out}


async def git_remote_url(remote: str = "origin") -> Optional[str]:
    if _bad_ref(remote):
        return None
    cwd = _cwd()
    if not await _is_repo(cwd):
        return None
    code, out, _ = await _run(["remote", "get-url", remote], cwd)
    return out.strip() if code == 0 else None


def parse_github_owner_repo(remote_url: str) -> Optional[tuple]:
    """'git@github.com:owner/repo.git' or 'https://github.com/owner/repo.git' -> (owner, repo)."""
    if not remote_url:
        return None
    url = remote_url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        # git@github.com:owner/repo
        try:
            path = url.split(":", 1)[1]
        except IndexError:
            return None
    elif "://" in url:
        try:
            path = url.split("://", 1)[1].split("/", 1)[1]
        except IndexError:
            return None
    else:
        return None
    parts = path.strip("/").split("/")
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


# ---- helpers for the agent's git tools (core/agent_tools/git_agent_tools.py) ----

_REF_RX = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,99}$")


def valid_branch_name(name: str) -> bool:
    """A branch name safe to pass as an argument: no option-like start, no '..', no trailing '/' or '.lock'."""
    n = str(name or "")
    return bool(_REF_RX.match(n)) and ".." not in n and not n.endswith(("/", ".lock", ".")) and "//" not in n


async def git_log(count: int = 10, path: Optional[str] = None) -> dict:
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    n = max(1, min(int(count or 10), 50))
    args = ["log", f"-n{n}", "--date=short", "--pretty=format:%h %ad %an: %s"]
    if path:
        args += ["--", path]
    code, out, err = await _run(args, cwd)
    if code != 0:
        return {"error": err.strip() or "git log failed"}
    return {"log": out.strip()}


async def git_switch(name: str, create: bool = False, start: Optional[str] = None) -> dict:
    """Create (and switch to) or switch to a branch. Never forces: a dirty tree that conflicts refuses."""
    if not valid_branch_name(name) or (start and not valid_branch_name(start)):
        return {"error": f"invalid branch name: {name!r}"}
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    args = ["switch"] + (["-c", name] + ([start] if start else []) if create else [name])
    code, out, err = await _run(args, cwd)
    if code != 0:
        return {"error": (err or out).strip() or "git switch failed"}
    return {"ok": True, "output": (out + err).strip()}


async def git_staged_names(paths: Optional[list] = None) -> list:
    """Paths currently staged (optionally limited to `paths`)."""
    cwd = _cwd()
    args = ["diff", "--cached", "--name-only"] + (["--"] + list(paths) if paths else [])
    code, out, _ = await _run(args, cwd)
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if code == 0 else []


async def git_commit_paths(message: str, paths: list) -> dict:
    """Commit only `paths` (git's --only semantics): changes a user staged elsewhere are not swept in."""
    cwd = _cwd()
    if not await _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    if not message or not message.strip():
        return {"error": "commit message is required"}
    code, out, err = await _run(["commit", "-m", message.strip(), "--"] + list(paths), cwd)
    if code != 0:
        return {"error": (err or out).strip() or "git commit failed"}
    return {"ok": True, "output": out.strip()}
