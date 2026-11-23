"""Open a URL in the paired companion's visible Test Browser window.

Used by the dock preview when a user navigates to an external site: the app's
own browser on the device, not the system default browser.
"""
from fastapi import Depends
from pydantic import BaseModel

from core import companion_bridge
from core.auth import Principal
from core.deps import get_current_user

from .base import router


class OpenBrowserBody(BaseModel):
    url: str


@router.post("/control/test-browser/open")
async def open_test_browser(body: OpenBrowserBody, user: Principal = Depends(get_current_user)):
    url = (body.url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "error": "only http(s) URLs can be opened"}
    try:
        await companion_bridge.call(user.id, "browser.window.open", {"url": url}, timeout=60)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
