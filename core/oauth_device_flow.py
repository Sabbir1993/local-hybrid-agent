"""Generic OAuth 2.0 Device Authorization Grant (RFC 8628) helper.

Driven entirely by a catalog entry's "auth" block (see core/mcp_catalog.py) - no
provider-specific code here. Device flow only needs a public client_id (no client
secret), so nothing secret has to live in this repo; the resulting access token is
handed to the caller to store (core/credentials.py), never persisted here.
"""

import time
from typing import Optional

import httpx

DEVICE_FLOW_TIMEOUT_S = 15


async def start(auth_cfg: dict) -> dict:
    """POST to the provider's device_code endpoint. Returns the RFC 8628 device response."""
    async with httpx.AsyncClient(timeout=DEVICE_FLOW_TIMEOUT_S) as client:
        r = await client.post(
            auth_cfg["device_code_url"],
            data={"client_id": auth_cfg["client_id"], "scope": auth_cfg.get("scope", "")},
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    return {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri") or data.get("verification_uri_complete"),
        "expires_in": data.get("expires_in", 900),
        "interval": data.get("interval", 5),
    }


async def poll(auth_cfg: dict, device_code: str) -> dict:
    """One poll of the token endpoint. Returns {"status": "pending"|"success"|"expired"|"denied"|"error", "token": ...}."""
    async with httpx.AsyncClient(timeout=DEVICE_FLOW_TIMEOUT_S) as client:
        r = await client.post(
            auth_cfg["token_url"],
            data={
                "client_id": auth_cfg["client_id"],
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
        )
        try:
            data = r.json()
        except Exception:
            return {"status": "error", "error": f"non-JSON response ({r.status_code})"}

    if "access_token" in data:
        return {"status": "success", "token": data["access_token"]}

    err = data.get("error")
    if err == "authorization_pending":
        return {"status": "pending"}
    if err == "slow_down":
        return {"status": "pending", "slow_down": True}
    if err == "expired_token":
        return {"status": "expired"}
    if err == "access_denied":
        return {"status": "denied"}
    return {"status": "error", "error": err or "unknown device-flow error"}


# ---------------- in-process session store ----------------
# Keyed by a short-lived session id handed to the UI; never exposes device_code to the client.

_sessions: dict = {}


def new_session(server_id: str, device_code: str, interval: int, expires_in: int) -> str:
    import secrets
    sid = secrets.token_urlsafe(16)
    _sessions[sid] = {
        "server_id": server_id,
        "device_code": device_code,
        "interval": interval,
        "expires_at": time.time() + expires_in,
    }
    return sid


def get_session(sid: str) -> Optional[dict]:
    s = _sessions.get(sid)
    if not s:
        return None
    if time.time() > s["expires_at"]:
        _sessions.pop(sid, None)
        return None
    return s


def drop_session(sid: str) -> None:
    _sessions.pop(sid, None)
