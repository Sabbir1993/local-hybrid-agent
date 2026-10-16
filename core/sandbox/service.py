"""Running one job on the runner and vetting what comes back. Nothing runs here: the app only sends text and files out and
accepts a short list of file types back, after the same card-number check every document goes through."""
import base64
import hashlib
import time
from pathlib import Path

from . import admin, client, config
from .client import RunnerError

MAX_CODE_CHARS = 60_000
MAX_INPUTS = 8
MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_OUTPUT_FILES = 10
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
OUTPUT_TYPES = {".xlsx", ".docx", ".pptx", ".pdf", ".csv", ".txt", ".md", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
_TEXT_TYPES = {".csv", ".txt", ".md", ".json"}
_DOC_TYPES = {".xlsx", ".docx", ".pptx", ".pdf"}
RATE_PER_10_MIN = 30
STOP_AFTER_FAILURES = 3
_runs: dict = {}
_fails: dict = {}


class SandboxRefused(RuntimeError):
    """Not an error of the runner: the request itself is not allowed. The message is for the model and the user."""


def _rate(uid) -> None:
    now = time.time()
    hist = [t for t in _runs.get(uid, []) if now - t < 600]
    if len(hist) >= RATE_PER_10_MIN:
        raise SandboxRefused("too many code runs in the last 10 minutes: wait a little")
    hist.append(now)
    _runs[uid] = hist
    if len(_runs) > 2000:
        _runs.clear()


def _pan_in(data: bytes, name: str) -> bool:
    """True when the file text contains a card number. Pictures cannot be read, so a picture is not covered by this check."""
    from .. import doc_ops
    ext = Path(name).suffix.lower()
    try:
        if ext in _TEXT_TYPES:
            text = data.decode("utf-8", "replace")
        elif ext in _DOC_TYPES:
            text = doc_ops.full_text(data, name)
        else:
            return False
    except Exception:
        return ext in _DOC_TYPES          # a document we cannot read is not one we can vouch for
    return doc_ops._pan_count(text) > 0


def _mask(text: str) -> str:
    from ..pan import mask_pans
    return mask_pans(str(text or ""))[0]


def _inputs(uid, names: list) -> list:
    from ..agent_tools import _common_resolve
    if len(names) > MAX_INPUTS:
        raise SandboxRefused(f"at most {MAX_INPUTS} input files")
    out = []
    for n in names:
        try:
            p = _common_resolve(str(n))
        except Exception:
            raise SandboxRefused(f"cannot find the file {n} in the user's files")
        if not p.is_file():
            raise SandboxRefused(f"cannot find the file {n} in the user's files")
        if p.stat().st_size > MAX_INPUT_BYTES:
            raise SandboxRefused(f"{p.name} is too large for the code runner")
        data = p.read_bytes()
        if _pan_in(data, p.name):
            raise SandboxRefused(f"{p.name} contains card numbers: the code runner is not approved for card data")
        out.append({"name": p.name, "b64": base64.b64encode(data).decode("ascii")})
    return out


def _save(uid, name: str, data: bytes) -> str:
    from .. import doc_store
    from ..agent_tools import _common_resolve
    from ..doc_ops.base import sha256
    p = Path(name)
    target = _common_resolve(doc_store.version_name(f"{doc_store.base_stem(p.name)}{p.suffix.lower()}", 1).replace("-v1-", "-"))
    target.write_bytes(data)
    doc_store.register(uid, target.name, target.suffix.lower().lstrip("."), location="common", sha256=sha256(data))
    doc_store.audit(uid, "sandbox.output", target.name, {"sha256": sha256(data), "bytes": len(data)})
    return target.name


async def run(uid, code: str, input_names: list, timeout_s: int = 60) -> dict:
    """-> {ok, text, files [names]}. Raises SandboxRefused for a request that must not go out."""
    from .. import doc_store, doc_ops
    if not await admin.ready():
        raise SandboxRefused("the code runner is not available right now")
    code = str(code or "")
    if not code.strip() or len(code) > MAX_CODE_CHARS:
        raise SandboxRefused(f"send code of 1-{MAX_CODE_CHARS} characters")
    try:
        doc_ops._check_ops_for_pan([{"op": "sandbox", "content": code}])
    except Exception:
        raise SandboxRefused("the code contains a payment card number; card numbers must not be sent to the runner")
    fails, when = _fails.get(uid, (0, 0.0))
    if fails >= STOP_AFTER_FAILURES and time.time() - when < 600:
        raise SandboxRefused("the last runs failed three times: stop retrying, tell the user what went wrong and what you tried")
    _rate(uid)
    files_in = _inputs(uid, list(input_names or []))
    try:
        r = await client.request(config.runner_url(), "POST", "/run",
                                {"code": code, "inputs": files_in, "timeout_s": int(timeout_s or 60)},
                                token=config.get_token(), pin=config.fingerprint(), allow_public=config.allow_public(),
                                timeout=int(timeout_s or 60) + 30)
    except RunnerError as e:
        raise SandboxRefused(str(e))
    res = r["json"]
    if r["status"] != 200 or ("error" in res and not res.get("stdout") and not res.get("stderr")):
        raise SandboxRefused(str(res.get("error") or f"the runner answered {r['status']}")[:200])
    saved, refused = [], []
    for f in (res.get("files") or [])[:MAX_OUTPUT_FILES]:
        name = Path(str((f or {}).get("name") or "")).name
        ext = Path(name).suffix.lower()
        if ext not in OUTPUT_TYPES:
            refused.append(f"{name} (file type not allowed)")
            continue
        try:
            data = base64.b64decode(str((f or {}).get("b64") or ""), validate=True)
        except ValueError:
            refused.append(f"{name} (damaged)")
            continue
        if not data or len(data) > MAX_OUTPUT_BYTES:
            refused.append(f"{name} (empty or too large)")
            continue
        if _pan_in(data, name):
            refused.append(f"{name} (contains a card number)")
            continue
        saved.append(_save(uid, name, data))
    ok = bool(res.get("ok"))
    _fails[uid] = (0, 0.0) if ok else (fails + 1, time.time())
    doc_store.audit(uid, "sandbox.run", "runner", {"code_sha256": hashlib.sha256(code.encode()).hexdigest(), "ok": ok,
                                                   "files_in": len(files_in), "files_out": len(saved), "ms": res.get("ms")})
    lines = [f"Ran on the isolated runner in {res.get('ms', '?')} ms ({'finished' if ok else 'failed'}"
             + (", timed out" if res.get("timed_out") else "") + ")."]
    out_txt = _mask(res.get("stdout") or "").strip()
    err_txt = _mask(res.get("stderr") or "").strip()
    if out_txt:
        lines.append("Output:\n" + out_txt[-3000:])
    if err_txt and not ok:
        lines.append("Error (last lines):\n" + err_txt[-2000:])
    for n in saved:
        lines.append(f"[DOWNLOAD: {n}]")
    if refused:
        lines.append("Not saved: " + "; ".join(refused))
    if not ok:
        lines.append("Fix the error and call run_sandbox_python again (the runs after three failures are stopped).")
    return {"ok": ok, "text": "\n".join(lines), "files": saved}

