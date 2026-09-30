"""Argument names models actually use for a tool's main input.

Small and cloud models alike pass a shell command as `code`, `cmd` or `script` (the name of the
neighbouring run_python parameter, or a habit from another tool). Resolving those in ONE place,
before the permission gate and before the tool runs, means the approval prompt and the tool always
look at the same command: an alias can never reach the tool without passing the gate.
"""

SHELL_COMMAND_KEYS = ("command", "cmd", "code", "script", "shell", "command_line", "commandline", "line")


def shell_command(args) -> str:
    """The shell command line in `args` under any of the accepted names, else ''."""
    if not isinstance(args, dict):
        return ""
    for key in SHELL_COMMAND_KEYS:
        val = args.get(key)
        if isinstance(val, (list, tuple)):          # ["ls", "-la"] -> "ls -la"
            val = " ".join(str(x) for x in val)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""
