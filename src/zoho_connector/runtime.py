"""Wires settings, token store, HTTP client and tools together for one merchant connection."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .auth import TokenManager, exchange_code
from .client import TTLCache, ZohoClient
from .config import READ_SCOPES, Settings
from .ratelimit import DailyQuota, MinuteWindow
from .store import Connection, MemoryStore, TokenStore, default_store
from .tools import InventoryTools

DEMO_BASE = "http://mock-zoho.local"  # never resolved: requests go to the in-process ASGI app


@dataclass
class Runtime:
    settings: Settings
    store: TokenStore
    http: httpx.AsyncClient
    tokens: TokenManager
    client: ZohoClient
    tools: InventoryTools
    mode: str  # "live" | "demo"
    mock: object | None = None  # the in-process MockZoho in demo mode (tests use it for fault injection)

    async def aclose(self) -> None:
        await self.http.aclose()


def _quota_path(settings: Settings, org_id: str) -> Path:
    return settings.state_dir / f"quota-{settings.connection}-{org_id or 'unknown'}.json"


async def fetch_organizations(http: httpx.AsyncClient, api_domain: str, access_token: str) -> list[dict]:
    resp = await http.get(
        f"{api_domain}/inventory/v1/organizations",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
    )
    resp.raise_for_status()
    return resp.json().get("organizations", [])


def build_runtime(settings: Settings, store: TokenStore, http: httpx.AsyncClient, *, mode: str = "live") -> Runtime:
    tokens = TokenManager(settings, store, http)
    conn = store.load(settings.connection)
    org_id = conn.organization_id if conn else ""
    quota = DailyQuota(settings.daily_quota, _quota_path(settings, org_id) if mode == "live" else None)
    client = ZohoClient(
        settings,
        tokens,
        http,
        limiter=MinuteWindow(settings.per_minute_limit, settings.max_wait_seconds),
        quota=quota,
        cache=TTLCache(settings.cache_ttl_seconds),
    )
    return Runtime(settings, store, http, tokens, client, InventoryTools(client, pii_full=settings.pii_mode == "full"), mode)


async def live_runtime(settings: Settings) -> Runtime:
    store = default_store(settings.state_dir)
    http = httpx.AsyncClient(timeout=settings.request_timeout)
    return build_runtime(settings, store, http, mode="live")


async def demo_runtime(settings: Settings) -> Runtime:
    """Zero-setup mode: an in-process mock Zoho, connected through the real OAuth code path
    (the merchant's 'Accept' click is simulated). No network, no account, fictional data."""
    from .mock.server import MockZoho

    mock = MockZoho()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app()), base_url=DEMO_BASE, timeout=settings.request_timeout)
    settings.accounts_url = DEMO_BASE
    settings.api_domain = DEMO_BASE
    settings.client_id = settings.client_id or "demo-client"
    settings.client_secret = settings.client_secret or "demo-secret"
    store = MemoryStore()

    code = mock.issue_code(settings.client_id)
    payload = await exchange_code(settings, code, DEMO_BASE, redirect_uri=settings.redirect_uri, http=http)
    orgs = await fetch_organizations(http, payload["api_domain"], payload["access_token"])
    org = orgs[0]
    store.save(Connection(
        name=settings.connection,
        dc="demo",
        accounts_url=DEMO_BASE,
        api_domain=payload["api_domain"],
        refresh_token=payload["refresh_token"],
        access_token=payload["access_token"],
        access_expires_at=time.time() + float(payload["expires_in"]),
        organization_id=org["organization_id"],
        organization_name=org["name"],
        currency_code=org.get("currency_code", "INR"),
        scopes=",".join(READ_SCOPES),
    ))
    runtime = build_runtime(settings, store, http, mode="demo")
    runtime.mock = mock
    return runtime
