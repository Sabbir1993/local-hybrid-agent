"""
routes/git.py - Source Control panel: status/diff/stage/commit/push for the active
project workspace, plus PR creation via a connected GitHub MCP server (routes/mcp_manager.py).
"""

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core import git_tools
from core import git_ai
from core import mcp as mcp_core
from core.agent_tools import WorkspaceAccessDenied
from core.auth import Principal
from core.deps import get_current_user, require_permission
from core.registry import registry

router = APIRouter(prefix="/git", tags=["git"])


def _git(fn, *args):
    """git_tools raise WorkspaceAccessDenied (local git runs on the server while
    project folders live on user machines, so the panel stays disabled until it
    is companion-routed): surface the reason as a 400, not an unhandled 500."""
    try:
        return fn(*args)
    except WorkspaceAccessDenied as e:
        return JSONResponse({"error": str(e)}, status_code=400)


def _is_denied(result) -> bool:
    return isinstance(result, JSONResponse)


@router.get("/status")
async def status():
    result = _git(git_tools.git_status)
    if _is_denied(result):
        return result
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.get("/branches")
async def branches():
    result = _git(git_tools.git_branches)
    if _is_denied(result):
        return result
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.get("/diff")
async def diff(path: Optional[str] = None, staged: bool = False):
    result = _git(git_tools.git_diff, path, staged)
    if _is_denied(result):
        return result
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


class PathsReq(BaseModel):
    paths: list


@router.post("/stage")
async def stage(req: PathsReq):
    result = _git(git_tools.git_stage, req.paths)
    if _is_denied(result):
        return result
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.post("/unstage")
async def unstage(req: PathsReq):
    result = _git(git_tools.git_unstage, req.paths)
    if _is_denied(result):
        return result
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


class CommitReq(BaseModel):
    message: str


@router.post("/commit")
async def commit(req: CommitReq):
    result = _git(git_tools.git_commit, req.message)
    if _is_denied(result):
        return result
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


class PushReq(BaseModel):
    remote: str = "origin"
    branch: Optional[str] = None


@router.post("/push")
async def push(req: PushReq, user: Principal = Depends(require_permission("git.push"))):
    result = _git(git_tools.git_push, req.remote, req.branch)
    if _is_denied(result):
        return result
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.post("/suggest_commit_message")
async def suggest_commit_message(user: Principal = Depends(get_current_user)):
    staged = _git(git_tools.git_diff, None, True)
    if _is_denied(staged):
        return staged
    diff_text = staged.get("diff", "")
    if not diff_text.strip():
        unstaged = _git(git_tools.git_diff, None, False)
        if _is_denied(unstaged):
            return unstaged
        if "error" in unstaged:
            return JSONResponse(unstaged, status_code=400)
        diff_text = unstaged.get("diff", "")
    if not diff_text.strip():
        return JSONResponse({"error": "no changes to summarize"}, status_code=400)
    try:
        message = await git_ai.generate_commit_message(diff_text, user.id)
    except Exception as e:
        return JSONResponse({"error": f"generation failed: {e}"}, status_code=502)
    return {"message": message}


class SuggestPrReq(BaseModel):
    base: str = "main"


@router.post("/suggest_pr")
async def suggest_pr(req: SuggestPrReq, user: Principal = Depends(get_current_user)):
    result = _git(git_tools.git_diff_range, req.base)
    if _is_denied(result):
        return result
    if "error" in result:
        return JSONResponse(result, status_code=400)
    diff_text = result.get("diff", "")
    if not diff_text.strip():
        return JSONResponse({"error": f"no diff against '{req.base}' to summarize"}, status_code=400)
    try:
        summary = await git_ai.generate_pr_summary(diff_text, user.id)
    except Exception as e:
        return JSONResponse({"error": f"generation failed: {e}"}, status_code=502)
    return summary


class PrReq(BaseModel):
    title: str
    body: str = ""
    base: str = "main"
    head: Optional[str] = None


@router.post("/pr")
async def create_pr(req: PrReq, user: Principal = Depends(require_permission("git.push"))):
    if not mcp_core.is_ready("github"):
        return JSONResponse({
            "error": "GitHub is not connected. Open Settings -> Capabilities -> MCP Servers "
                     "and click Connect on the GitHub card first.",
        }, status_code=400)

    remote_url = _git(git_tools.git_remote_url, "origin")
    if _is_denied(remote_url):
        return remote_url
    owner_repo = git_tools.parse_github_owner_repo(remote_url) if remote_url else None
    if not owner_repo:
        return JSONResponse({"error": "could not resolve a GitHub owner/repo from 'origin' remote"}, status_code=400)
    owner, repo = owner_repo

    head = req.head
    if not head:
        st = _git(git_tools.git_status)
        if _is_denied(st):
            return st
        if "error" in st:
            return JSONResponse(st, status_code=400)
        head = st.get("branch")
    if not head:
        return JSONResponse({"error": "could not resolve current branch"}, status_code=400)

    args = {
        "owner": owner, "repo": repo,
        "title": req.title, "body": req.body,
        "base": req.base, "head": head,
    }
    result = await registry.async_run("mcp__github__create_pull_request", args)
    if isinstance(result, str) and result.startswith("error:"):
        return JSONResponse({"error": result}, status_code=502)
    return {"ok": True, "result": result}
