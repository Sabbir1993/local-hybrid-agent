from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from core.config import BASE_DIR
from core.small_model import APP_CONFIG
from core.deps import require_permission


_PERM = "capabilities.install"


_manage = require_permission(_PERM)


KINDS = ("skills", "plugins", "connectors")


MAX_PREVIEW_CHARS = 20000


# Remote plugin registry + URL installs.
# An operator can point this at any JSON registry of the form
# {"plugins": [{"name": ..., "manifest_url": ..., "code_url": ..., ...}]}.
# Every URL fetch below goes through core.net_guard.guarded_get (SSRF guard,
# private-network block, capped body). Nothing is written to plugins/ until the
# user reviews the manifest + code preview and confirms install.
LOCAL_REGISTRY_FILE = BASE_DIR / "plugin_registry.json"


MARKETPLACE_TIMEOUT_S = 15


REMOTE_MAX_MANIFEST_BYTES = 64 * 1024


REMOTE_MAX_CODE_BYTES = 512 * 1024


REMOTE_PREVIEW_CHARS = 12000


router = APIRouter(prefix="/customize", tags=["customize"])


class InstallReq(BaseModel):
    secrets: dict = {}        # connectors only: {ENV_KEY: value} -> OS keychain


class RemoteInspectReq(BaseModel):
    manifest_url: str = ""
    code_url: str = ""          # optional override; else taken from manifest's code_url/source_url


class RemoteInstallReq(BaseModel):
    manifest_url: str = ""
    code_url: str = ""          # optional override
    name: str = ""              # optional override (must match manifest name when both given)
    code_sha256: str = ""       # from inspect-remote: install only the exact code that was reviewed


def _err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


def _caps() -> dict:
    return APP_CONFIG.get("capabilities", {}) or {}
