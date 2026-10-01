"""Too many separate run_python scripts in one run: each one interrupts the user for approval.

Pure helpers so the rule can be unit-tested; routes/agent/run.py counts the calls and injects the message.
"""


def should_nudge(before: int, after: int, limit: int) -> bool:
    """True when the script count reaches the limit or a further multiple of it (so it never spams every step)."""
    return limit > 0 and after // limit > before // limit


def message(python_calls: int) -> str:
    # starts with a prefix the executor view treats as loop-injected, not as the user's request
    return (f"[continue] You have run {python_calls} separate Python scripts in this task, and each one needs the user's "
            "approval. To look at files use grep, read_file (offset, limit) or list_files - they need no approval. "
            "If you really need to run code, put the steps into ONE script, or tell the user what you need.")
