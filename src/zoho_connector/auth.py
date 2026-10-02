"""Zoho OAuth 2.0 (authorization-code grant, offline access).

Flow used by `zoho-connector connect`:
  1. Open https://accounts.zoho.<dc>/oauth/v2/auth with read-only scopes, access_type=offline,
     prompt=consent and a random `state`.
  2. A loopback HTTP server on localhost receives ?code=...&state=...&accounts-server=...
  3. Exchange the code at <accounts-server>/oauth/v2/token for an access token (1h) and a
     long-lived refresh token. `api_domain` in the response tells us which API DC to call.
  4. Persist the connection in the token store; the MCP server refreshes access tokens itself.

Self-client grant (headless): paste a code generated in the Zoho API console instead of steps 1-2.
"""

from __future__ import annotations

import asyncio
import http.server
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any

import httpx

from .config import DATA_CENTRES, Settings
from .errors import ConnectorError, ErrorCode
from .store import Connection, TokenStore

log = logging.getLogger(__name__)

REFRESH_SKEW_SECONDS = 120  # refresh this long before the access token actually expires


def build_authorize_url(settings: Settings, state: str) -> str:
    params = {
        "scope": settings.scopes,
        "client_id": settings.client_id,
        "response_type": "code",
        "access_type": "offline",  # we need a refresh token
        "prompt": "consent",  # always show the consent screen so scopes are visible to the merchant
        "redirect_uri": settings.redirect_uri,
        "state": state,
    }
    return f"{settings.resolved_accounts_url()}/oauth/v2/auth?{urllib.parse.urlencode(params)}"


def trusted_accounts_server(settings: Settings, candidate: str | None) -> str:
    """Zoho tells us (via `accounts-server`) which DC the user actually signed in to.
    Only accept known Zoho hosts (or the configured override), so the client secret can
    never be sent to a host injected into the redirect."""
    configured = settings.resolved_accounts_url()
    if not candidate:
        return configured
    candidate = candidate.rstrip("/")
    allowed = {acc for acc, _ in DATA_CENTRES.values()} | {configured}
    if candidate not in allowed:
        raise ConnectorError(ErrorCode.INVALID_INPUT, f"Untrusted accounts-server in OAuth redirect: {candidate}")
    return candidate


def dc_for_accounts_server(accounts_url: str, fallback: str) -> str:
    for dc, (acc, _) in DATA_CENTRES.items():
        if acc == accounts_url:
            return dc
    return fallback


@dataclass
class CallbackResult:
    code: str
    accounts_server: str | None
    location: str | None


_SUCCESS_PAGE = b"""<!doctype html><meta charset=utf-8><title>Connected</title>
<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;line-height:1.5">
<h2>Zoho Inventory connected</h2><p>Read-only access was granted. You can close this tab and
return to the terminal.</p></body>"""


def wait_for_callback(settings: Settings, state: str, *, open_browser: bool = True, timeout: float = 300) -> CallbackResult:
    """Run a one-shot loopback server and return the authorization code."""
    result: dict[str, Any] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            if qs.get("state") != state:
                result["error"] = "state mismatch (possible CSRF); start `connect` again"
            elif "error" in qs:
                result["error"] = f"authorization denied: {qs['error']}"
            elif "code" not in qs:
                result["error"] = "no authorization code in redirect"
            else:
                result.update(code=qs["code"], accounts_server=qs.get("accounts-server"), location=qs.get("location"))
            ok = "error" not in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_PAGE if ok else f"<h2>Connection failed</h2><p>{result['error']}</p>".encode())
            done.set()

        def log_message(self, *args: Any) -> None:  # keep the auth code out of terminal logs
            pass

    server = http.server.HTTPServer(("127.0.0.1", settings.redirect_port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = build_authorize_url(settings, state)
        print(f"\nOpen this URL to authorize read-only access:\n\n  {url}\n", flush=True)
        if open_browser:
            webbrowser.open(url)
        if not done.wait(timeout):
            raise ConnectorError(ErrorCode.TIMEOUT, "Timed out waiting for the OAuth redirect")
    finally:
        server.shutdown()
        server.server_close()
    if "error" in result:
        raise ConnectorError(ErrorCode.AUTH_EXPIRED, result["error"])
    return CallbackResult(result["code"], result.get("accounts_server"), result.get("location"))


def _raise_for_token_error(payload: dict[str, Any], status: int) -> None:
    # Zoho's token endpoint often returns HTTP 200 with {"error": "..."} on failure.
    if status >= 400 or "error" in payload or "access_token" not in payload:
        reason = payload.get("error", f"HTTP {status}")
        raise ConnectorError(ErrorCode.AUTH_EXPIRED, f"Zoho token endpoint rejected the request: {reason}")


async def exchange_code(
    settings: Settings,
    code: str,
    accounts_url: str,
    *,
    redirect_uri: str | None,
    http: httpx.AsyncClient,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "client_id": settings.client_id,
        "client_secret": settings.client_secret,
        "code": code,
    }
    if redirect_uri:  # self-client codes are exchanged without a redirect_uri
        data["redirect_uri"] = redirect_uri
    resp = await http.post(f"{accounts_url}/oauth/v2/token", data=data)
    payload = resp.json()
    _raise_for_token_error(payload, resp.status_code)
    if "refresh_token" not in payload:
        raise ConnectorError(
            ErrorCode.AUTH_EXPIRED,
            "Zoho did not return a refresh token. Revoke the app's existing grant in Zoho and connect again.",
        )
    return payload


async def revoke(conn: Connection, http: httpx.AsyncClient) -> None:
    await http.post(f"{conn.accounts_url}/oauth/v2/token/revoke", params={"token": conn.refresh_token})


class TokenManager:
    """Hands out a valid access token, refreshing at most once at a time (single-flight)."""

    def __init__(self, settings: Settings, store: TokenStore, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.store = store
        self.http = http
        self._lock = asyncio.Lock()
        self._conn: Connection | None = None
        self.refresh_count = 0

    @property
    def connection(self) -> Connection:
        if self._conn is None:
            self._conn = self.store.load(self.settings.connection)
        if self._conn is None:
            raise ConnectorError(
                ErrorCode.NOT_CONNECTED,
                f"No Zoho connection named {self.settings.connection!r}. Run `zoho-connector connect` first.",
            )
        return self._conn

    async def access_token(self) -> str:
        conn = self.connection
        if conn.access_token and time.time() < conn.access_expires_at - REFRESH_SKEW_SECONDS:
            return conn.access_token
        return await self.refresh(stale_token=conn.access_token)

    async def refresh(self, stale_token: str | None = None) -> str:
        async with self._lock:
            conn = self.connection
            # Another task refreshed while we waited for the lock: reuse its token.
            if conn.access_token and conn.access_token != stale_token and time.time() < conn.access_expires_at:
                return conn.access_token
            resp = await self.http.post(
                f"{conn.accounts_url}/oauth/v2/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                    "refresh_token": conn.refresh_token,
                },
            )
            try:
                payload = resp.json()
            except ValueError:
                raise ConnectorError(ErrorCode.UPSTREAM_ERROR, f"Token refresh failed with HTTP {resp.status_code}")
            _raise_for_token_error(payload, resp.status_code)
            conn.access_token = payload["access_token"]
            conn.access_expires_at = time.time() + float(payload.get("expires_in", 3600))
            if payload.get("api_domain"):
                conn.api_domain = payload["api_domain"].rstrip("/")
            self.store.save(conn)
            self.refresh_count += 1
            log.info("access token refreshed for connection %s", conn.name)
            return conn.access_token
