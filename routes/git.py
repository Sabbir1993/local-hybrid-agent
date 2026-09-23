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
from core.auth import Principal
from core.deps import get_current_user, require_permission
from core.registry import registry

router = APIRouter(prefix="/git", tags=["git"])


@router.get("/status")
async def status():
    result = git_tools.git_status()
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.get("/branches")
async def branches():
    result = git_tools.git_branches()
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.get("/diff")
async def diff(path: Optional[str] = None, staged: bool = False):
    result = git_tools.git_diff(path, staged)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


class PathsReq(BaseModel):
    paths: list


@router.post("/stage")
async def stage(req: PathsReq):
    result = git_tools.git_stage(req.paths)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.post("/unstage")
async def unstage(req: PathsReq):
    result = git_tools.git_unstage(req.paths)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


class CommitReq(BaseModel):
    message: str


@router.post("/commit")
async def commit(req: CommitReq):
    result = git_tools.git_commit(req.message)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


class PushReq(BaseModel):
    remote: str = "origin"
    branch: Optional[str] = None


@router.post("/push")
async def push(req: PushReq, user: Principal = Depends(require_permission("git.push"))):
    result = git_tools.git_push(req.remote, req.branch)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.post("/suggest_commit_message")
async def suggest_commit_message(user: Principal = Depends(get_current_user)):
    staged = git_tools.git_diff(staged=True)
    diff_text = staged.get("diff", "")
    if not diff_text.strip():
        unstaged = git_tools.git_diff(staged=False)
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
    result = git_tools.git_diff_range(req.base)
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

    remote_url = git_tools.git_remote_url("origin")
    owner_repo = git_tools.parse_github_owner_repo(remote_url) if remote_url else None
    if not owner_repo:
        return JSONResponse({"error": "could not resolve a GitHub owner/repo from 'origin' remote"}, status_code=400)
    owner, repo = owner_repo

    head = req.head
    if not head:
        st = git_tools.git_status()
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
