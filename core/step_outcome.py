"""What one model turn of the agent loop produced, as a code.

Failure is decided from what the turn did (did it call a tool, was it empty, did
the transport fail), not from guessing at its wording. Codes only - never text -
so they are safe to store in the routing tables (core/route_log.py).
"""

TOOL_CALL = "tool_call"
NO_TOOL_CALL = "no_tool_call"
EMPTY = "empty"
PARSE_FAIL = "parse_fail"
TRANSPORT_ERROR = "transport_error"
LOOP = "loop"
FINISH = "finish"      # ended the turn with finish(answer)

ALL = (TOOL_CALL, NO_TOOL_CALL, EMPTY, PARSE_FAIL, TRANSPORT_ERROR, LOOP, FINISH)

# Outcomes that mean the turn did not do what an action request needs.
_FAILED_ACTION = frozenset({NO_TOOL_CALL, EMPTY, PARSE_FAIL, TRANSPORT_ERROR, LOOP, FINISH})


def classify_step(content: str, tool_calls, *, is_loop: bool = False,
                  parse_failed: bool = False, finished: bool = False) -> str:
    """Code for one model turn from what it emitted."""
    if is_loop:
        return LOOP
    if finished:
        return FINISH if (content or "").strip() else EMPTY
    if tool_calls:
        return PARSE_FAIL if parse_failed else TOOL_CALL
    if not (content or "").strip():
        return EMPTY
    return NO_TOOL_CALL


def failed(outcome: str, action_expected: bool) -> bool:
    """True when `outcome` is a failure for this kind of request.

    A plain-text reply is a good answer to a question or greeting but a failure
    for a request that needs tools; a loop or transport error fails either way.
    """
    if outcome in (LOOP, TRANSPORT_ERROR, PARSE_FAIL):
        return True
    return action_expected and outcome in _FAILED_ACTION
