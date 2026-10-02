from datetime import datetime, timedelta, timezone

import pytest

from zoho_connector.errors import ConnectorError, ErrorCode
from zoho_connector.ratelimit import DailyQuota, MinuteWindow, backoff_delay


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_window_allows_up_to_limit_then_waits():
    clock = FakeClock()
    window = MinuteWindow(limit=3, max_wait=120, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await window.acquire()
    assert clock.slept == []
    clock.now += 10
    await window.acquire()  # 4th call must wait until the first stamp is 60s old
    assert clock.slept == [pytest.approx(50)]
    assert window.throttled_waits == 1


async def test_window_fails_fast_when_wait_exceeds_budget():
    clock = FakeClock()
    window = MinuteWindow(limit=2, max_wait=5, clock=clock, sleep=clock.sleep)
    await window.acquire()
    await window.acquire()
    with pytest.raises(ConnectorError) as exc:
        await window.acquire()
    assert exc.value.code is ErrorCode.RATE_LIMITED
    assert exc.value.retry_after_s == pytest.approx(60)


async def test_upstream_429_penalizes_all_callers():
    clock = FakeClock()
    window = MinuteWindow(limit=100, max_wait=30, clock=clock, sleep=clock.sleep)
    window.penalize(7)
    await window.acquire()
    assert clock.slept == [pytest.approx(7)]


def test_daily_quota_persists_and_rolls_over(tmp_path):
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "quota.json"
    q = DailyQuota(3, path, utc_offset_minutes=330, now=lambda: now)
    q.record()
    q.record()
    # A restarted server sees the same usage.
    q2 = DailyQuota(3, path, utc_offset_minutes=330, now=lambda: now)
    assert q2.used == 2 and q2.remaining == 1
    q2.record()
    with pytest.raises(ConnectorError) as exc:
        q2.check()
    assert exc.value.code is ErrorCode.DAILY_QUOTA_EXHAUSTED
    assert exc.value.details["resets_at"].startswith("2026-10-03T00:00:00+05:30")
    # Next IST day: budget is back.
    later = now + timedelta(hours=13)
    q3 = DailyQuota(3, path, utc_offset_minutes=330, now=lambda: later)
    assert q3.remaining == 3


def test_backoff_honours_retry_after_and_caps():
    assert backoff_delay(0, 4.0) == 4.0
    assert backoff_delay(0, 999.0) == 30.0
    for attempt in range(5):
        assert 0 <= backoff_delay(attempt, None) <= min(30, 2**attempt)
