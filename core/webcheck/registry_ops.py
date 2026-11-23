from ..browser_tools.constants import DEVICES
from ..browser_tools.registry_ops import _fn
from .tools import tool_browser_audit, tool_browser_check, tool_browser_check_report, tool_browser_preview

_STEP = {"type": "object", "description": (
    "one of: {do:'navigate',url} {do:'click',ref|selector|text} {do:'type',ref|selector|text,text,submit?} "
    "{do:'press',key} {do:'select',ref|selector|text,values} {do:'wait',text?} "
    "{do:'expect',contains|not_contains} - expect checks the page snapshot; a failed expect is reported, not fatal")}

WEBCHECK_TOOLS = [
    ("browser_preview", tool_browser_preview, _fn(
        "browser_preview",
        "Call this right after building a web app or report: opens it in a VISIBLE browser on the user's device "
        "and runs a read-only UI audit (console/network errors, broken images, missing labels, overflow, blank render). "
        "The findings come back here for you to fix. Report-only - a finding does not block the answer.",
        {"url": {"type": "string", "description": "local dev URL (e.g. http://localhost:5173) or a file:// path inside the approved workspace"},
         "device": {"type": "string", "enum": DEVICES},
         "screenshot": {"type": "boolean", "description": "save a screenshot (default true)"},
         "describe": {"type": "boolean", "description": "also describe the screenshot with the vision model"}},
         ["url"])),
    ("browser_audit", tool_browser_audit, _fn(
        "browser_audit",
        "Check ONE page of a web app in a single call: console errors, failed/4xx/5xx requests, broken images, "
        "missing alt text, unnamed buttons/links, unlabelled form fields, sideways overflow, blank render. "
        "Read-only (never clicks). Use after building or changing a page; fix the issues, then call it again.",
        {"url": {"type": "string", "description": "e.g. http://localhost:5173/login"},
         "device": {"type": "string", "enum": DEVICES},
         "screenshot": {"type": "boolean", "description": "save a screenshot to .agent/screens (default true)"},
         "describe": {"type": "boolean", "description": "also describe the screenshot with the vision model"},
         "show": {"type": "boolean", "description": "watch it run in a visible window (default headless)"}},
        ["url"])),
    ("browser_check", tool_browser_check, _fn(
        "browser_check",
        "Check a whole web app: audit several pages and run click-through flows, then save a report under "
        ".agent/webcheck/<run_id>/ and show what is NEW or FIXED since the previous run of the same base_url. "
        "Run it, fix the errors it lists, run it again to verify.",
        {"base_url": {"type": "string", "description": "e.g. http://localhost:5173"},
         "pages": {"type": "array", "items": {"type": "string"}, "description": "paths to audit (default ['/'])"},
         "flows": {"type": "array", "items": {"type": "object", "properties": {
             "name": {"type": "string"}, "steps": {"type": "array", "items": _STEP}}}},
         "device": {"type": "string", "enum": DEVICES},
         "screenshot": {"type": "boolean", "description": "save a screenshot per page into the report folder"},
         "compare_to": {"type": "string", "description": "run_id to diff against (default: the previous run)"},
         "show": {"type": "boolean"}},
        ["base_url"])),
    ("browser_check_report", tool_browser_check_report, _fn(
        "browser_check_report", "Re-read a saved browser_check report, optionally diffed against another run.",
        {"run_id": {"type": "string"}, "compare_to": {"type": "string"}}, ["run_id"])),
]
