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


def was_cut_off(res_dict) -> bool:
    return bool(res_dict) and res_dict.get("finish_reason") == "length"
