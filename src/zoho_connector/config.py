"""Runtime configuration, read from environment variables (and an optional .env file).

Nothing here is secret except the OAuth client secret. Tokens are never read from env;
they live in the token store (see auth.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Zoho runs isolated data centres. An org created on zoho.in can only be reached through
# accounts.zoho.in / www.zohoapis.in, so the DC must be known before the first call.
DATA_CENTRES: dict[str, tuple[str, str]] = {
    # dc: (accounts server, api domain)
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in"),
    "com": ("https://accounts.zoho.com", "https://www.zohoapis.com"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "com.au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    "ca": ("https://accounts.zohocloud.ca", "https://www.zohoapis.ca"),
    "com.cn": ("https://accounts.zoho.com.cn", "https://www.zohoapis.com.cn"),
    "sa": ("https://accounts.zoho.sa", "https://www.zohoapis.sa"),
}

# Read-only scopes only. Zoho enforces these server-side, so even a bug in this
# connector cannot create, edit or delete anything in the merchant's account.
READ_SCOPES: tuple[str, ...] = (
    "ZohoInventory.settings.READ",  # organizations + locations/warehouses
    "ZohoInventory.items.READ",
    "ZohoInventory.salesorders.READ",
    "ZohoInventory.packages.READ",
    "ZohoInventory.shipmentorders.READ",
)

# Daily API quota per Zoho Inventory plan (calls/day/org), from Zoho's API docs.
PLAN_DAILY_QUOTA = {"free": 1000, "standard": 2000, "professional": 5000, "premium": 10000, "enterprise": 10000}


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real env vars always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else default


def default_state_dir() -> Path:
    return Path(os.environ.get("ZOHO_CONNECTOR_HOME") or Path.home() / ".zoho-connector")


@dataclass
class Settings:
    client_id: str = ""
    client_secret: str = ""
    dc: str = "in"
    connection: str = "default"  # name of the stored connection (one merchant org each)
    redirect_port: int = 8765

    # Overrides used by the mock/demo mode or a proxy. Normally derived from `dc`.
    accounts_url: str | None = None
    api_domain: str | None = None

    # Rate limiting. Zoho allows 100 req/min/org; keep headroom for the merchant's other integrations.
    per_minute_limit: int = 90
    max_concurrency: int = 5  # free plan allows 5 concurrent calls, paid plans 10
    daily_quota: int = 1000
    max_wait_seconds: float = 15.0  # longest we will block an agent before failing with RATE_LIMITED
    max_retries: int = 3
    request_timeout: float = 20.0

    cache_ttl_seconds: float = 60.0
    pii_mode: str = "masked"  # "masked" | "full" -- an operator decision, never the model's
    state_dir: Path = field(default_factory=default_state_dir)

    @property
    def scopes(self) -> str:
        return ",".join(READ_SCOPES)

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.redirect_port}/callback"

    def resolved_accounts_url(self) -> str:
        return (self.accounts_url or DATA_CENTRES[self.dc][0]).rstrip("/")

    def resolved_api_domain(self) -> str:
        return (self.api_domain or DATA_CENTRES[self.dc][1]).rstrip("/")

    @classmethod
    def from_env(cls, dotenv: Path | None = Path(".env")) -> "Settings":
        if dotenv is not None:
            _load_dotenv(dotenv)
        dc = os.environ.get("ZOHO_DC", "in").lower().lstrip(".")
        if dc not in DATA_CENTRES:
            raise ValueError(f"ZOHO_DC must be one of {sorted(DATA_CENTRES)}, got {dc!r}")
        plan = os.environ.get("ZOHO_PLAN", "free").lower()
        pii_mode = os.environ.get("ZOHO_PII_MODE", "masked").lower()
        if pii_mode not in ("masked", "full"):
            raise ValueError("ZOHO_PII_MODE must be 'masked' or 'full'")
        return cls(
            client_id=os.environ.get("ZOHO_CLIENT_ID", ""),
            client_secret=os.environ.get("ZOHO_CLIENT_SECRET", ""),
            dc=dc,
            connection=os.environ.get("ZOHO_CONNECTION", "default"),
            redirect_port=_int("ZOHO_REDIRECT_PORT", 8765),
            accounts_url=os.environ.get("ZOHO_ACCOUNTS_URL") or None,
            api_domain=os.environ.get("ZOHO_API_DOMAIN") or None,
            per_minute_limit=_int("ZOHO_RATE_PER_MINUTE", 90),
            max_concurrency=_int("ZOHO_MAX_CONCURRENCY", 5 if plan == "free" else 10),
            daily_quota=_int("ZOHO_DAILY_QUOTA", PLAN_DAILY_QUOTA.get(plan, 1000)),
            max_wait_seconds=_float("ZOHO_MAX_WAIT_SECONDS", 15.0),
            max_retries=_int("ZOHO_MAX_RETRIES", 3),
            request_timeout=_float("ZOHO_TIMEOUT_SECONDS", 20.0),
            cache_ttl_seconds=_float("ZOHO_CACHE_TTL_SECONDS", 60.0),
            pii_mode=pii_mode,
        )
