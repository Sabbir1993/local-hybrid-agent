"""Machine-readable lifecycle events for the operator.

print() is console scrollback: it dies with the terminal and cannot be
queried. The DB event store (core/route_log) holds run/tool traffic, but the
supervisor and the autotune sweep only printed, so "why did the server keep
restarting at 3am" had no answer that survived process exit. This module
appends one JSON line per event to logs/events-YYYYMMDD.jsonl - queryable
with jq/Select-String, day-rotated and pruned like the llama-server log.

Rules:
- values are bounded: strings are truncated, so no field can smuggle a
  file dump or a user prompt into retained storage
- never raises: a logging failure must not take the model server down
  (same contract as state._write_log_file)
- LOG_WRITE_FILE=False keeps the machine file-free and print-only, same
  switch the llama-server log honours
"""
import json
import time

from .config import LOG_DIR, LOG_KEEP_DAYS, LOG_WRITE_FILE

_MAX_FIELD = 200

_fh = None
_day = ""


def log_event(kind: str, **fields) -> None:
    """Record kind (dot.notation, e.g. server.restart) with bounded fields."""
    if not LOG_WRITE_FILE:
        return
    if not kind or not kind.replace(".", "").replace("_", "").isalnum():
        return
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind}
    for k, v in fields.items():
        if isinstance(v, str):
            record[k] = v[:_MAX_FIELD]
        elif isinstance(v, (int, float, bool)) or v is None:
            record[k] = v
        else:
            record[k] = json.dumps(v)[:_MAX_FIELD]
    global _fh, _day
    try:
        day = time.strftime("%Y%m%d")
        if _fh is None or day != _day:
            if _fh is not None:
                try:
                    _fh.close()
                except Exception:
                    pass
                _fh = None
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _fh = open(LOG_DIR / f"events-{day}.jsonl", "a", encoding="utf-8",
                       errors="replace")
            _day = day
            _prune()
        _fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        _fh.flush()
    except Exception:
        _fh = None          # stop trying every record after a failure


def close() -> None:
    global _fh, _day
    if _fh is not None:
        try:
            _fh.close()
        except Exception:
            pass
    _fh, _day = None, ""


def _prune() -> None:
    try:
        files = sorted(LOG_DIR.glob("events-*.jsonl"))
        for old in files[:-LOG_KEEP_DAYS]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass
