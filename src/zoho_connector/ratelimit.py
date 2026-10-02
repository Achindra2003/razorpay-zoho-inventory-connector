"""Client-side budgeting for Zoho Inventory's three limits:

* per minute  - 100 requests/min/org (we default to 90 to leave room for the merchant's other apps)
* concurrency - 5 in-flight calls on the free plan, 10 on paid plans (Zoho error code 1070)
* per day     - 1,000 (free) to 10,000 (premium) requests/day/org

Throttling client-side is cheaper than collecting 429s: every rejected call still burns quota.
When we would have to wait longer than `max_wait_seconds`, we fail fast with RATE_LIMITED and a
`retry_after_s` instead of leaving the agent hanging.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .errors import ConnectorError, ErrorCode

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class MinuteWindow:
    """Sliding 60-second window limiter shared by every tool call for one org."""

    def __init__(self, limit: int, max_wait: float, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep) -> None:
        self.limit = limit
        self.max_wait = max_wait
        self.clock = clock
        self.sleep = sleep
        self._stamps: deque[float] = deque()
        self._cooldown_until = 0.0  # set when Zoho itself says 429
        self._lock = asyncio.Lock()
        self.throttled_waits = 0

    def _prune(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= 60:
            self._stamps.popleft()

    def wait_time(self, now: float) -> float:
        self._prune(now)
        wait = max(0.0, self._cooldown_until - now)
        if len(self._stamps) >= self.limit:
            wait = max(wait, 60 - (now - self._stamps[0]))
        return wait

    async def acquire(self) -> None:
        async with self._lock:
            wait = self.wait_time(self.clock())
            if wait > self.max_wait:
                raise ConnectorError(
                    ErrorCode.RATE_LIMITED,
                    f"Per-minute budget of {self.limit} requests is used up",
                    retry_after_s=wait,
                )
            if wait > 0:
                self.throttled_waits += 1
                await self.sleep(wait)
            now = self.clock()
            self._prune(now)
            self._stamps.append(now)

    def penalize(self, seconds: float) -> None:
        """Zoho returned 429: make every caller back off, not just the one that got it."""
        self._cooldown_until = max(self._cooldown_until, self.clock() + seconds)

    @property
    def remaining(self) -> int:
        self._prune(self.clock())
        return max(0, self.limit - len(self._stamps))


class DailyQuota:
    """Counts requests per org per day and persists the count, so a server restart
    does not forget how much of the merchant's daily quota the agent already spent."""

    def __init__(
        self,
        limit: int,
        path: Path | None,
        *,
        utc_offset_minutes: int | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.limit = limit
        self.path = path
        offset = utc_offset_minutes if utc_offset_minutes is not None else int(os.environ.get("ZOHO_QUOTA_UTC_OFFSET_MINUTES", "330"))
        self.tz = timezone(timedelta(minutes=offset))  # IST by default; Zoho resets per org day
        self.now = now
        self._day = ""
        self.used = 0
        self._load()

    def _today(self) -> str:
        return self.now().astimezone(self.tz).date().isoformat()

    def _load(self) -> None:
        self._day = self._today()
        if self.path and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("day") == self._day:
                    self.used = int(data.get("used", 0))
            except (ValueError, OSError):
                pass

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self.used = today, 0

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"day": self._day, "used": self.used}), encoding="utf-8")
        except OSError:  # quota tracking must never break a read
            pass

    def resets_at(self) -> str:
        local_now = self.now().astimezone(self.tz)
        tomorrow = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return tomorrow.isoformat()

    def check(self) -> None:
        self._roll()
        if self.used >= self.limit:
            seconds = (datetime.fromisoformat(self.resets_at()) - self.now()).total_seconds()
            raise ConnectorError(
                ErrorCode.DAILY_QUOTA_EXHAUSTED,
                f"Daily Zoho API quota of {self.limit} requests is used up",
                retry_after_s=seconds,
                details={"resets_at": self.resets_at()},
            )

    def record(self) -> None:
        self._roll()
        self.used += 1
        self._save()

    def mark_exhausted(self) -> None:
        self._roll()
        self.used = max(self.used, self.limit)
        self._save()

    @property
    def remaining(self) -> int:
        self._roll()
        return max(0, self.limit - self.used)


def backoff_delay(attempt: int, retry_after: float | None, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Honour Retry-After when Zoho sends it, else exponential backoff with full jitter."""
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, cap)
    import random

    return random.uniform(0, min(cap, base * (2**attempt)))
