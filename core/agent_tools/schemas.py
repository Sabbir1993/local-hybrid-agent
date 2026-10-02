AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files, inspect directory contents, view folder structure, or see what files exist in the workspace. Pattern supports globs like * or **/*",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob pattern, e.g. * or **/* or **/*.py"},
                    "path": {"type": "string", "description": "optional sub-folder to list instead of the workspace root"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a slice of a text file. Returns numbered lines (the numbers are not part of the file), the total line count and, when more remains, the offset to continue from. Read only the part you need.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "workspace-relative path"},
                    "offset": {"type": "integer", "description": "first line to read (1-based, default 1)"},
                    "limit": {"type": "integer", "description": "number of lines (default 200)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search workspace files with a regex; returns file:line: match. Use it to locate code before reading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "regex to search"},
                    "path": {"type": "string", "description": "optional sub-folder to search in"},
                    "glob": {"type": "string", "description": "optional file filter, e.g. *.py"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a NEW file (fails if it exists unless overwrite=true). One call carries at most a few thousand tokens: for anything longer than ~150 lines write a skeleton (imports, signatures, TODO markers) first, then add sections with append_file or edit_file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean", "description": "replace an existing file completely (rarely needed - prefer edit_file)"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Add text to the end of an existing file. Use it to build a large file section by section after write_file created the skeleton.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "the next section (about 2-3k tokens at most)"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Change a file you have read, either way: old_string/new_string replaces one block (old_string must be unique (add surrounding lines) or set replace_all; whitespace/indentation differences and slightly drifted lines are tolerated), or diff accepts a unified diff (@@ hunks with -/+ lines) and applies every hunk atomically. Send one form per call, not both.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "diff": {"type": "string",
                             "description": "unified diff to apply (diff -u or git patch format). Use instead of old_string/new_string when changing several places at once."},
                    "replace_all": {"type": "boolean"},
                },
                # only path is required: either old_string+new_string or diff
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_at_line",
            "description": "Insert text so its first line becomes line N of a file you have read (1 = top, total_lines+1 = end).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer", "description": "1-based line number the inserted text starts at"},
                    "text": {"type": "string"},
                },
                "required": ["path", "line", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Run a Python script on the user's machine; it needs the user's approval every time. Use it to run or test code you wrote. NEVER use it to read, search or list files - use grep, read_file and list_files (no approval needed).",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to execute"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_diff",
            "description": "List files the agent has created or modified this session",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revert",
            "description": "Undo your last edit to one file (steps=N undoes N edits; to_start=true goes back to the file as it was before you touched it this session). Also deletes a file you created.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "steps": {"type": "integer", "description": "how many edits to undo (default 1)"},
                    "to_start": {"type": "boolean"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Describe an image file in the workspace using the vision model (screenshots, diagrams, UI captures)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "question": {"type": "string", "description": "What to focus on, e.g. 'what error is shown?'"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Hybrid semantic + keyword search over workspace files and past sessions. Use for 'where did we...', 'how did we...', and finding relevant code by meaning.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_plan",
            "description": "Create the tracked task plan for this session: an ordered list of concrete steps. Call it first for any multi-step job, before changing files. 3-12 short steps, each one checkable outcome or one file section (never a whole large file in one step). Steps are done in order, one at a time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered step descriptions, e.g. ['inspect config.py', 'add retry helper to client.py', 'verify with run_python']",
                    },
                    "replace": {"type": "boolean", "description": "Only to discard a plan that is already in progress because the task changed."},
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_item",
            "description": "Mark one plan step as in_progress, done, or failed (by its 1-based step number). Call it immediately after each step finishes or fails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {"type": "integer", "description": "1-based step number from the plan"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "done", "failed"]},
                    "note": {"type": "string", "description": "optional short note, e.g. the error message"},
                },
                "required": ["item", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_plan",
            "description": "Show the current plan with per-step status. Use it to re-orient after an interruption or before summarizing.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Search the organizational company knowledge base (internal company documents, employee records, policies, services, guidelines). Returns authentic company records.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search term or question to search the company knowledge base for (e.g. 'engineering managers', 'leave policy', 'MIOT benefits', 'company overview')"
                    }
                },
                "required": ["query"],
            },
        },
    },
]

from .finish import FINISH_SCHEMA

AGENT_TOOLS.append(FINISH_SCHEMA)

AGENT_CORE_TOOLS = [
    t for t in AGENT_TOOLS
    if t["function"]["name"] in ("write_file", "append_file", "read_file", "edit_file", "list_files", "run_python", "search_knowledge_base")
]

CHAT_WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Create or write a file in the shared common space and share a downloadable link with the user. Use this whenever the user asks to generate, create, fill, or save data to an Excel (.xlsx), CSV, code, or document file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "A filename that describes THIS file's actual content, with the correct extension for the format requested (e.g. quarterly_sales.xlsx, user_report.csv, fetch_data.py) - never reuse a name or extension from an earlier example or an earlier file in this conversation unless the user explicitly asked to edit that exact file."
                },
                "content": {
                    "type": "string",
                    "description": "The complete file content to write. For spreadsheets (.xlsx, .csv), provide formatted CSV rows or markdown table rows."
                }
            },
            "required": ["path", "content"]
        }
    }
}
