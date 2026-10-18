"""
core/kb_guard.py - guardrail for knowledge-base READS.

Policy (decision 2026-10-18): nothing is guarded when a document is uploaded -- it is stored and
indexed verbatim. The guard applies when the data is accessed: text that leaves the KB toward a
model prompt, a tool result or the client is masked here, with the same detector as chat input,
model output and cloud egress (core/pan.py, Bangladesh-first). Config: config/app.json
"pci": {"pan_kb_read": "off"} disables it; anything else keeps it on.
"""

import functools

from . import pan


def active() -> bool:
    return pan.enabled("pan_kb_read")


def guard_text(text):
    """Mask card numbers (and CVVs next to their keyword) in text read from the knowledge base."""
    if not text or not isinstance(text, str) or not active():
        return text
    return pan.mask_pans(text)[0]


def guarded_tool(fn):
    """Decorator for async KB tools returning text: the result is masked on the way out."""
    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        return guard_text(await fn(*a, **kw))
    return wrapper
