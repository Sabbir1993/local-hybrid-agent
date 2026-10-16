"""
routes/sandbox.py - admin page for the isolated code runner (core/sandbox, tools/runner).

  GET    /sandbox/status        {configured, enabled, url, fingerprint, reachable, sandbox, ready, ...}
  POST   /sandbox/pair          {url, code} -> pairs with the one-time code, pins the certificate, runs the self-test
  POST   /sandbox/selftest      re-run the runner's self-test
  PUT    /sandbox/enabled       {enabled}  - switch the chat's code tool on / off (needs a passed self-test to switch on)
  DELETE /sandbox/runner        forget the runner (token and pinned certificate)

Every change needs users.manage with step-up and is written to the audit log (no code, no token, no pairing code in it).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.audit import audit_log
from core.auth import Principal
from core.deps import require_verified
from core.sandbox import RunnerError, admin

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


class PairBody(BaseModel):
    url: str
    code: str


class EnabledBody(BaseModel):
    enabled: bool


def _ip(request: Request):
    return request.client.host if request.client else None


@router.get("/status")
async def sandbox_status(user: Principal = Depends(require_verified("users.manage"))):
    return await admin.status()


@router.post("/pair")
async def sandbox_pair(body: PairBody, request: Request, user: Principal = Depends(require_verified("users.manage"))):
    try:
        out = await admin.pair(body.url, body.code)
    except RunnerError as e:
        audit_log(user, action="sandbox.pair", resource="runner", result="deny", detail={"reason": str(e)[:200]}, ip=_ip(request))
        raise HTTPException(status_code=400, detail=str(e))
    audit_log(user, action="sandbox.pair", resource="runner", result="allow",
              detail={"fingerprint": out["fingerprint"], "selftest_ok": bool(out["selftest"].get("ok"))}, ip=_ip(request))
    return out


@router.post("/selftest")
async def sandbox_selftest(request: Request, user: Principal = Depends(require_verified("users.manage"))):
    try:
        out = await admin.selftest()
    except RunnerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_log(user, action="sandbox.selftest", resource="runner", result="allow", detail={"ok": bool(out.get("ok"))}, ip=_ip(request))
    return out


@router.put("/enabled")
async def sandbox_enabled(body: EnabledBody, request: Request, user: Principal = Depends(require_verified("users.manage"))):
    try:
        admin.set_enabled(body.enabled)
    except RunnerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_log(user, action="sandbox.enabled", resource="runner", result="allow", detail={"enabled": body.enabled}, ip=_ip(request))
    return await admin.status()


@router.delete("/runner")
async def sandbox_forget(request: Request, user: Principal = Depends(require_verified("users.manage"))):
    admin.forget()
    audit_log(user, action="sandbox.forget", resource="runner", result="allow", ip=_ip(request))
    return await admin.status()
