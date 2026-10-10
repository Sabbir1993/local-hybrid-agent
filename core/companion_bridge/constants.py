import asyncio
import time
from typing import Optional
from fastapi import WebSocket

DEVICE_KEY_PREFIX = "a770_dev_"

# user_id -> WebSocket
_connections: dict[int, WebSocket] = {}
# user_id -> {req_id: asyncio.Future}
_pending: dict[int, dict[str, asyncio.Future]] = {}
# user_id -> {"hostname": str, "connected_at": float}
_meta: dict[int, dict] = {}

# user_id -> time the last companion socket closed (for the reconnect grace window)
_last_seen: dict[int, float] = {}

DEFAULT_TIMEOUT_S = 60
RECONNECT_GRACE_S = 10    # a companion that dropped this recently is expected back
CONNECT_WAIT_S = 5        # how long call() waits for a (re)connection before failing
# ops that are safe to repeat after a dropped connection; writes and shell are not
READ_ONLY_OPS = {"fs.read", "fs.read_b64", "fs.list", "fs.grep", "fs.tree", "fs.browse", "fs.read_many", "fs.diagnose"}
# ops the companion would otherwise confirm in a native dialog of its own (companion/policy.js)
CONFIRM_OPS = {"browser.navigate", "browser.eval", "android.boot_avd", "android.pair", "android.connect",
               "android.install", "ios.boot", "ios.install"}


def is_connected(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in _connections


def is_available(user_id: Optional[int], grace: float = RECONNECT_GRACE_S) -> bool:
    """Connected, or disconnected so recently that a reconnect is expected --
    lets a tool ride out the companion's brief reconnect instead of failing."""
    import sys
    conn_fn = getattr(sys.modules.get("core.companion_bridge"), "is_connected", is_connected)
    if conn_fn(user_id):
        return True
    t = _last_seen.get(user_id) if user_id is not None else None
    return t is not None and time.time() - t < grace


def connection_info(user_id: Optional[int]) -> Optional[dict]:
    import sys
    conn_fn = getattr(sys.modules.get("core.companion_bridge"), "is_connected", is_connected)
    if not conn_fn(user_id):
        return None
    return dict(_meta.get(user_id) or {})
