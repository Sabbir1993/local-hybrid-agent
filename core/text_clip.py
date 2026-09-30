"""Shortening long tool output for the model, with a note that says what happened and what to do.

A small model that sees text simply stop tends to decide "the display is truncating" and change approach
(typically to run_python). The note therefore states that the tool finished normally and names the ways to
get the rest. One wording is shared by the history cap (agent_loop/tool_output), the executor's view
(agent_loop/executor_view) and run_shell (shell_tools).
"""

SLACK = 500            # room for the note, so a clipped text is never clipped again


def omission_note(omitted: int) -> str:
    return (f"\n... [{omitted} characters omitted to keep the conversation small. The tool finished normally and the "
            "output was shortened by the app, this is not a display problem. For the rest: read_file_chunk on a "
            "narrower range, filter the command's output (findstr / Select-String), or save it with write_file "
            "and read part of it] ...\n")


def clip_head_tail(text: str, cap: int, head_frac: float = 0.7) -> str:
    """`text` unchanged when within cap (+ slack); else its start and end with the omission note in between.
    head_frac is the share kept from the start: 0.7 suits files (headers, errors), 0.2 suits command
    output (the result is at the end)."""
    if not cap or not isinstance(text, str) or len(text) <= cap + SLACK:
        return text
    head = int(cap * head_frac)
    tail = cap - head
    return text[:head].rstrip() + omission_note(len(text) - head - tail) + text[-tail:].lstrip()
