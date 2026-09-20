"""Local git operations (status/diff/stage/commit/push) against the active project workspace.

No destructive operations are exposed here (no reset --hard, no force-push, no branch delete) -
this is a review/commit/push surface for the UI, not a full git client.
"""

import subprocess
from pathlib import Path
from typing import Optional

from .agent_tools import active_workspace, MAX_TOOL_OUTPUT

GIT_TIMEOUT_S = 30


def _run(args: list, cwd: Path) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            ["git"] + args, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_S,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "git executable not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} timed out after {GIT_TIMEOUT_S}s"


def _is_repo(cwd: Path) -> bool:
    code, out, _ = _run(["rev-parse", "--is-inside-work-tree"], cwd)
    return code == 0 and out.strip() == "true"


def git_status() -> dict:
    cwd = active_workspace()
    if not _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    code, out, err = _run(["status", "--porcelain=v1", "-b"], cwd)
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
    _, branch_out, _ = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    branch = branch_out.strip() or None
    detached = branch == "HEAD"
    if detached:
        _, sha_out, _ = _run(["rev-parse", "--short", "HEAD"], cwd)
        branch = f"detached@{sha_out.strip()}" if sha_out.strip() else "detached HEAD"
    for ln in lines:
        if len(ln) < 4:
            continue
        index_status, worktree_status, path = ln[0], ln[1], ln[3:]
        files.append({"path": path, "index_status": index_status, "worktree_status": worktree_status})
    return {"branch": branch, "detached": detached, "ahead": ahead, "behind": behind, "files": files}


def git_diff(path: Optional[str] = None, staged: bool = False) -> dict:
    cwd = active_workspace()
    if not _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    args = ["diff"]
    if staged:
        args.append("--cached")
    if path:
        args += ["--", path]
    code, out, err = _run(args, cwd)
    if code != 0:
        return {"error": err.strip() or "git diff failed"}
    if len(out) > MAX_TOOL_OUTPUT:
        out = out[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(out)} chars total)"
    return {"diff": out}


def git_stage(paths: list) -> dict:
    cwd = active_workspace()
    if not _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    if not paths:
        return {"error": "no paths given"}
    code, _, err = _run(["add", "--"] + list(paths), cwd)
    if code != 0:
        return {"error": err.strip() or "git add failed"}
    return {"ok": True}


def git_unstage(paths: list) -> dict:
    cwd = active_workspace()
    if not _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    if not paths:
        return {"error": "no paths given"}
    code, _, err = _run(["restore", "--staged", "--"] + list(paths), cwd)
    if code != 0:
        return {"error": err.strip() or "git restore --staged failed"}
    return {"ok": True}


def git_commit(message: str) -> dict:
    cwd = active_workspace()
    if not _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    if not message or not message.strip():
        return {"error": "commit message is required"}
    code, out, err = _run(["commit", "-m", message.strip()], cwd)
    if code != 0:
        return {"error": (err or out).strip() or "git commit failed"}
    return {"ok": True, "output": out.strip()}


def git_push(remote: str = "origin", branch: Optional[str] = None) -> dict:
    cwd = active_workspace()
    if not _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    args = ["push", remote]
    if branch:
        args.append(branch)
    code, out, err = _run(args, cwd)
    if code != 0:
        return {"error": (err or out).strip() or "git push failed"}
    return {"ok": True, "output": (out + err).strip()}


def git_branches() -> dict:
    """Local branch names for the PR-base picker, with the repo's default branch
    (origin/HEAD, if known) flagged so the UI can preselect it."""
    cwd = active_workspace()
    if not _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    code, out, err = _run(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], cwd)
    if code != 0:
        return {"error": err.strip() or "git for-each-ref failed"}
    branches = [b for b in out.splitlines() if b.strip()]

    default_branch = None
    _, head_out, _ = _run(["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], cwd)
    if head_out.strip():
        default_branch = head_out.strip().split("/", 1)[-1]
    if not default_branch:
        for candidate in ("main", "master"):
            if candidate in branches:
                default_branch = candidate
                break
    return {"branches": branches, "default": default_branch}


def git_diff_range(base: str) -> dict:
    """Diff of everything the current branch has that `base` doesn't - i.e. what a PR
    against `base` would contain. Used to auto-generate a PR title/description."""
    cwd = active_workspace()
    if not _is_repo(cwd):
        return {"error": f"'{cwd}' is not a git repository"}
    if not base or not base.strip():
        return {"error": "base branch is required"}
    code, out, err = _run(["diff", f"{base}...HEAD"], cwd)
    if code != 0:
        return {"error": err.strip() or f"git diff {base}...HEAD failed"}
    if len(out) > MAX_TOOL_OUTPUT:
        out = out[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(out)} chars total)"
    return {"diff": out}


def git_remote_url(remote: str = "origin") -> Optional[str]:
    cwd = active_workspace()
    if not _is_repo(cwd):
        return None
    code, out, _ = _run(["remote", "get-url", remote], cwd)
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
