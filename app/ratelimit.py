"""Outbound throttle. Protects the LinkedIn account, not this server.

LinkedIn soft-blocks a normal member somewhere around 80-150 profile views a
day. That number is the real budget this service spends, so the limiter sits in
front of the *upstream* call and is bypassed entirely by cache hits.

Two mechanisms:

* Sliding-window counters, hourly and daily. Hard stop with a Retry-After.
* Randomised spacing between calls. A scraper that fires requests exactly
  1.000s apart is trivially fingerprintable; humans are irregular.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque


class RateLimiter:
    def __init__(
        self,
        *,
        per_hour: int,
        per_day: int,
        min_delay: float,
        max_delay: float,
    ) -> None:
        self.per_hour = per_hour
        self.per_day = per_day
        self.min_delay = min_delay
        self.max_delay = max(min_delay, max_delay)
        self._events: deque[float] = deque()
        self._last_release = 0.0
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - 86_400
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def _counts(self, now: float) -> tuple[int, int]:
        hour_cutoff = now - 3_600
        in_hour = sum(1 for t in self._events if t >= hour_cutoff)
        return in_hour, len(self._events)

    def check(self) -> tuple[bool, int, str]:
        """Non-blocking probe. Returns (allowed, retry_after, reason)."""
        now = time.time()
        self._prune(now)
        in_hour, in_day = self._counts(now)
        if in_hour >= self.per_hour:
            oldest = next(t for t in self._events if t >= now - 3_600)
            return False, max(1, int(oldest + 3_600 - now)), "hourly"
        if in_day >= self.per_day:
            return False, max(1, int(self._events[0] + 86_400 - now)), "daily"
        return True, 0, ""

    async def acquire(self) -> None:
        """Reserve one upstream call, sleeping for human-looking spacing.

        Callers must have already passed `check()`; this only handles pacing.
        """
        async with self._lock:
            now = time.time()
            gap = random.uniform(self.min_delay, self.max_delay)
            wait = (self._last_release + gap) - now
            if wait > 0:
                await asyncio.sleep(wait)
            release = time.time()
            self._last_release = release
            self._events.append(release)
            self._prune(release)

    def status(self) -> dict:
        now = time.time()
        self._prune(now)
        in_hour, in_day = self._counts(now)
        return {
            "used_this_hour": in_hour,
            "limit_per_hour": self.per_hour,
            "used_today": in_day,
            "limit_per_day": self.per_day,
            "spacing_seconds": [self.min_delay, self.max_delay],
        }
