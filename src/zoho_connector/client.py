"""Async HTTP client for the Zoho Inventory v1 API.

Responsibilities: auth header + organization_id on every call, transparent token refresh,
client-side rate/concurrency/daily budgeting, bounded retries, a short read cache, and
mapping every failure to a ConnectorError with a stable code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .auth import TokenManager
from .config import Settings
from .errors import ConnectorError, ErrorCode
from .ratelimit import DailyQuota, MinuteWindow, backoff_delay

log = logging.getLogger(__name__)

CONCURRENCY_LIMIT_CODE = 1070  # Zoho: too many concurrent requests
API_PATH = "/inventory/v1"


class TTLCache:
    def __init__(self, ttl: float, max_entries: int = 512, clock=time.monotonic) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock
        self._data: dict[tuple, tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> Any | None:
        entry = self._data.get(key)
        if entry and entry[0] > self.clock():
            self.hits += 1
            return entry[1]
        self._data.pop(key, None)
        self.misses += 1
        return None

    def put(self, key: tuple, value: Any, ttl: float | None = None) -> None:
        if len(self._data) >= self.max_entries:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (self.clock() + (self.ttl if ttl is None else ttl), value)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _body(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


class ZohoClient:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenManager,
        http: httpx.AsyncClient,
        *,
        limiter: MinuteWindow | None = None,
        quota: DailyQuota | None = None,
        cache: TTLCache | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.tokens = tokens
        self.http = http
        self.limiter = limiter or MinuteWindow(settings.per_minute_limit, settings.max_wait_seconds)
        self.quota = quota or DailyQuota(settings.daily_quota, None)
        self.cache = cache or TTLCache(settings.cache_ttl_seconds)
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)
        self.sleep = sleep
        self.stats = {"requests": 0, "retries": 0, "upstream_429": 0, "token_refreshes_on_401": 0}

    @property
    def organization_id(self) -> str:
        return self.tokens.connection.organization_id

    async def get(self, path: str, params: dict[str, Any] | None = None, *, cache_ttl: float | None = None, org_scoped: bool = True) -> dict[str, Any]:
        query = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        if org_scoped:
            query["organization_id"] = self.organization_id
        key = (path, tuple(sorted((k, str(v)) for k, v in query.items())))
        if cache_ttl != 0:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        data = await self._request(path, query)
        if cache_ttl != 0:
            self.cache.put(key, data, cache_ttl)
        return data

    async def _request(self, path: str, query: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.tokens.connection.api_domain}{API_PATH}{path}"
        refreshed = False
        started = time.monotonic()
        attempt = 0
        while True:
            retry_after: float | None = None
            failure: ConnectorError | None = None
            self.quota.check()
            await self.limiter.acquire()
            token = await self.tokens.access_token()
            async with self.semaphore:
                self.quota.record()
                self.stats["requests"] += 1
                try:
                    resp = await self.http.get(
                        url,
                        params=query,
                        headers={"Authorization": f"Zoho-oauthtoken {token}", "Accept": "application/json"},
                        timeout=self.settings.request_timeout,
                    )
                except httpx.TimeoutException:
                    resp = None
                    failure = ConnectorError(ErrorCode.TIMEOUT, f"Zoho did not respond within {self.settings.request_timeout}s")
                except httpx.HTTPError as exc:
                    resp = None
                    failure = ConnectorError(ErrorCode.UPSTREAM_ERROR, f"Network error talking to Zoho: {type(exc).__name__}")

            if resp is not None:
                body = _body(resp)
                zoho_code = body.get("code")
                message = str(body.get("message", ""))

                if resp.status_code == 200 and zoho_code in (0, None):
                    log.debug("GET %s -> 200 in %.0fms", path, (time.monotonic() - started) * 1000)
                    return body

                if resp.status_code == 401 and not refreshed:
                    # Access token expired or revoked early: refresh once, then retry.
                    refreshed = True
                    self.stats["token_refreshes_on_401"] += 1
                    await self.tokens.refresh(stale_token=token)
                    continue
                if resp.status_code == 401:
                    raise ConnectorError(ErrorCode.AUTH_EXPIRED, "Zoho rejected the refreshed access token")
                if resp.status_code == 403:
                    raise ConnectorError(ErrorCode.FORBIDDEN, message or "Not permitted")
                if resp.status_code == 404 or "does not exist" in message.lower():
                    raise ConnectorError(ErrorCode.NOT_FOUND, message or "Not found")

                throttled = resp.status_code == 429 or zoho_code == CONCURRENCY_LIMIT_CODE
                if throttled and ("day" in message.lower() or "daily" in message.lower()):
                    self.quota.mark_exhausted()
                    self.quota.check()  # raises DAILY_QUOTA_EXHAUSTED with reset time
                if throttled:
                    self.stats["upstream_429"] += 1
                    retry_after = _retry_after(resp)
                    self.limiter.penalize(retry_after if retry_after is not None else 2.0)
                    failure = ConnectorError(
                        ErrorCode.RATE_LIMITED,
                        "Zoho rate limit hit" + (" (too many concurrent requests)" if zoho_code == CONCURRENCY_LIMIT_CODE else ""),
                        retry_after_s=retry_after if retry_after is not None else 2.0,
                    )
                elif resp.status_code >= 500:
                    failure = ConnectorError(ErrorCode.UPSTREAM_ERROR, f"Zoho returned HTTP {resp.status_code}")
                else:
                    raise ConnectorError(ErrorCode.INVALID_INPUT, message or f"Zoho rejected the request (HTTP {resp.status_code})", details={"zoho_code": zoho_code})

            # Retryable: 429/1070, 5xx, timeouts, network errors -- all idempotent GETs.
            delay = backoff_delay(attempt, retry_after)
            elapsed = time.monotonic() - started
            if attempt >= self.settings.max_retries or elapsed + delay > self.settings.max_wait_seconds:
                assert failure is not None
                raise failure
            attempt += 1
            self.stats["retries"] += 1
            log.info("retrying GET %s in %.1fs (attempt %d): %s", path, delay, attempt, failure)
            await self.sleep(delay)
