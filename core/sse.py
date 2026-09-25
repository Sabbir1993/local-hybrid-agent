"""core/sse.py - server-sent event formatting for tool activity.

Tool calls/results streamed to the browser carry model-written arguments and raw
tool output (file contents, web pages, KB hits). PANs in them are masked before
they leave the server (PCI DSS 3.3 - display masking); the model still sees the
unmasked tool result, only the UI copy is masked.
"""

import json

from . import pan


def mask_value(v):
    """PAN-mask every string inside a JSON-like value (dicts, lists, strings)."""
    if isinstance(v, str):
        return pan.mask_pans(v)[0]
    if isinstance(v, dict):
        return {k: mask_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [mask_value(x) for x in v]
    return v


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(mask_value(data))}\n\n"
