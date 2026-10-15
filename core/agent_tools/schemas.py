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
            "name": "project_overview",
            "description": "Get the shape of the project in ONE call: top-level areas with file counts, languages, likely entry points, and the first lines of the README and manifests (package.json, pyproject.toml, ...). Call this FIRST when asked to analyze, explore, understand or document a project, instead of many list_files/read_file steps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "optional sub-folder to summarize instead of the workspace root"},
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
            "description": "Search the organizational company knowledge base (internal company documents, employee records, policies, services, guidelines). Returns authentic company records. "
                           "Use it ONLY when the answer needs company knowledge that is not already in this conversation. Do NOT use it to "
                           "summarise or analyse a file or data the user attached or pasted (that data is already in the message), for general "
                           "knowledge, or for small talk. If a knowledge-base block is already in your instructions, answer from it instead of searching again.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A SHORT question or a few key terms, at most ~15 words (e.g. 'engineering managers', 'leave policy', 'MIOT benefits', 'company overview'). Never paste file text or the whole user message."
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_find_mentions",
            "description": "List EVERY line in the company knowledge base that mentions a person or exact term, across all documents "
                           "(search_knowledge_base returns only the 6 best matches). Use it for 'everything about <employee>', 'where does "
                           "<name> appear', 'employee details of <name>'. Returns quoted lines grouped by document with table column "
                           "headers; build the table from those lines only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The person's name or exact term, e.g. 'Rahim Uddin' (case-insensitive, whole words)"}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_sql",
            "description": "Read-only SQL (SQLite) over the Excel/CSV files in the company knowledge base: joins between sheets, "
                           "pivots with SUM(CASE WHEN ...), sums / averages / medians / percentiles, several group-bys, month "
                           "buckets with strftime, top-N. Call with describe=true first to see the table and column names. "
                           "Use kb_query for a simple count or list; use this for anything that needs totals, ratios or joins. "
                           "Not available while a cloud model answers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "describe": {"type": "boolean", "description": "List the tables and columns instead of running a query"},
                    "source": {"type": "string", "description": "Only tables whose document title or sheet name contains this text"},
                    "sql": {"type": "string", "description": "one SELECT (or WITH ... SELECT) statement"},
                    "limit": {"type": "integer", "description": "max rows returned (default 50, max 200)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_query",
            "description": "Exact counts, lists and group-bys over the Excel/CSV files in the company knowledge base (staff lists, rosters, "
                           "registers). Use it for 'how many X', 'list all Y where ...', 'count by department'. Call it first with "
                           "describe=true to see the available tables and their columns. Answers come from the real rows, not from guessing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "describe": {"type": "boolean", "description": "List the tables and their column names instead of querying"},
                    "source": {"type": "string", "description": "Only tables whose document title or sheet name contains this text"},
                    "filters": {
                        "type": "array",
                        "description": "All must match. Each: {column, op, value}. op is one of =, !=, contains, starts_with, >, >=, <, <=, in (value is a list), is_empty, not_empty",
                        "items": {"type": "object", "properties": {
                            "column": {"type": "string"}, "op": {"type": "string"},
                            "value": {"description": "text, number or list (for in)"}},
                            "required": ["column"]},
                    },
                    "select": {"type": "array", "items": {"type": "string"}, "description": "Columns to show (default: all)"},
                    "group_by": {"type": "string", "description": "Count rows per distinct value of this column"},
                    "count": {"type": "boolean", "description": "Only return how many rows match"},
                    "limit": {"type": "integer", "description": "Max rows to list (default 50, max 200)"},
                },
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
        "description": "Create or write a file in the shared common space and share a downloadable link with the user. Use this whenever the user asks to generate, create, fill, or save data to an Excel (.xlsx), PowerPoint (.pptx), Word (.docx), CSV, code, or document file. A PowerPoint/PPT/slides request MUST use a .pptx filename (never .md): it is built into a real slide deck.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "A filename that describes THIS file's actual content, with the correct extension for the format requested (e.g. quarterly_sales.xlsx, user_report.csv, fetch_data.py) - never reuse a name or extension from an earlier example or an earlier file in this conversation unless the user explicitly asked to edit that exact file."
                },
                "content": {
                    "type": "string",
                    "description": "The complete file content to write. For spreadsheets (.xlsx, .csv), provide formatted CSV rows or markdown table rows. For a .pptx deck, write markdown: a '# Title' line per slide, bullet lines under it, and slides separated by a line containing only '---'. For .docx, plain markdown."
                }
            },
            "required": ["path", "content"]
        }
    }
}
