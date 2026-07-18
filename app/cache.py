"""
Tiny in-memory TTL cache.

Good enough for a single process. Like the rate limiter, if you scale to
multiple workers each keeps its own cache (harmless, just fewer hits) — move to
Redis when you want a shared cache. Kept dependency-free on purpose.
"""
import time
from collections import OrderedDict

from . import config


class TTLCache:
    def __init__(self, max_entries: int, ttl_seconds: int) -> None:
        self.max_entries = max_entries
        self.ttl = ttl_seconds
        # OrderedDict gives us cheap LRU eviction: move_to_end on access,
        # popitem(last=False) to drop the oldest.
        self._store: "OrderedDict[str, tuple[float, object]]" = OrderedDict()

    def get(self, key: str):
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.monotonic() > expires_at:
            del self._store[key]  # expired
            return None
        self._store.move_to_end(key)  # mark as recently used
        return value

    def set(self, key: str, value: object) -> None:
        self._store[key] = (time.monotonic() + self.ttl, value)
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)  # evict least-recently-used


cache = TTLCache(config.CACHE_MAX_ENTRIES, config.DATA_TTL)
