"""Reports live on the user's device (.agent/webcheck/), written through the companion -
never on the server's disk."""

import json
import re
import time
from typing import Optional

from ..browser_tools.bridge import _device, call_companion
from .constants import REPORT_DIR


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "site"


def _abs(rel: str) -> str:
    return str(_device()[1] / rel)


async def write_text(rel: str, text: str) -> bool:
    try:
        await call_companion("fs.write", {"path": _abs(rel), "content": text}, timeout=30)
        return True
    except Exception:
        return False


async def write_b64(rel: str, b64: str) -> bool:
    try:
        await call_companion("fs.write_b64", {"path": _abs(rel), "data": b64}, timeout=30)
        return True
    except Exception:
        return False


async def read_text(rel: str) -> Optional[str]:
    try:
        return (await call_companion("fs.read", {"path": _abs(rel)}, timeout=30)).get("content")
    except Exception:
        return None


async def load_report(run_id: str) -> Optional[dict]:
    if not re.fullmatch(r"[0-9A-Za-z_-]{1,40}", run_id or ""):
        return None
    raw = await read_text(f"{REPORT_DIR}/{run_id}/report.json")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


async def previous_report(base_url: str) -> Optional[dict]:
    run_id = (await read_text(f"{REPORT_DIR}/latest-{_slug(base_url)}.txt") or "").strip()
    return await load_report(run_id) if run_id else None


async def save_report(report: dict, markdown: str) -> str:
    """Write report.json/report.md and point 'latest' at this run. Returns the report's relative dir ('' on failure)."""
    rel = f"{REPORT_DIR}/{report['run_id']}"
    ok = await write_text(f"{rel}/report.json", json.dumps(report, indent=1))
    await write_text(f"{rel}/report.md", markdown)
    if ok:
        await write_text(f"{REPORT_DIR}/latest-{_slug(report['base_url'])}.txt", report["run_id"])
    return rel if ok else ""
