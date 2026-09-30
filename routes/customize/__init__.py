"""routes/customize.py - Customize page: one browse/install surface over the three
capability catalogs.

  skills      skill_catalog/      -> .agents/skills/     (core/skills.py)
  plugins     plugin_catalog/     -> plugins/            (core/plugins.py)
  connectors  mcp_catalog.PRESETS -> capabilities.mcp_servers (routes/mcp_manager.py)

Everything installable from the in-repo catalogs is reviewed, in-repo content.
The public MCP Registry is proxied browse-only. The Marketplace tab browses an
external plugin registry and installs plugins via URL: every fetch goes through
the SSRF guard (core.net_guard.guarded_get), the manifest + code preview must be
reviewed first, and installs need capabilities.install and are audit-logged.
Browsing needs a session; every install/uninstall needs capabilities.install
and is audit-logged.
"""

from .base import (
    _PERM,
    _manage,
    KINDS,
    MAX_PREVIEW_CHARS,
    LOCAL_REGISTRY_FILE,
    MARKETPLACE_TIMEOUT_S,
    REMOTE_MAX_MANIFEST_BYTES,
    REMOTE_MAX_CODE_BYTES,
    REMOTE_PREVIEW_CHARS,
    router,
    InstallReq,
    RemoteInspectReq,
    RemoteInstallReq,
    _err,
    _caps,
)
from .listing import (
    _skills_items,
    _plugins_items,
    _connectors_items,
    _LISTERS,
    _ENABLED_KEY,
    _item,
)
from .browse_endpoints import (
    registry_search,
    list_kind,
    preview,
)
from .marketplace import (
    _marketplace_url,
    _fetch_url,
    _normalize_registry_entry,
    _validate_manifest_dict,
    _fetch_manifest_and_code,
)
from .marketplace_endpoints import (
    plugin_registry,
    inspect_remote_plugin,
    install_remote_plugin,
)
from .install_endpoints import (
    install,
    _install_connector,
    uninstall,
)

__all__ = [
    "_PERM",
    "_manage",
    "KINDS",
    "MAX_PREVIEW_CHARS",
    "LOCAL_REGISTRY_FILE",
    "MARKETPLACE_TIMEOUT_S",
    "REMOTE_MAX_MANIFEST_BYTES",
    "REMOTE_MAX_CODE_BYTES",
    "REMOTE_PREVIEW_CHARS",
    "router",
    "InstallReq",
    "RemoteInspectReq",
    "RemoteInstallReq",
    "_err",
    "_caps",
    "_skills_items",
    "_plugins_items",
    "_connectors_items",
    "_LISTERS",
    "_ENABLED_KEY",
    "_item",
    "registry_search",
    "list_kind",
    "preview",
    "_marketplace_url",
    "_fetch_url",
    "_normalize_registry_entry",
    "_validate_manifest_dict",
    "_fetch_manifest_and_code",
    "plugin_registry",
    "inspect_remote_plugin",
    "install_remote_plugin",
    "install",
    "_install_connector",
    "uninstall",
]

from . import client_endpoints  # noqa: E402,F401  (registers the admin connector-setup routes)
