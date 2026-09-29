from .bridge import (
    _base,
    _clip,
    _err,
    _mask,
    _page_text,
    _target,
    call_companion,
    handle_screenshot,
)


async def tool_browser_navigate(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "error: url required"
    try:
        d = await call_companion("browser.navigate", {
            **_base(args), "url": url, "device": args.get("device"),
            "headless": not bool(args.get("show")), "timeout_ms": 30000}, timeout=120)
    except Exception as e:
        return _err(e)
    return _page_text(d)


async def tool_browser_snapshot(args: dict) -> str:
    try:
        return _page_text(await call_companion("browser.snapshot", _base(args)))
    except Exception as e:
        return _err(e)


async def tool_browser_click(args: dict) -> str:
    if not _target(args):
        return "error: give ref (from the snapshot), selector or text"
    try:
        d = await call_companion("browser.click", {**_base(args), **_target(args),
                                                   "double": bool(args.get("double")),
                                                   "button": args.get("button")})
    except Exception as e:
        return _err(e)
    return _page_text(d)


async def tool_browser_type(args: dict) -> str:
    if not _target(args):
        return "error: give ref (from the snapshot), selector or text"
    try:
        d = await call_companion("browser.type", {**_base(args), **_target(args),
                                                  "text_value": str(args.get("value", "")),
                                                  "submit": bool(args.get("submit")),
                                                  "clear": args.get("clear", True)})
    except Exception as e:
        return _err(e)
    return _page_text(d)


async def tool_browser_press(args: dict) -> str:
    try:
        return _page_text(await call_companion("browser.press", {**_base(args), "key": args.get("key") or "Enter"}))
    except Exception as e:
        return _err(e)


async def tool_browser_select(args: dict) -> str:
    try:
        return _page_text(await call_companion("browser.select", {**_base(args), **_target(args),
                                                                  "values": args.get("values")}))
    except Exception as e:
        return _err(e)


async def tool_browser_wait(args: dict) -> str:
    try:
        return _page_text(await call_companion("browser.wait", {**_base(args), "text": args.get("text"),
                                                                "timeout_ms": args.get("timeout_ms")}))
    except Exception as e:
        return _err(e)


async def tool_browser_screenshot(args: dict) -> str:
    try:
        d = await call_companion("browser.screenshot", {**_base(args), "full_page": bool(args.get("full_page"))},
                                 timeout=60)
    except Exception as e:
        return _err(e)
    q = args.get("question") or ("Describe this web page screenshot for a developer checking their app: "
                                 "layout, visible text, anything broken, overlapping, cut off or unstyled.")
    shot = await handle_screenshot("browser_screenshot", args, d.get("png_b64", ""), "browser", q)
    return _clip(_mask(f"url: {d.get('url', '')}\ntitle: {d.get('title', '')}\n{shot}"))


async def tool_browser_console(args: dict) -> str:
    try:
        d = await call_companion("browser.console", _base(args))
    except Exception as e:
        return _err(e)
    con = d.get("console") or []
    net = d.get("network") or []
    lines = [f"url: {d.get('url', '')}", f"console errors/warnings: {len(con)}"]
    lines += [f"  [{c.get('type')}] {c.get('text')}" for c in con[-60:]]
    lines.append(f"failed / 4xx-5xx requests: {len(net)}")
    lines += [f"  {n.get('method', '')} {n.get('url')} -> {n.get('status') or n.get('failure')}" for n in net[-60:]]
    if not con and not net:
        lines.append("(no errors since the last navigation)")
    return _clip(_mask("\n".join(lines)))


async def tool_browser_eval(args: dict) -> str:
    expr = str(args.get("expression") or "").strip()
    if not expr:
        return "error: expression required"
    try:
        d = await call_companion("browser.eval", {**_base(args), "expression": expr}, timeout=180)
    except Exception as e:
        return _err(e)
    return _clip(_mask(f"url: {d.get('url', '')}\nresult:\n{d.get('result', '')}"))


async def tool_browser_close(args: dict) -> str:
    try:
        d = await call_companion("browser.close", _base(args))
    except Exception as e:
        return _err(e)
    return "browser closed" if d.get("closed") else "no browser was open"
