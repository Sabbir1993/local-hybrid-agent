"""Replies cut off by the output limit (finish_reason == "length").

A tool call that was cut off is thrown away, never repaired and run: a half-written
write_file would otherwise land on disk and be reported as done. The model is told to
send a smaller chunk. A cut-off plain-text reply is continued and joined instead."""

MAX_CUTOFF_RETRIES = 2

CUT_CALL_NOTICE = (
    "[cut off] Your last reply hit the output limit in the middle of a tool call, so that call was "
    "discarded and nothing was changed. Send a smaller piece: create a file with a short skeleton "
    "(write_file), then add one section per call (append_file, or edit_file). "
    "Keep each call under about 100-150 lines.")

CUT_TEXT_NOTICE = (
    "[continue] Your last reply was cut off by the output limit. Continue exactly where it stopped. "
    "Do not repeat anything you already wrote.")

# arguments of a write_file/edit_file call that were not valid JSON (see repair.py)
INVALID_ARGS_ERROR = (
    "error: the arguments of this call were not valid JSON, most likely because the reply was cut off "
    "by the output limit. Nothing was changed. Send a smaller piece: create the file with a short "
    "skeleton (write_file), then add one section per call (append_file, or edit_file).")


def was_cut_off(res_dict) -> bool:
    return bool(res_dict) and res_dict.get("finish_reason") == "length"
