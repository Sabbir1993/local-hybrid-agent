"""Click-through flows. Steps reuse the companion's browser.* ops; only audit is new on the device.

An `expect` that fails is recorded as an issue and the flow carries on, so one broken assertion
does not hide the rest. A step that throws (page won't load, element missing) fails the flow and
stops it; the other flows still run.
"""

from urllib.parse import urljoin

from ..browser_tools.bridge import _target, call_companion
from .constants import max_steps

STEP_KINDS = ("navigate", "click", "type", "press", "select", "wait", "expect")


async def _snapshot(session: str) -> str:
    return str((await call_companion("browser.snapshot", {"session": session})).get("snapshot") or "")


async def run_flow(flow: dict, base_url: str, session: str, *, device: str = None, headless: bool = True) -> dict:
    name = str(flow.get("name") or "flow")[:60]
    steps = [s for s in (flow.get("steps") or []) if isinstance(s, dict)][:max_steps()]
    res = {"name": name, "ok": True, "steps": [], "issues": []}
    snap = ""
    for n, step in enumerate(steps, 1):
        do = str(step.get("do") or "")
        try:
            if do not in STEP_KINDS:
                raise ValueError(f"unknown step '{do}' (use {', '.join(STEP_KINDS)})")
            if do == "navigate":
                d = await call_companion("browser.navigate", {
                    "session": session, "url": urljoin(base_url.rstrip("/") + "/", str(step.get("url") or "")),
                    "device": device, "headless": headless, "timeout_ms": 30000}, timeout=120)
                if d.get("error"):
                    raise RuntimeError(d["error"])
                snap = str(d.get("snapshot") or "")
            elif do in ("click", "type", "select"):
                if not _target(step):
                    raise ValueError("give ref, selector or text")
                params = {"session": session, **_target(step)}
                if do == "type":
                    params.update(text_value=str(step.get("text", "")), submit=bool(step.get("submit")))
                if do == "select":
                    params["values"] = step.get("values")
                snap = str((await call_companion(f"browser.{do}", params)).get("snapshot") or "")
            elif do == "press":
                d = await call_companion("browser.press", {"session": session, "key": step.get("key") or "Enter"})
                snap = str(d.get("snapshot") or "")
            elif do == "wait":
                d = await call_companion("browser.wait", {"session": session, "text": step.get("text"),
                                                          "timeout_ms": step.get("timeout_ms")})
                snap = str(d.get("snapshot") or "")
            else:   # expect
                snap = snap or await _snapshot(session)
                hay = snap.casefold()
                want, nope = step.get("contains"), step.get("not_contains")
                if want is None and nope is None:
                    raise ValueError("expect needs contains or not_contains")
                failed = (want is not None and str(want).casefold() not in hay) or \
                         (nope is not None and str(nope).casefold() in hay)
                if failed:
                    msg = f'expected "{want}" on the page' if want is not None else f'did not expect "{nope}" on the page'
                    res["issues"].append({"kind": "expect_failed", "severity": "error", "message": msg, "selector": None})
                res["steps"].append({"n": n, "do": do, "ok": not failed})
                continue
            res["steps"].append({"n": n, "do": do, "ok": True})
        except Exception as e:
            res["ok"] = False
            res["error"] = f"step {n} ({do or '?'}): {e}"[:300]
            res["steps"].append({"n": n, "do": do, "ok": False})
            break
    if res["ok"] and any(i["kind"] == "expect_failed" for i in res["issues"]):
        res["ok"] = False
    if res["steps"]:
        try:   # what the clicks left behind: console errors, 4xx/5xx, broken markup
            a = await call_companion("browser.audit", {"session": session, "screenshot": False}, timeout=60)
            res["issues"] += a.get("issues") or []
        except Exception:
            pass
    return res
