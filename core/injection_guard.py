"""core/injection_guard.py - what a run may do after it has read content an attacker could have written.

Fencing (core/prompt_fence.py) tells the model that a web page or a ticket is data. A model can still be talked
into acting on it, so the second layer does not rely on the model: once a run has ingested external content
("tainted"), actions that change or leak something need the user's approval card even where a saved rule or a
connector setting would otherwise have run them unasked.

  * a side-effecting MCP connector tool (send a message, create a ticket, post a comment, ...)
  * a shell command that is not plain read-only inspection

Not covered, on purpose: `bypass` mode (the user turned approvals off for this run), and file edits, which the
user reviews in the diff and can rewind.

External content = web pages and search results, the agent's browser, and whatever a connector returns (mail,
tickets, chat messages and documents are written by third parties). The user's own workspace files are not.
"""

import re
from typing import Optional

# tools whose results come from outside the user's machine and outside their control
_EXTERNAL_NAMES = frozenset({"web_fetch", "web_search", "web_search_images"})
_EXTERNAL_PREFIXES = ("browser_",)

# MCP tool names that read: used when the server gives no readOnlyHint annotation
_READ_VERB_RX = re.compile(r"^(get|list|search|read|fetch|find|query|describe|view|show|lookup|look_up|count|check|"
                           r"retrieve|browse|download|export|resolve|inspect|summari[sz]e|whoami|me|status)([_-]|$)", re.I)

# shell commands that only look at things. `git branch` and `git remote` are read-only only in their listing
# forms ("git branch -D x" is not), and options that write a file or run a program are refused everywhere.
_READ_ONLY_CMD_RX = re.compile(
    r"^(git\s+(status|diff|log|show|rev-parse)\b|git\s+branch(\s+(--list|-a|-r|-v|-vv))*\s*$|git\s+remote\s+-v\s*$|"
    r"dir\b|type\b|grep\b|findstr\b|ls\b|cat\b|pwd\b|where\b|echo\b|more\b|tree\b)", re.I)
_WRITING_OPTION_RX = re.compile(r"(?:^|\s)(?:--output\b|--ext-diff\b|--exec\b|--upload-pack\b|-c\s|-o\s)", re.I)
_SHELL_META_RX = re.compile(r"[&|<>^;`\r\n]|\$\(|%[^%\s]+%")

POLICIES = ("tainted", "always", "never")
DEFAULT_POLICY = "tainted"


def is_external_content_tool(name: str, read_only: Optional[bool] = None) -> bool:
    """Does a successful call of this tool put third-party-controlled text into the run?"""
    n = str(name or "")
    if n in _EXTERNAL_NAMES or n.startswith(_EXTERNAL_PREFIXES):
        return True
    return n.startswith("mcp__")           # a connector's answer is somebody else's data, read or write


def mcp_tool_is_read_only(tool: dict) -> bool:
    """From an MCP tool definition: the server's readOnlyHint when it gives one, else the tool's name.

    The MCP default for an unannotated tool is "may write", so an unrecognised verb counts as side-effecting:
    a wrongly gated read costs one click, a wrongly trusted write costs a leaked message."""
    ann = tool.get("annotations") or {}
    if isinstance(ann.get("readOnlyHint"), bool):
        return ann["readOnlyHint"]
    return bool(_READ_VERB_RX.match(str(tool.get("name") or "")))


def is_read_only_command(cmd: str) -> bool:
    """A single plain inspection command (no pipes, redirects, chaining or substitution)."""
    c = (cmd or "").strip()
    return (bool(c) and not _SHELL_META_RX.search(c) and not _WRITING_OPTION_RX.search(c)
            and bool(_READ_ONLY_CMD_RX.match(c)))


def policy(config: Optional[dict]) -> str:
    v = str(((config or {}).get("capabilities") or {}).get("mcp_write_approval") or DEFAULT_POLICY).lower()
    return v if v in POLICIES else DEFAULT_POLICY


def mcp_call_needs_approval(read_only: Optional[bool], tainted: bool, config: Optional[dict]) -> bool:
    """Should this connector call raise an approval card (bypass is decided by the caller)?
    `read_only` None (unknown, e.g. registered before classification existed) counts as side-effecting."""
    if read_only is True:
        return False
    mode = policy(config)
    return mode == "always" or (mode == "tainted" and tainted)


def shell_needs_approval_when_tainted(cmd: str, tainted: bool) -> bool:
    """A tainted run's shell commands are asked about unless they are plain inspection, whatever saved
    allow rules say."""
    return bool(tainted) and not is_read_only_command(cmd)


TAINT_NOTE = ("This run has read content from outside your machine (a web page, the agent's browser or a "
              "connector). Approve only if this action is what YOU asked for.")
