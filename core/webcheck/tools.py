from urllib.parse import urljoin

from ..browser_tools.bridge import _base, _clip, _err, _mask, call_companion, handle_screenshot
from .audit import audit_page
from .constants import REPORT_DIR, max_flows, max_pages
from .flows import run_flow
from .report import diff_reports, render_audit, render_markdown, summarize
from . import store

_SHOT_QUESTION = ("Describe this web page screenshot for a developer checking their app: "
                  "layout, visible text, anything broken, overlapping, cut off or unstyled.")


def _headless(args: dict) -> bool:
    return str(args.get("show")).strip().lower() not in ("1", "true", "yes")


async def tool_browser_audit(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "error: url required"
    want_shot = args.get("screenshot", True) is not False
    try:
        d = await audit_page(url, _base(args)["session"], device=args.get("device"),
                             headless=_headless(args), screenshot=want_shot)
        text = render_audit(d)
        if d.get("png_b64"):
            text += "\n\n" + await handle_screenshot("browser_audit", {"describe": bool(args.get("describe"))},
                                                     d["png_b64"], "audit", _SHOT_QUESTION)
    except Exception as e:
        return _err(e)
    return _clip(_mask(text))


async def _close(session: str) -> None:
    try:
        await call_companion("browser.close", {"session": session})
    except Exception:
        pass


async def tool_browser_check(args: dict) -> str:
    base = str(args.get("base_url") or "").strip()
    if not base:
        return "error: base_url required"
    paths = [str(p) for p in (args.get("pages") or ["/"]) if str(p).strip()][:max_pages()] or ["/"]
    flows = [f for f in (args.get("flows") or []) if isinstance(f, dict)][:max_flows()]
    device, headless = args.get("device"), _headless(args)
    run_id = store.new_run_id()
    report = {"run_id": run_id, "base_url": base, "device": device or "desktop", "pages": [], "flows": []}
    shots = args.get("screenshot") is True
    try:
        prev = await store.load_report(str(args["compare_to"])) if args.get("compare_to") else await store.previous_report(base)
        sess = f"webcheck-{run_id}-pages"
        try:
            for n, path in enumerate(paths, 1):
                url = urljoin(base.rstrip("/") + "/", path)
                d = await audit_page(url, sess, device=device, headless=headless, screenshot=shots)
                if d.get("png_b64"):
                    await store.write_b64(f"{REPORT_DIR}/{run_id}/page-{n}.png", d.pop("png_b64"))
                d["path"] = path
                report["pages"].append(d)
        finally:
            await _close(sess)
        for n, flow in enumerate(flows, 1):
            sess = f"webcheck-{run_id}-flow{n}"
            try:
                report["flows"].append(await run_flow(flow, base, sess, device=device, headless=headless))
            finally:
                await _close(sess)
        report["summary"] = summarize(report)
        md = _mask(render_markdown(report, diff_reports(prev, report)))
        where = await store.save_report(report, md)
    except Exception as e:
        return _err(e)
    tail = f"\n\nreport saved: {where}/report.md  (run_id {run_id}; re-run browser_check after a fix to see what changed)" \
        if where else "\n\n(report could not be saved to the project)"
    return _clip(md + tail)


async def tool_browser_check_report(args: dict) -> str:
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return "error: run_id required"
    try:
        rep = await store.load_report(run_id)
        if not rep:
            return f"error: no report for run {run_id}"
        prev = await store.load_report(str(args["compare_to"])) if args.get("compare_to") else None
    except Exception as e:
        return _err(e)
    return _clip(_mask(render_markdown(rep, diff_reports(prev, rep))))
