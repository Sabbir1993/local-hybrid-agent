"""PCI DSS helper: find card numbers (PANs) leaked into workspace files.

Uses core/pan.py (Luhn + BIN-prefix check). Output is always masked to the
last 4 digits -- a raw PAN never reaches the model or the chat transcript.
File access goes through the companion (the user's own device).
"""

MAX_HITS = 50


def register(api):
    api.add_system_prompt(
        "The pci_masker plugin is active: use scan_file_for_pans to check logs, test "
        "fixtures or configs for leaked card numbers. Never print a full card number."
    )
    api.add_tool(
        "scan_file_for_pans",
        _scan_file,
        "Scan one workspace file for payment card numbers; returns masked hits with line numbers.",
        {"type": "object",
         "properties": {"path": {"type": "string", "description": "workspace-relative path"}},
         "required": ["path"]},
    )
    api.add_tool(
        "mask_pans_in_text",
        _mask_text,
        "Mask any card numbers in a piece of text to their last 4 digits.",
        {"type": "object",
         "properties": {"text": {"type": "string"}},
         "required": ["text"]},
    )


async def _scan_file(args: dict) -> str:
    from core import companion_bridge
    from core.agent_tools import _ws_resolve, require_device_workspace
    uid, _ = require_device_workspace()
    p = _ws_resolve(args.get("path") or "")
    data = await companion_bridge.call(uid, "fs.read", {"path": str(p)})
    text = data.get("content")
    if text is None:
        return f"error: file not found: {args.get('path')}"
    return scan_text(text, args["path"])


def scan_text(text: str, label: str) -> str:
    from core import pan
    hits, total = [], 0
    for lineno, line in enumerate(text.splitlines(), 1):
        masked, n = pan.mask_pans(line)
        if n:
            total += n
            if len(hits) < MAX_HITS:
                hits.append(f"  line {lineno}: {masked.strip()[:200]}")
    if not total:
        return f"{label}: no card numbers found"
    more = f"\n  ... (only the first {MAX_HITS} matching lines shown)" if len(hits) == MAX_HITS else ""
    return (f"{label}: {total} card number(s) found (masked) -- remove or tokenize them:\n"
            + "\n".join(hits) + more)


def _mask_text(args: dict) -> str:
    from core import pan
    masked, n = pan.mask_pans(str(args.get("text") or ""))
    return f"{masked}\n\n({n} card number(s) masked)"
