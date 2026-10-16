"""The chat tool `run_sandbox_python`: offered only when the runner is paired, switched on and answering, and only for a request
that needs custom code. The builders (doc_create, doc_edit, data_query) stay the first choice for everything they cover."""
import re

from . import admin, service

RUN_SANDBOX_SCHEMA = {"type": "function", "function": {
    "name": "run_sandbox_python",
    "description": ("Run Python on an isolated machine (no internet, no access to anything else) to make a file or an analysis that "
                    "the document tools cannot: a custom chart, a calculation over a data file, a merge or clean-up, a converted "
                    "file. Files the user has are available read-only in /in; every file your code writes into the current folder "
                    "is returned as a download. Use doc_create for decks, PDFs, workbooks and Word files, and data_query for "
                    "questions about an attached sheet."),
    "parameters": {"type": "object", "properties": {
        "code": {"type": "string", "description": "complete Python 3 program; print a short summary of the result"},
        "inputs": {"type": "array", "maxItems": 8, "items": {"type": "string"},
                   "description": "names of files in the user's files to make available in /in (read-only)"},
        "timeout_s": {"type": "integer", "minimum": 5, "maximum": 120, "description": "default 60"}},
        "required": ["code"]}}}

SANDBOX_MANUAL = (
    "CODE RUNNER (run_sandbox_python): your Python runs on an isolated machine with no internet and no pip. Use it ONLY when no "
    "builder fits. Input files named in `inputs` are in /in (read-only); write results into the CURRENT folder with relative paths "
    "(for example plt.savefig('chart.png'), wb.save('result.xlsx')) - each file written there comes back as a [DOWNLOAD: ...] "
    "link. Available: the standard library, openpyxl, python-pptx, python-docx, Pillow, lxml, matplotlib (Agg backend). One short "
    "program, under 60 seconds, print a one-line summary. Never put card numbers in code or files. If it fails, read the error, fix "
    "it, and run again (stopped after three failures). Reply with a short summary and the download links.")

_CODE_WORDS = re.compile(
    r"\b(python|script|pandas|numpy|matplotlib|scatter plot|histogram|box ?plot|regression|forecast(?:ing)?|simulat\w+|monte carlo|"
    r"correlation|statistical|custom (?:chart|graph|report|script|calculation)|plot (?:the|a|this|these)|run (?:some |a )?code)\b", re.I)
_DATA_WORDS = re.compile(r"\b(analy[sz]e|clean|dedup\w*|merge|join|pivot|transform|reshape|convert|extract|parse)\b", re.I)


def sandbox_wanted(query: str, has_data_attachment: bool = False) -> bool:
    """True for a request that needs custom code: a code word, or a data-handling verb over an attached data file."""
    q = str(query or "")
    return bool(_CODE_WORDS.search(q) or (has_data_attachment and _DATA_WORDS.search(q)))


async def available() -> bool:
    try:
        return await admin.ready()
    except Exception:
        return False


async def tool_run_sandbox_python(args: dict, uid) -> str:
    a = args or {}
    try:
        res = await service.run(uid, a.get("code"), a.get("inputs") or [], int(a.get("timeout_s") or 60))
    except service.SandboxRefused as e:
        return f"error: {e}"
    except Exception as e:                                  # never a traceback or an internal address in a tool result
        return f"error: the code runner failed ({type(e).__name__})"
    return res["text"]
