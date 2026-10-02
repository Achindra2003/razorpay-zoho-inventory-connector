"""OAuth: consent URL, redirect validation, code exchange, and the complete browser flow
(`zoho-connector connect`) against the mock Zoho running on a real local port."""

import asyncio
import socket
import threading
import time
import urllib.parse

import httpx
import pytest
import uvicorn

from zoho_connector import auth, cli
from zoho_connector.config import READ_SCOPES, Settings
from zoho_connector.errors import ConnectorError, ErrorCode
from zoho_connector.mock.server import MockZoho
from zoho_connector.store import FileStore


def test_authorize_url_requests_only_read_scopes_and_offline_access(tmp_path):
    s = Settings(client_id="cid", dc="in", state_dir=tmp_path)
    url = auth.build_authorize_url(s, "xyz")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith("https://accounts.zoho.in/oauth/v2/auth?")
    assert q["scope"][0].split(",") == list(READ_SCOPES)
    assert all(scope.endswith(".READ") for scope in READ_SCOPES)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"] and q["state"] == ["xyz"]


def test_untrusted_accounts_server_is_rejected(tmp_path):
    s = Settings(dc="in", state_dir=tmp_path)
    assert auth.trusted_accounts_server(s, "https://accounts.zoho.eu") == "https://accounts.zoho.eu"
    with pytest.raises(ConnectorError):
        auth.trusted_accounts_server(s, "https://evil.example.com")


async def test_token_endpoint_error_with_http_200_is_detected(tmp_path):
    mock = MockZoho()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app()), base_url="http://m") as http:
        with pytest.raises(ConnectorError) as exc:
            await auth.exchange_code(Settings(client_id="c", client_secret="s", state_dir=tmp_path), "bad-code", "http://m", redirect_uri=None, http=http)
    assert exc.value.code is ErrorCode.AUTH_EXPIRED


async def test_tools_report_not_connected(tmp_path):
    from zoho_connector.runtime import build_runtime
    from zoho_connector.store import MemoryStore

    async with httpx.AsyncClient() as http:
        rt = build_runtime(Settings(state_dir=tmp_path), MemoryStore(), http)
        with pytest.raises(ConnectorError) as exc:
            await rt.tools.list_items()
    assert exc.value.code is ErrorCode.NOT_CONNECTED


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mock_zoho_http():
    """The mock Zoho on a real TCP port, like `zoho-connector mock-server`."""
    port = _free_port()
    mock = MockZoho()
    server = uvicorn.Server(uvicorn.Config(mock.app(), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", mock
    server.should_exit = True
    thread.join(timeout=5)


def test_full_browser_connect_flow(tmp_path, monkeypatch, mock_zoho_http):
    base, mock = mock_zoho_http
    monkeypatch.setenv("ZOHO_TOKEN_STORE", "file")
    settings = Settings(client_id="cid", client_secret="secret", accounts_url=base, api_domain=base,
                        redirect_port=_free_port(), state_dir=tmp_path)

    # Play the merchant's browser: open consent page, click Accept, follow redirect to localhost.
    def browser(url: str) -> bool:
        def run() -> None:
            consent = httpx.get(url)
            assert "ZohoInventory.items.READ" in consent.text
            approve = urllib.parse.unquote(consent.text.split('href="')[1].split('"')[0]).replace("&amp;", "&")
            redirect = httpx.get(base + approve, follow_redirects=False)
            httpx.get(redirect.headers["location"])
        threading.Thread(target=run, daemon=True).start()
        return True

    monkeypatch.setattr(auth.webbrowser, "open", browser)
    asyncio.run(cli._connect(settings, code=None, org_id=None, open_browser=True))

    conn = FileStore(tmp_path).load("default")
    assert conn is not None
    assert conn.organization_name == "Saffron Threads (Demo)"
    assert conn.refresh_token in mock.refresh_tokens
    assert conn.api_domain == base


def test_state_mismatch_is_rejected(tmp_path, monkeypatch):
    settings = Settings(client_id="cid", redirect_port=_free_port(), state_dir=tmp_path)

    def attacker(url: str) -> bool:
        threading.Thread(target=lambda: httpx.get(f"{settings.redirect_uri}?code=stolen&state=wrong"), daemon=True).start()
        return True

    monkeypatch.setattr(auth.webbrowser, "open", attacker)
    with pytest.raises(ConnectorError) as exc:
        auth.wait_for_callback(settings, "expected-state", timeout=10)
    assert "state mismatch" in exc.value.message
