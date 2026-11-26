"""Replies cut off by the output limit (finish_reason == "length").

A tool call that was cut off is thrown away, never repaired and run: a half-written
write_file would otherwise land on disk and be reported as done. The model is told to
send a smaller chunk. A cut-off plain-text reply is continued and joined instead."""

MAX_CUTOFF_RETRIES = 2

CUT_CALL_NOTICE = (
    "[cut off] Your last reply hit the output limit in the middle of a tool call, so that call was "
    "discarded and nothing was changed. Send a smaller piece, under about 100 lines. If the file already "
    "exists, do NOT recreate or rewrite it - it keeps everything written so far: continue with append_file "
    "(or edit_file) and send only the next section. Only for a file that does not exist yet: write_file a "
    "short skeleton first, then add one section per call.")

CUT_TEXT_NOTICE = (
    "[continue] Your last reply was cut off by the output limit. Continue exactly where it stopped. "
    "Do not repeat anything you already wrote.")

# arguments of a write_file/edit_file call that were not valid JSON (see repair.py)
INVALID_ARGS_ERROR = (
    "error: the arguments of this call were not valid JSON, most likely because the reply was cut off "
    "by the output limit. Nothing was changed. Send a smaller piece. If the file already exists, do NOT "
    "recreate or rewrite it: continue with append_file (or edit_file), one section per call. Only for a "
    "new file: write_file a short skeleton first.")


def _doc_advice(path) -> str:
    try:
        from core.agent_tools.limits import document_split_advice
        return document_split_advice(path)
    except Exception:
        return ""


def tool_call_path(tool_calls) -> str:
    """The file path named in the arguments of the (cut-off) calls, '' when none is readable."""
    import re
    for tc in tool_calls or []:
        fn = (tc or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, dict):
            v = args.get("path") or args.get("file") or args.get("filename")
            if v:
                return str(v)
        elif isinstance(args, str):
            m = re.search(r'"(?:path|file|filename)"\s*:\s*"([^"]+)"', args)
            if m:
                return m.group(1)
    return ""


def cut_call_notice(tool_calls=None) -> str:
    """CUT_CALL_NOTICE, or for a workbook / Word / slide / PDF call the way to build that file in pieces (append_file / a
    web-page skeleton do not apply to an Office file)."""
    advice = _doc_advice(tool_call_path(tool_calls))
    if not advice:
        return CUT_CALL_NOTICE
    return ("[cut off] Your last reply hit the output limit in the middle of a tool call, so that call was discarded and "
            "nothing was changed. " + advice)


def invalid_args_error(path="") -> str:
    advice = _doc_advice(path)
    return ("error: the arguments of this call were not valid JSON, most likely because the reply was cut off by the output "
            "limit. Nothing was changed. " + advice) if advice else INVALID_ARGS_ERROR


def was_cut_off(res_dict) -> bool:
    return bool(res_dict) and res_dict.get("finish_reason") == "length"
