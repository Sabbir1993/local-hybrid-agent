"""Open a page and run the companion's fixed audit on it."""

from ..browser_tools.bridge import call_companion


async def audit_page(url: str, session: str, *, device: str = None, headless: bool = True,
                     screenshot: bool = False) -> dict:
    """Navigate (the companion's origin approval applies) then audit. A page that fails to load
    comes back as a one-issue result rather than raising, so a check can go on to the next page."""
    nav = await call_companion("browser.navigate", {"session": session, "url": url, "device": device,
                                                    "headless": headless, "timeout_ms": 30000}, timeout=120)
    if nav.get("error") or (nav.get("status") or 0) >= 400:
        msg = nav.get("error") or f"HTTP {nav.get('status')}"
        issue = {"kind": "load_failed", "severity": "error", "message": msg, "selector": None}
        return {"url": nav.get("url") or url, "title": nav.get("title", ""), "device": nav.get("device"),
                "issues": [issue], "dropped": 0, "error": msg,
                "summary": {"errors": 1, "warnings": 0, "checked": {}}}
    return await call_companion("browser.audit", {"session": session, "screenshot": screenshot}, timeout=60)
