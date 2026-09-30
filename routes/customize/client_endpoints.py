"""Admin setup for connectors: the shared OAuth app (client id + secret) for vendors that do not register
clients automatically, and which connectors users may add."""
import json
import re
from fastapi import Depends
from pydantic import BaseModel
from core import credentials, mcp_catalog
from core.audit import audit_log
from core.auth import Principal
from core.config import CONFIG_FILE, write_app_config
from core.small_model import APP_CONFIG

from .base import _PERM, _err, _manage, router

_CLIENT_ID_RX = re.compile(r"^[A-Za-z0-9._~@:/+=-]{3,300}$")


class OAuthClientReq(BaseModel):
    client_id: str = ""
    client_secret: str = ""      # blank on update = keep the stored secret


class EnabledReq(BaseModel):
    enabled: bool


def _write_caps(mutate) -> None:
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    mutate(cfg.setdefault("capabilities", {}))
    write_app_config(cfg, CONFIG_FILE)
    mutate(APP_CONFIG.setdefault("capabilities", {}))


def _oauth_preset(item_id: str):
    pre = mcp_catalog.get_preset(item_id)
    if not pre or not pre.get("auth"):
        return None, _err(f"'{item_id}' does not use an OAuth sign-in", 404)
    return pre, None


@router.put("/connectors/{item_id}/oauth-client")
async def set_oauth_client(item_id: str, req: OAuthClientReq, user: Principal = Depends(_manage)):
    """The OAuth app an admin registered once with the vendor. Users then only press Connect account;
    the secret stays in the OS keychain and is sent only to the vendor's own token endpoint."""
    pre, err = _oauth_preset(item_id)
    if err:
        return err
    cid = req.client_id.strip() or mcp_catalog.shared_client_id(item_id)     # blank = keep the saved id
    if not _CLIENT_ID_RX.match(cid):
        return _err("client id looks wrong (letters, digits and . _ ~ @ : / + = - only)")
    secret = req.client_secret.strip()
    if len(secret) > 600 or any(c in secret for c in "\r\n"):
        return _err("client secret looks wrong")

    def put(caps):
        ov = caps.setdefault("mcp_catalog_overrides", {}).setdefault(item_id, {})
        ov["client_id"] = cid
    _write_caps(put)
    if secret:
        credentials.set_token(mcp_catalog.shared_secret_ref(item_id), secret)
    audit_log(user, action="customize.connectors.oauth_client", resource=item_id, permission_key=_PERM,
              detail={"secret_changed": bool(secret)})
    return {"ok": True, "has_client": True}


@router.delete("/connectors/{item_id}/oauth-client")
async def clear_oauth_client(item_id: str, user: Principal = Depends(_manage)):
    pre, err = _oauth_preset(item_id)
    if err:
        return err

    def drop(caps):
        (caps.get("mcp_catalog_overrides") or {}).pop(item_id, None)
    _write_caps(drop)
    credentials.delete_token(mcp_catalog.shared_secret_ref(item_id))
    audit_log(user, action="customize.connectors.oauth_client_clear", resource=item_id, permission_key=_PERM)
    return {"ok": True, "has_client": False}


@router.put("/connectors/{item_id}/enabled")
async def set_connector_enabled(item_id: str, req: EnabledReq, user: Principal = Depends(_manage)):
    """Allow or block users from adding this connector (capabilities.connectors_enabled)."""
    pre = mcp_catalog.get_preset(item_id)
    if not pre:
        return _err(f"'{item_id}' is not in the connector catalog", 404)

    def put(caps):
        cur = caps.get("connectors_enabled")
        ids = list(cur) if isinstance(cur, list) else mcp_catalog.default_enabled_ids()
        ids = [i for i in ids if i != item_id]
        if req.enabled:
            ids.append(item_id)
        caps["connectors_enabled"] = sorted(ids)
    _write_caps(put)
    audit_log(user, action="customize.connectors.enabled", resource=item_id, permission_key=_PERM,
              detail={"enabled": req.enabled})
    return {"ok": True, "allowed": mcp_catalog.connector_allowed(pre)}
