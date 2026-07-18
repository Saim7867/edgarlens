"""
Two limiters, two different jobs:

1. RateLimiter (per API key) — protects YOU from your users. Token bucket per
   key; empty bucket -> 429 with Retry-After. Same design as before.

2. OutboundLimiter (global) — protects you from the SEC. Their fair-access
   policy caps clients at 10 req/sec; exceed it and they ban your IP, which
   kills your product for every user at once. This limiter makes ALL outbound
   SEC calls (across all your users) share one 8/sec budget. Instead of
   rejecting, it WAITS — an internal delay beats surfacing errors to users.

GIL/concurrency note: both are in-memory, mutated on asyncio's single thread —
safe within one process. Multiple uvicorn workers each get their own copies:
per-key limits become N x looser (fix: Redis), and the outbound budget becomes
N x 8/sec (fix: lower SEC_MAX_RPS to 8/N per worker, or centralize in Redis).
"""
import asyncio
import time
from dataclasses import dataclass, field

from . import config


@dataclass
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)

    def try_consume(self) -> tuple[bool, float]:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        return False, (1.0 - self.tokens) / self.refill_per_sec


class RateLimiter:
    """Per-API-key limiter. Rejects (the client can retry)."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    async def check(self, key_id: str, tier: str) -> tuple[bool, float]:
        cfg = config.TIERS.get(tier, config.TIERS["free"])
        b = self._buckets.get(key_id)
        if b is None:
            b = _Bucket(cfg["capacity"], cfg["refill_per_sec"], tokens=cfg["capacity"])
            self._buckets[key_id] = b
        return b.try_consume()


class OutboundLimiter:
    """Global pace-setter for SEC calls. Waits instead of rejecting."""

    def __init__(self, max_rps: float) -> None:
        self._bucket = _Bucket(capacity=max_rps, refill_per_sec=max_rps, tokens=max_rps)
        self._lock = asyncio.Lock()  # serialize acquire so waiters queue fairly

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                ok, wait = self._bucket.try_consume()
                if ok:
                    return
                await asyncio.sleep(wait)  # GIL released; other requests proceed


limiter = RateLimiter()
outbound = OutboundLimiter(config.SEC_MAX_RPS)
