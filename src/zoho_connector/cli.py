"""Command line: connect a merchant's Zoho org, inspect it, and run the MCP server.

  zoho-connector connect            OAuth in the browser (read-only scopes), stores tokens
  zoho-connector connect --code X   headless: exchange a Self Client code from api-console.zoho.<dc>
  zoho-connector status             show the stored connection and verify it with one API call
  zoho-connector disconnect         revoke the refresh token at Zoho and forget it locally
  zoho-connector serve [--demo]     run the MCP server (stdio by default)
  zoho-connector mock-server        run the mock Zoho on :8900 to try the browser OAuth flow
  zoho-connector spec               write the MCP tool specification to docs/tools.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import sys
import time
from pathlib import Path

import httpx

from . import auth
from .config import READ_SCOPES, Settings
from .errors import ConnectorError
from .runtime import fetch_organizations
from .store import Connection, default_store


def _require_client(settings: Settings) -> None:
    if not settings.client_id or not settings.client_secret:
        sys.exit(
            "ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET are not set.\n"
            "Create a 'Server-based' (or 'Self Client') app at https://api-console.zoho.in, then copy "
            ".env.example to .env and fill them in. To try everything without Zoho, run: zoho-connector serve --demo"
        )


async def _connect(settings: Settings, code: str | None, org_id: str | None, open_browser: bool) -> None:
    _require_client(settings)
    redirect_uri: str | None = settings.redirect_uri
    accounts_url = settings.resolved_accounts_url()
    if code is None:
        state = secrets.token_urlsafe(24)
        result = await asyncio.to_thread(auth.wait_for_callback, settings, state, open_browser=open_browser)
        code = result.code
        accounts_url = auth.trusted_accounts_server(settings, result.accounts_server)
    else:
        redirect_uri = None  # Self Client codes are exchanged without a redirect URI

    async with httpx.AsyncClient(timeout=settings.request_timeout) as http:
        payload = await auth.exchange_code(settings, code, accounts_url, redirect_uri=redirect_uri, http=http)
        api_domain = payload.get("api_domain", settings.resolved_api_domain()).rstrip("/")
        orgs = await fetch_organizations(http, api_domain, payload["access_token"])

    if not orgs:
        sys.exit("This Zoho user has no Zoho Inventory organization.")
    if org_id:
        chosen = next((o for o in orgs if str(o["organization_id"]) == org_id), None)
        if chosen is None:
            sys.exit(f"Organization {org_id} not found. Available: " + ", ".join(f"{o['name']} ({o['organization_id']})" for o in orgs))
    elif len(orgs) == 1:
        chosen = orgs[0]
    else:
        print("This user can access several organizations:")
        for i, o in enumerate(orgs, 1):
            print(f"  {i}. {o['name']} ({o['organization_id']})")
        chosen = orgs[int(input("Pick one [1]: ") or "1") - 1]

    store = default_store(settings.state_dir)
    store.save(Connection(
        name=settings.connection,
        dc=auth.dc_for_accounts_server(accounts_url, settings.dc),
        accounts_url=accounts_url,
        api_domain=api_domain,
        refresh_token=payload["refresh_token"],
        access_token=payload["access_token"],
        access_expires_at=time.time() + float(payload.get("expires_in", 3600)),
        organization_id=str(chosen["organization_id"]),
        organization_name=chosen.get("name", ""),
        currency_code=chosen.get("currency_code", "INR"),
        scopes=",".join(READ_SCOPES),
    ))
    print(f"\nConnected '{settings.connection}' to {chosen.get('name')} ({chosen['organization_id']}), data centre {api_domain}")
    print(f"Access: read-only ({len(READ_SCOPES)} scopes). Tokens stored in: {store.description}")
    print("Next: zoho-connector serve   (or add it to your MCP client, see README)")


async def _status(settings: Settings) -> None:
    from .runtime import live_runtime

    runtime = await live_runtime(settings)
    try:
        conn = runtime.tokens.connection
        info = runtime.tools.connector_status()
        await runtime.client.get("/organizations", org_scoped=False, cache_ttl=0)  # proves the token works
        info["verified"] = "ok (1 API call)"
        info["access_token_valid_for_s"] = max(0, int(conn.access_expires_at - time.time()))
        print(json.dumps(info, indent=2))
    finally:
        await runtime.aclose()


async def _disconnect(settings: Settings) -> None:
    store = default_store(settings.state_dir)
    conn = store.load(settings.connection)
    if conn is None:
        print(f"No connection named {settings.connection!r}.")
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout) as http:
        try:
            await auth.revoke(conn, http)
            print("Refresh token revoked at Zoho.")
        except httpx.HTTPError as exc:
            print(f"Could not reach Zoho to revoke ({type(exc).__name__}); removing locally anyway.")
    store.delete(settings.connection)
    print(f"Connection {settings.connection!r} removed from {store.description}.")


async def _spec(out: Path) -> None:
    from mcp import Client

    from .server import create_server

    server, _ = create_server(Settings.from_env(dotenv=None), demo=True)
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        spec = {
            "server": {"name": "zoho-inventory", "version": "0.1.0", "transport": ["stdio", "streamable-http"]},
            "instructions": client.instructions,
            "tools": [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools],
        }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(tools)} tool definitions to {out}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="zoho-connector", description="Read-only Zoho Inventory connector for Agent Studio (MCP)")
    parser.add_argument("--connection", help="Connection name (one per merchant org). Default: ZOHO_CONNECTION or 'default'.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("connect", help="Authorize read-only access to a Zoho Inventory org")
    p.add_argument("--code", help="Self Client authorization code (headless setup)")
    p.add_argument("--org-id", help="Organization to bind when the user has several")
    p.add_argument("--no-browser", action="store_true", help="Print the consent URL instead of opening a browser")

    sub.add_parser("status", help="Show and verify the stored connection")
    sub.add_parser("disconnect", help="Revoke and delete the stored connection")

    p = sub.add_parser("serve", help="Run the MCP server")
    p.add_argument("--demo", action="store_true", help="Use the built-in mock Zoho with fictional data (no account needed)")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("mock-server", help="Run the mock Zoho accounts + API server")
    p.add_argument("--port", type=int, default=8900)

    p = sub.add_parser("spec", help="Write the MCP tool specification")
    p.add_argument("--out", type=Path, default=Path("docs/tools.json"))

    args = parser.parse_args(argv)
    from .server import configure_logging

    configure_logging("INFO" if args.command == "serve" else "WARNING")
    settings = Settings.from_env()
    if args.connection:
        settings.connection = args.connection

    try:
        if args.command == "connect":
            asyncio.run(_connect(settings, args.code, args.org_id, not args.no_browser))
        elif args.command == "status":
            asyncio.run(_status(settings))
        elif args.command == "disconnect":
            asyncio.run(_disconnect(settings))
        elif args.command == "mock-server":
            from .mock.server import run

            run(port=args.port)
        elif args.command == "spec":
            asyncio.run(_spec(args.out))
        elif args.command == "serve":
            from .server import create_server

            if not args.demo:
                _require_client(settings)
                settings.state_dir.mkdir(parents=True, exist_ok=True)
                fh = logging.FileHandler(settings.state_dir / "audit.jsonl", encoding="utf-8")
                fh.setFormatter(logging.Formatter("%(message)s"))
                logging.getLogger("zoho_connector.audit").addHandler(fh)
            server, _ = create_server(settings, demo=args.demo)
            logging.getLogger(__name__).info("starting MCP server (%s, %s)", "demo data" if args.demo else f"connection {settings.connection!r}", args.transport)
            if args.transport == "stdio":
                server.run("stdio")
            else:
                server.run("streamable-http", host=args.host, port=args.port)
    except ConnectorError as exc:
        sys.exit(f"Error: {exc}\nHint: {exc.to_dict()['error']['hint']}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
