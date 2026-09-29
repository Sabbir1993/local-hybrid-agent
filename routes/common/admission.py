"""
routes/common/admission.py - Fair-share admission gate for the local llama-server.
"""

import asyncio
from typing import Optional

from core.small_model import APP_CONFIG
from core.state import state


class _Admission:
    """Fair-share gate in front of the local main llama-server.

    - global: at most n_slots generations in flight (one per llama-server slot),
      so extra requests wait here -- where the UI can be told -- instead of
      silently inside llama-server;
    - per user: at most `max_inflight_per_user` (default 1), so one person's
      agent loop / second tab can't occupy every slot while others wait.
    Configured under app.json "serving"."""

    def __init__(self):
        self._global: Optional[asyncio.Semaphore] = None
        self._global_n = 0
        self._users: dict = {}
        self._users_n = 0
        self.waiting = 0

    def _cfg(self) -> dict:
        return APP_CONFIG.get("serving") or {}

    def enabled(self) -> bool:
        return bool(self._cfg().get("queue_enabled", True))

    def _sems(self, uid):
        n = max(1, int((state.profile or {}).get("n_slots") or 1))
        if self._global is None or n != self._global_n:
            # resized on model (re)load; holders of the old one release it harmlessly
            self._global, self._global_n = asyncio.Semaphore(n), n
        per_user = max(1, int(self._cfg().get("max_inflight_per_user", 1)))
        if per_user != self._users_n:
            self._users, self._users_n = {}, per_user
        us = self._users.get(uid)
        if us is None:
            us = self._users[uid] = asyncio.Semaphore(per_user)
        return self._global, us


admission = _Admission()
