"""Which llama-server slot each request uses on the local main model (n_slots > 1).

Two rules, so 15 users can share one server without wiping each other's cached prompt:

1. Sticky slots. Each user owns a slot while they keep using it; their conversation prefix stays cached there between
   turns. A new user takes a free slot nobody owns, else the free slot used longest ago. This replaced `uid % n_slots`,
   where two users could share one slot (and evict each other's cache every turn) while another slot sat empty.
2. A helper slot. With 4+ slots the last one is kept for the app's own calls to the main model: background jobs that
   fall back to main (titles, summaries, answer check...), image reading on main and the keepalive ping. They wait for
   that one slot instead of taking a user's slot or overwriting a user's cached prompt. The user gate then counts
   n_slots - 1. Switch: app.json serving.helper_slot (0 = off, the default, so existing setups keep every slot for
   users; 1 = on). The 15-user B70 profile turns it on with 16 slots (15 users + 1 helper).
"""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional

from . import main_router

MIN_SLOTS_FOR_HELPER = 4
CHAT_INSTANCE_MAX = 5000      # sticky chat -> instance entries kept (oldest dropped)


def _serving_cfg() -> dict:
    from .small_model import APP_CONFIG
    return APP_CONFIG.get("serving") or {}


def helper_slot_index(n_slots: int) -> Optional[int]:
    """The reserved helper slot, or None (too few slots, or switched off)."""
    n = int(n_slots or 1)
    if n < MIN_SLOTS_FOR_HELPER or not int(_serving_cfg().get("helper_slot", 0) or 0):
        return None
    return n - 1


def user_slot_count(n_slots: int) -> int:
    """Slots users may take: all of them, or all but the helper slot."""
    n = max(1, int(n_slots or 1))
    return n - 1 if helper_slot_index(n) is not None else n


def main_n_slots() -> int:
    """Slots of the main model: one server's, or the total over the healthy instances in multi mode."""
    if main_router.is_multi():
        return max(1, sum(i["n_slots"] for i in main_router.live_instances() if i["healthy"]))
    from .state import state
    return max(1, int((getattr(state, "launch_info", None) or {}).get("n_slots")
                      or (state.profile or {}).get("n_slots") or 1))


def total_user_slots() -> int:
    """Slots users may take across the main model (the admission gate's size)."""
    if main_router.is_multi():
        return max(1, sum(user_slot_count(i["n_slots"]) for i in main_router.live_instances() if i["healthy"]))
    return user_slot_count(main_n_slots())


def instance_n_slots(index: int) -> int:
    for i in main_router.live_instances():
        if i["index"] == index:
            return i["n_slots"]
    return main_n_slots()


class SlotTable:
    """User -> slot ownership for the user slots (0 .. n_user-1)."""

    def __init__(self):
        self._n = 0
        self.owner: dict = {}       # slot -> uid
        self.slot_of: dict = {}     # uid -> slot
        self.last_used: dict = {}   # slot -> time
        self.inflight: dict = {}    # slot -> generations running

    def _resize(self, n_user: int) -> None:
        if n_user != self._n:       # new model or slot count: ownership starts over (the caches are gone anyway)
            self._n = n_user
            self.owner.clear()
            self.slot_of.clear()
            self.last_used.clear()
            # in place: routes/common/llm_stream.py keeps a reference to this dict
            for s in [s for s, c in self.inflight.items() if s >= n_user or not c]:
                del self.inflight[s]

    def choose(self, uid, n_user: int) -> int:
        n_user = max(1, int(n_user))
        self._resize(n_user)
        now = time.time()
        mine = self.slot_of.get(uid)
        if mine is not None and not self.inflight.get(mine):
            self.last_used[mine] = now
            return mine
        free = [s for s in range(n_user) if not self.inflight.get(s)]
        if mine is not None:
            # own slot busy (second tab, parallel agent run): borrow a free one without taking it over, so the
            # user's cached conversation stays where it is. Prefer a slot nobody owns.
            cand = [s for s in free if s not in self.owner] or sorted(free, key=lambda s: self.last_used.get(s, 0))
            return cand[0] if cand else mine
        unowned = [s for s in free if s not in self.owner]
        if unowned:
            pick = unowned[0]
        elif free:
            pick = min(free, key=lambda s: self.last_used.get(s, 0))
        else:
            # every slot generating (the gate normally prevents this): share the least busy, longest-idle slot without
            # taking it over, so its owner's cached conversation is not handed to someone else
            return min(range(n_user), key=lambda s: (self.inflight.get(s, 0), self.last_used.get(s, 0)))
        prev = self.owner.get(pick)
        if prev is not None and prev != uid:
            self.slot_of.pop(prev, None)
        self.owner[pick], self.slot_of[uid], self.last_used[pick] = uid, pick, now
        return pick

    def begin(self, slot: int) -> None:
        self.inflight[slot] = self.inflight.get(slot, 0) + 1

    def end(self, slot: int) -> None:
        self.inflight[slot] = max(0, self.inflight.get(slot, 1) - 1)

    def snapshot(self) -> dict:
        """For telemetry: which slots are owned and busy (user ids are not exposed)."""
        return {"user_slots": self._n, "owned": len(self.owner),
                "busy": sum(1 for c in self.inflight.values() if c)}


table = SlotTable()
# multi mode: one table per llama-server instance. table_for(0) is `table` itself, so a replaced `table` still holds.
tables: dict = {0: table}


def table_for(index: int) -> SlotTable:
    if index == 0:
        return table
    return tables.setdefault(index, SlotTable())


# ---------------------------------------------------------------------------------------------------------------
# Pool guard for the shared KV pool (-kvu with several slots)
# ---------------------------------------------------------------------------------------------------------------
# Measured on llama-server b12810: idle slots' cached conversations are evicted when a request needs room, but when the
# requests running at the same time need more than the pool holds, EVERY one of them fails ("Context size has been
# exceeded", HTTP 500). So the running requests' needs are counted here and a request that would overflow the pool
# waits (and is told why) until enough of the others finish. Rare in practice (2-3 of 15 users at 128K), but it turns
# the worst case from "everyone's answer fails" into "the last long one waits".

GEN_RESERVE_TOKENS = 4096     # room kept for the answer when the request sets no max_tokens
POOL_MARGIN = 0.03            # for helper calls, the keepalive and estimation error


def pool_capacity(profile: Optional[dict]) -> int:
    """Tokens the shared pool can hold for running requests, or 0 when there is no shared pool to guard."""
    p = profile or {}
    try:
        n_slots, ctx = int(p.get("n_slots") or 1), int(p.get("context_size") or 0)
    except (TypeError, ValueError):
        return 0
    if not p.get("kv_unified") or n_slots <= 1 or ctx <= 0:
        return 0
    return int(ctx * (1 - POOL_MARGIN))


def request_need(prompt_tokens: int, max_tokens: Optional[int], profile: Optional[dict]) -> int:
    """Tokens one request may occupy: prompt + answer, at most one slot's cap."""
    gen = int(max_tokens) if max_tokens and int(max_tokens) > 0 else GEN_RESERVE_TOKENS
    need = max(1, int(prompt_tokens)) + gen
    p = profile or {}
    cap = int(p.get("kv_unified_per_slot") or 0) or int(p.get("context_size") or 0)
    return min(need, cap) if cap > 0 else need


class TokenCalibration:
    """The app's prompt estimate (core/agent_loop: ~3 characters per token) against what llama-server really read.
    Measured with Spark-X2.6 on English text: the estimate was ~1.7x too high, so the pool guard made long chats wait
    while the pool still had room. Each answered request teaches the real ratio (prompt_n + cache_n from its timings);
    the guard scales its estimate by it, plus a safety margin. Until the first answer the plain estimate is used."""

    SAFETY = 1.10
    LOW, HIGH = 0.25, 1.5

    def __init__(self):
        self.ratio = 1.0
        self.samples = 0

    def observe(self, estimated: int, real: int) -> None:
        if estimated < 200 or real <= 0:          # tiny prompts say little about the tokenizer
            return
        r = max(self.LOW, min(self.HIGH, real / estimated))
        self.ratio = r if self.samples == 0 else 0.8 * self.ratio + 0.2 * r
        self.samples += 1

    def scale(self, estimated: int) -> int:
        if not self.samples:
            return int(estimated)
        return int(estimated * min(self.HIGH, self.ratio * self.SAFETY))


calibration = TokenCalibration()


class PoolLedger:
    def __init__(self):
        self.used = 0
        self.running = 0
        self.waiting = 0
        self._cond: Optional[asyncio.Condition] = None
        self._loop = None

    def _condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._cond is None or self._loop is not loop:
            self._cond, self._loop = asyncio.Condition(), loop
        return self._cond

    def fits(self, need: int, capacity: int) -> bool:
        # one request alone always runs (if it is too big for the pool, llama-server's own error says so)
        return self.running == 0 or self.used + need <= capacity

    async def acquire(self, need: int, capacity: int) -> None:
        cond = self._condition()
        async with cond:
            if not self.fits(need, capacity):
                self.waiting += 1
                try:
                    await cond.wait_for(lambda: self.fits(need, capacity))
                finally:
                    self.waiting -= 1
            self.used += need
            self.running += 1

    async def _notify(self) -> None:
        if self._cond is not None:
            async with self._cond:
                self._cond.notify_all()

    def release(self, need: int) -> None:
        """Synchronous, so it is safe in a generator's `finally` (an abandoned stream cannot await there); the waiters
        are woken by a task on the running loop."""
        self.used = max(0, self.used - need)
        self.running = max(0, self.running - 1)
        try:
            asyncio.get_running_loop().create_task(self._notify())
        except RuntimeError:
            pass                    # no loop (shutdown): nobody is waiting


pool = PoolLedger()
pools: dict = {0: pool}     # multi mode: one pool per instance (each has its own KV cache)


def pool_for(index: int) -> PoolLedger:
    if index == 0:
        return pool
    return pools.setdefault(index, PoolLedger())


def instance_capacity(profile: Optional[dict], n_slots: int) -> int:
    """pool_capacity for one instance: its own slot count (the context size is per instance)."""
    p = dict(profile or {})
    p["n_slots"] = n_slots
    return pool_capacity(p)


# multi mode: the instance each chat (uid, session) runs on. Its prompt cache lives there, so it stays put.
chat_instance: dict = {}


def _remember_chat(key, index: int) -> None:
    chat_instance.pop(key, None)
    chat_instance[key] = index
    while len(chat_instance) > CHAT_INSTANCE_MAX:
        chat_instance.pop(next(iter(chat_instance)))


def _busy_slots(index: int) -> int:
    return sum(1 for c in table_for(index).inflight.values() if c)


def place(uid, session, need: int, profile: Optional[dict]) -> int:
    """Multi mode: the instance for a new request. The chat's own instance while it has a free user slot and room in
    its pool; else the healthy instance with the most free user slots, then the most free pool tokens, then the fewest
    requests in flight, then the lowest index."""
    key = (uid, session)
    insts = [i for i in main_router.live_instances() if i["healthy"]]
    if not insts:
        return 0
    by_index = {i["index"]: i for i in insts}

    def free_slots(idx: int) -> int:
        return max(0, user_slot_count(by_index[idx]["n_slots"]) - _busy_slots(idx))

    def fits(idx: int) -> bool:
        cap = instance_capacity(profile, by_index[idx]["n_slots"])
        return cap == 0 or pool_for(idx).fits(need, cap)

    sticky = chat_instance.get(key)
    if sticky in by_index and free_slots(sticky) > 0 and fits(sticky):
        chosen = sticky
    else:
        ranked = sorted(by_index, key=lambda idx: (
            -free_slots(idx),
            -(instance_capacity(profile, by_index[idx]["n_slots"]) - pool_for(idx).used),
            main_router.inflight.get(idx, 0), idx))
        if free_slots(ranked[0]) > 0:
            chosen = ranked[0]
        elif sticky in by_index:            # nothing free (the gate normally prevents this): stay with the cache
            chosen = sticky
        else:
            chosen = min(by_index, key=lambda idx: (main_router.inflight.get(idx, 0), idx))
    _remember_chat(key, chosen)
    return chosen


# ---------------------------------------------------------------------------------------------------------------
# llama-server /slots, shared: the status bar (every viewer, every 2.5 s) and telemetry each called it per viewer
# ---------------------------------------------------------------------------------------------------------------
SLOTS_TTL_S = 1.0
_slots_cache: tuple = (0.0, None)           # the primary (instance 0); kept as it was (tests patch it)
_slots_task: Optional["asyncio.Future"] = None
_extra_slots_cache: dict = {}               # the other instances: index -> (time, data)
_extra_slots_tasks: dict = {}


def _client_for(index: int):
    """The httpx client of a live instance, or None when it is down."""
    from .state import state
    if index == 0:
        ok = state.process and state.process.poll() is None and state.client is not None
        return state.client if ok else None
    for e in getattr(state, "extras", None) or []:
        if int(e.index) == index:
            return e.client if e.is_running() and e.client is not None else None
    return None


async def main_slots(timeout: float = 1.5, index: Optional[int] = None) -> Optional[list]:
    """An instance's /slots list (at most SLOTS_TTL_S old), or None when it is down or not answering.
    index None = the primary, as before instances existed."""
    global _slots_cache, _slots_task
    idx = 0 if index is None else int(index)
    client = _client_for(idx)
    if client is None:
        return None
    at, data = _slots_cache if idx == 0 else _extra_slots_cache.get(idx, (0.0, None))
    if data is not None and time.time() - at < SLOTS_TTL_S:
        return data

    async def fetch():
        resp = await client.get("/slots", timeout=timeout)
        return resp.json() if resp.status_code == 200 else None

    loop = asyncio.get_running_loop()
    if idx == 0:
        if _slots_task is None or _slots_task.done() or _slots_task.get_loop() is not loop:
            _slots_task = asyncio.ensure_future(fetch())
        task = _slots_task
    else:
        task = _extra_slots_tasks.get(idx)
        if task is None or task.done() or task.get_loop() is not loop:
            task = _extra_slots_tasks[idx] = asyncio.ensure_future(fetch())
    try:
        data = await asyncio.shield(task)
    except Exception:
        return None
    if isinstance(data, list):
        if idx == 0:
            _slots_cache = (time.time(), data)
        else:
            _extra_slots_cache[idx] = (time.time(), data)
        return data
    return None


async def all_main_slots(timeout: float = 1.2) -> dict:
    """Every instance's /slots list, for telemetry: {index: list or None}."""
    idxs = [i["index"] for i in main_router.live_instances()]
    res = await asyncio.gather(*(main_slots(timeout, index=i) for i in idxs), return_exceptions=True)
    return {i: (None if isinstance(r, BaseException) else r) for i, r in zip(idxs, res)}


def user_slot(uid, n_slots: int) -> int:
    """The slot a user's conversation is cached in (sticky table), for showing their context fill."""
    s = table.slot_of.get(uid)
    return int(s) if s is not None else int(uid or 0) % max(1, user_slot_count(n_slots))


def user_instance_slot(uid, session=None) -> Optional[tuple]:
    """(instance index, slot) of a chat's conversation in multi mode, or None when it has no slot yet."""
    inst = chat_instance.get((uid, session))
    if inst is None:
        return None
    s = table_for(inst).slot_of.get((uid, session))
    return (inst, int(s)) if s is not None else None


def latest_user_instance(uid) -> Optional[tuple]:
    """(instance index, slot) of the user's most recently used chat that holds a slot, or None. Used when the caller
    has no default-session entry (the status bar asks without a chat)."""
    for key in reversed(list(chat_instance.keys())):
        if not isinstance(key, tuple) or not key or key[0] != uid:
            continue
        inst = chat_instance.get(key)
        if inst is None:
            continue
        s = table_for(inst).slot_of.get(key)
        if s is not None:
            return (inst, int(s))
    return None


def queue_stats() -> dict:
    """Queue depth, waits and time to first token, from the admission gate (routes/common/llm_stream.py registers it:
    core must not import routes)."""
    return _queue_stats_fn() if _queue_stats_fn else {}


_queue_stats_fn = None


def register_queue_stats(fn) -> None:
    global _queue_stats_fn
    _queue_stats_fn = fn

_helper_sem: Optional[asyncio.Semaphore] = None


_helper_sems: dict = {}     # multi mode: one helper gate per instance


@asynccontextmanager
async def helper_call():
    """Wrap an app-internal request to the local main model. Yields extra payload fields: the helper slot (waiting
    for it when another helper call holds it), or {} when no helper slot is reserved (behaviour as before).
    Multi mode: the least busy instance and its helper slot; the routing transport sends the call there."""
    global _helper_sem
    if main_router.is_multi():
        idx = main_router.least_busy_index()
        h = helper_slot_index(instance_n_slots(idx)) if idx is not None else None
        if h is None:
            yield {}
            return
        sem = _helper_sems.setdefault(idx, asyncio.Semaphore(1))
        tok = main_router._instance_choice.set(idx)
        try:
            async with sem:
                yield {"id_slot": h, "cache_prompt": True}
        finally:
            main_router._instance_choice.reset(tok)
        return
    h = helper_slot_index(main_n_slots())
    if h is None:
        yield {}
        return
    if _helper_sem is None:
        _helper_sem = asyncio.Semaphore(1)
    async with _helper_sem:
        yield {"id_slot": h, "cache_prompt": True}
