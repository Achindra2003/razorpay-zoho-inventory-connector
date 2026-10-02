"""HTTP behaviour against the mock Zoho: auth refresh, retries, throttling, quota, cache."""

import asyncio

import pytest

from zoho_connector.errors import ConnectorError, ErrorCode


async def test_every_request_carries_org_and_token(runtime, mock):
    data = await runtime.client.get("/items", {"per_page": 1}, cache_ttl=0)
    assert data["code"] == 0
    assert runtime.client.stats["requests"] == 1


async def test_cache_avoids_repeat_calls(runtime):
    await runtime.client.get("/items", {"per_page": 5})
    await runtime.client.get("/items", {"per_page": 5})
    assert runtime.client.stats["requests"] == 1
    assert runtime.client.cache.hits == 1


async def test_expired_access_token_is_refreshed_transparently(runtime, mock):
    mock.access_tokens = {t: 0.0 for t in mock.access_tokens}  # Zoho revoked/expired it early
    data = await runtime.client.get("/items", {"per_page": 1}, cache_ttl=0)
    assert data["code"] == 0
    assert runtime.client.stats["token_refreshes_on_401"] == 1
    assert runtime.tokens.refresh_count == 1


async def test_concurrent_calls_share_one_refresh(runtime, mock):
    conn = runtime.tokens.connection
    conn.access_expires_at = 0  # force proactive refresh for every caller
    await asyncio.gather(*(runtime.client.get("/items", {"page": i}, cache_ttl=0) for i in range(1, 9)))
    assert runtime.tokens.refresh_count == 1


async def test_revoked_refresh_token_surfaces_auth_expired(runtime, mock):
    mock.refresh_tokens.clear()
    mock.access_tokens.clear()
    with pytest.raises(ConnectorError) as exc:
        await runtime.client.get("/items", cache_ttl=0)
    assert exc.value.code is ErrorCode.AUTH_EXPIRED


async def test_429_with_retry_after_is_retried(runtime, mock):
    mock.fail_next = [429, 429]
    data = await runtime.client.get("/items", {"per_page": 1}, cache_ttl=0)
    assert data["code"] == 0
    assert runtime.client.stats["upstream_429"] == 2
    assert runtime.client.stats["retries"] == 2


async def test_concurrency_error_1070_is_retried(runtime, mock):
    mock.fail_next = [1070]
    data = await runtime.client.get("/items", {"per_page": 1}, cache_ttl=0)
    assert data["code"] == 0


async def test_5xx_is_retried_then_succeeds(runtime, mock):
    mock.fail_next = [503]
    assert (await runtime.client.get("/items", cache_ttl=0))["code"] == 0


async def test_persistent_throttling_fails_fast_with_retry_after(runtime, mock):
    mock.fail_next = [429] * 10
    with pytest.raises(ConnectorError) as exc:
        await runtime.client.get("/items", cache_ttl=0)
    assert exc.value.code is ErrorCode.RATE_LIMITED
    assert exc.value.retry_after_s is not None
    assert runtime.client.stats["requests"] == runtime.settings.max_retries + 1


async def test_upstream_daily_limit_maps_to_quota_exhausted(runtime, mock):
    mock.daily_used = mock.daily_limit
    with pytest.raises(ConnectorError) as exc:
        await runtime.client.get("/items", cache_ttl=0)
    assert exc.value.code is ErrorCode.DAILY_QUOTA_EXHAUSTED
    # The connector now refuses locally instead of spending more calls.
    before = runtime.client.stats["requests"]
    with pytest.raises(ConnectorError):
        await runtime.client.get("/items", {"page": 2}, cache_ttl=0)
    assert runtime.client.stats["requests"] == before


async def test_local_daily_budget_blocks_before_calling_zoho(runtime):
    runtime.client.quota.used = runtime.client.quota.limit
    with pytest.raises(ConnectorError) as exc:
        await runtime.client.get("/items", cache_ttl=0)
    assert exc.value.code is ErrorCode.DAILY_QUOTA_EXHAUSTED
    assert runtime.client.stats["requests"] == 0


async def test_not_found_maps_cleanly(runtime):
    with pytest.raises(ConnectorError) as exc:
        await runtime.client.get("/items/does-not-exist", cache_ttl=0)
    assert exc.value.code is ErrorCode.NOT_FOUND


async def test_burst_is_throttled_client_side_not_by_zoho(tmp_path):
    """150 calls against a mock that allows 100/min: the connector's own 90/min budget
    must stop us before Zoho ever returns 429."""
    from conftest import make_settings
    from zoho_connector.runtime import demo_runtime

    settings = make_settings(tmp_path, per_minute_limit=90, max_wait_seconds=0.5)
    rt = await demo_runtime(settings)
    try:
        ok = limited = 0
        for i in range(150):
            try:
                await rt.client.get("/items", {"page": 1, "per_page": 1, "n": i}, cache_ttl=0)
                ok += 1
            except ConnectorError as exc:
                assert exc.code is ErrorCode.RATE_LIMITED
                limited += 1
        assert ok == 90
        assert limited == 150 - ok
        assert rt.client.stats["upstream_429"] == 0
    finally:
        await rt.aclose()
