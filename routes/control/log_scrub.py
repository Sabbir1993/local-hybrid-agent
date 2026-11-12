"""Makes a model-server log line safe to show in the UI: card numbers masked, secrets and server paths removed.

vLLM / llama-server print absolute paths (home folders, model folders), Hugging Face tokens when a download fails, and
sometimes the request headers of a failed call. /control/status and /control/log show these lines to signed-in users, so
they pass through here first."""
import re

from core.pan import mask_pans

_HF_TOKEN = re.compile(r"\bhf_[A-Za-z0-9]{6,}")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{6,}")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}")
# key=value / "key": "value" pairs whose key says it is a secret
_SECRET_PAIR = re.compile(r"(?i)\b([A-Za-z0-9_\-]*(?:api[_\-]?key|token|secret|passw(?:or)?d|authorization)[A-Za-z0-9_\-]*)"
                          r"(\"?\s*[:=]\s*\"?)(?!<)[^\s\"',;]+")
_API_KEY_FLAG = re.compile(r"(?i)(--api[_\-]key)(\s+|=)\S+")
# /home/x/models/y (two or more parts; URL paths such as /v1/chat/completions are left alone), C:\Users\x, \\host\share
_POSIX_PATH = re.compile(r"(?<![\w:/.\-])/(?!v\d+/)(?:[^\s'\"<>|:;,()\[\]{}\\/]+/)+[^\s'\"<>|:;,()\[\]{}\\/]*")
_WIN_PATH = re.compile(r"\b[A-Za-z]:[\\/][^\s'\"<>|:;,()\[\]{}]*")
_UNC_PATH = re.compile(r"\\\\[^\s'\"<>|:;,()\[\]{}\\]+\\[^\s'\"<>|:;,()\[\]{}]*")


def scrub_log_text(text) -> str:
    """One log line (or any text from the model server) with PANs masked, secrets redacted and absolute paths hidden."""
    s = str(text if text is not None else "")
    s = mask_pans(s)[0]
    s = _API_KEY_FLAG.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", s)
    s = _HF_TOKEN.sub("<token>", s)
    s = _BEARER.sub("Bearer <token>", s)
    s = _SK_KEY.sub("<token>", s)
    s = _SECRET_PAIR.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", s)
    s = _UNC_PATH.sub("<path>", s)
    s = _WIN_PATH.sub("<path>", s)
    s = _POSIX_PATH.sub("<path>", s)
    return s


def scrub_log_lines(lines) -> list:
    return [scrub_log_text(l) for l in (lines or [])]
