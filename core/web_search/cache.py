import time
from collections import OrderedDict
from .constants import CACHE_TTL_S, CACHE_MAX

_cache: "OrderedDict[tuple, tuple]" = OrderedDict()


def cache_get(key: tuple):
    hit = _cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return val


def cache_put(key: tuple, val) -> None:
    _cache[key] = (time.time(), val)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
