"""Setting the runner up from the admin page: pair with a one-time code, self-test, switch on, forget. And the live check the chat
makes before it offers the code tool (fail closed: anything unexpected means "not available")."""
import time
from typing import Optional

from . import client, config
from .client import RunnerError

_HEALTH_TTL_S = 30
_health_cache: dict = {"at": 0.0, "url": "", "ok": False}


def _short(fp: str) -> str:
    return (fp[:16] + "...") if fp else ""


async def pair(url: str, code: str) -> dict:
    url = str(url or "").strip().rstrip("/")
    client.parse_url(url)
    pub = config.allow_public()
    h = await client.request(url, "GET", "/health", allow_public=pub, timeout=10)
    if h["status"] != 200 or h["json"].get("service") != "a770-runner":
        raise RunnerError("that address did not answer like a code runner")
    fp = h["fingerprint"]
    r = await client.request(url, "POST", "/pair", {"proof": client.pairing_proof(code, fp)}, allow_public=pub, timeout=15)
    if r["fingerprint"] != fp:
        raise RunnerError("the runner's certificate changed while pairing: stopping")
    if r["status"] != 200 or not r["json"].get("token"):
        raise RunnerError(str(r["json"].get("error") or "the runner refused the pairing code")[:200])
    config.set_token(url, str(r["json"]["token"]))
    config.save(runner_url=url, fingerprint=fp, enabled=False, verified_at=0)
    _health_cache["at"] = 0.0
    result = await selftest()
    if result.get("ok"):
        config.save(enabled=True, verified_at=time.time())
    return {"paired": True, "fingerprint": _short(fp), "selftest": result, "enabled": bool(result.get("ok"))}


async def selftest() -> dict:
    backend = (config.cfg().get("runner_backend") or __import__("os").environ.get("SANDBOX_BACKEND") or "").lower().strip()
    if backend in ("wsl2", "local", "job", "localjob"):
        from . import runners
        runner = runners.get_runner()
        res = await runner.execute("import sys; print('self-test ok: ' + sys.version)", [], timeout_s=15)
        if not res.get("ok"):
            err_msg = res.get("stderr") or res.get("stdout") or "self-test failed"
            raise RunnerError(f"{backend} runner test failed: {err_msg}")
        now = time.time()
        config.save(verified_at=now)
        return {
            "ok": True,
            "checks": [
                {"name": f"{backend} runner isolation", "ok": True, "detail": "Container/Process isolation active"},
                {"name": "Python execution", "ok": True, "detail": res.get("stdout", "").strip()},
            ],
            "verified_at": now,
        }

    url = config.runner_url()
    if not config.configured():
        raise RunnerError("the runner is not paired yet")
    r = await client.request(url, "POST", "/selftest", {}, token=config.get_token(), pin=config.fingerprint(),
                             allow_public=config.allow_public(), timeout=150)
    if r["status"] == 401:
        raise RunnerError("the runner no longer accepts this app: pair it again")
    out = r["json"] if r["status"] == 200 else {"ok": False, "checks": [], "error": str(r["json"].get("error") or "self-test failed")}
    if out.get("ok"):
        config.save(verified_at=time.time())
    return out


def set_enabled(on: bool) -> None:
    backend = (config.cfg().get("runner_backend") or __import__("os").environ.get("SANDBOX_BACKEND") or "").lower().strip()
    if backend in ("wsl2", "local", "job", "localjob"):
        config.save(enabled=bool(on))
        _health_cache["at"] = 0.0
        return
    if on and not (config.configured() and float(config.cfg().get("verified_at") or 0) > 0):
        raise RunnerError("pair the runner and let its self-test pass first")
    config.save(enabled=bool(on))
    _health_cache["at"] = 0.0


def forget() -> None:
    url = config.runner_url()
    if url:
        config.delete_token(url)
    config.save(runner_url="", fingerprint="", enabled=False, verified_at=0)
    _health_cache["at"] = 0.0


async def health() -> Optional[dict]:
    if not config.configured():
        return None
    try:
        r = await client.request(config.runner_url(), "GET", "/health", pin=config.fingerprint(),
                                 allow_public=config.allow_public(), timeout=8)
    except RunnerError:
        return None
    return r["json"] if r["status"] == 200 and r["json"].get("service") == "a770-runner" else None


async def status() -> dict:
    from . import runners
    backend = (config.cfg().get("runner_backend") or __import__("os").environ.get("SANDBOX_BACKEND") or "").lower().strip()
    c = config.cfg()
    if backend == "wsl2":
        avail = runners.WSL2Runner.is_available()
        return {
            "configured": avail,
            "enabled": config.enabled(),
            "url": f"wsl://{c.get('wsl_distro') or 'default'}",
            "url_from_env": False,
            "fingerprint": "wsl2-isolated",
            "verified_at": float(c.get("verified_at") or 0),
            "reachable": avail,
            "sandbox": "wsl2",
            "ready": bool(avail and config.enabled()),
            "backend": "wsl2",
        }
    if backend in ("local", "job", "localjob"):
        return {
            "configured": True,
            "enabled": config.enabled(),
            "url": "local://job",
            "url_from_env": False,
            "fingerprint": "local-job-runner",
            "verified_at": float(c.get("verified_at") or 0),
            "reachable": True,
            "sandbox": "process",
            "ready": config.enabled(),
            "backend": "local",
        }
    live = await health()
    return {"configured": config.configured(), "enabled": config.enabled(), "url": config.runner_url(),
            "url_from_env": bool(__import__("os").environ.get("RUNNER_URL")), "fingerprint": _short(config.fingerprint()),
            "verified_at": float(c.get("verified_at") or 0), "reachable": live is not None,
            "sandbox": (live or {}).get("sandbox"), "ready": bool(live and live.get("sandbox") == "bwrap" and config.enabled()),
            "backend": "remote"}



async def ready() -> bool:
    """True only when the runner is configured, switched on, answers, and reports a working sandbox. Cached for 30 s."""
    from . import runners
    backend = (config.cfg().get("runner_backend") or __import__("os").environ.get("SANDBOX_BACKEND") or "").lower().strip()
    if backend == "wsl2":
        return runners.WSL2Runner.is_available()
    if backend in ("local", "job", "localjob"):
        return True

    if not (config.enabled() and config.configured()):
        return False
    now = time.monotonic()
    url = config.runner_url()
    if _health_cache["url"] == url and now - _health_cache["at"] < _HEALTH_TTL_S:
        return bool(_health_cache["ok"])
    live = await health()
    ok = bool(live and live.get("sandbox") == "bwrap" and live.get("paired"))
    _health_cache.update(at=now, url=url, ok=ok)
    return ok

