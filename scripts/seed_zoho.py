"""Load the fictional demo dataset into a real (free) Zoho Inventory org.

This script is deliberately SEPARATE from the connector: it needs write access, the connector
never does. It uses a short-lived Self Client grant with full scope, seeds the hand-written demo
records (the ones the README and evals refer to), and revokes the grant when done.

  1. https://api-console.zoho.in -> Add Client -> Self Client (it gets its OWN client id/secret)
     -> Generate Code   scope: ZohoInventory.FullAccess.all   duration: 10 minutes
  2. uv run python scripts/seed_zoho.py --code 1000.xxxx --client-id 1000.SELF... --client-secret ... [--dry-run]

A Self Client code can only be exchanged with the Self Client's id/secret. If --client-id /
--client-secret are omitted, ZOHO_SEED_CLIENT_ID / ZOHO_SEED_CLIENT_SECRET (then ZOHO_CLIENT_ID /
ZOHO_CLIENT_SECRET) from .env are used. ZOHO_DC selects the data centre. About 70 API calls (well inside
the free plan's 1,000/day); paced under Zoho's 100/min limit. The free plan has one warehouse,
so per-location stock from the demo data is collapsed into it.

Caveat: written against Zoho's public API docs and validated with --dry-run; Zoho orgs differ
(inventory tracking, packaging and shipment settings), so a step may need a small payload tweak.
Each failure prints Zoho's message and the script continues where it safely can.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zoho_connector.config import Settings  # noqa: E402
from zoho_connector.mock.data import build_dataset  # noqa: E402

DEMO_ORDERS = {"SO-00101", "SO-00102", "SO-00103", "SO-00104", "SO-00105", "SO-00106"}
PACE_SECONDS = 0.7  # ~85 calls/minute


class Seeder:
    def __init__(self, api: str, token: str, org_id: str, dry_run: bool) -> None:
        self.api = api
        self.org_id = org_id
        self.dry_run = dry_run
        self.http = httpx.Client(timeout=30, headers={"Authorization": f"Zoho-oauthtoken {token}"})
        self.calls = 0
        self._fake_id = 0

    def call(self, method: str, path: str, body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {"organization_id": self.org_id, **(params or {})}
        if self.dry_run:
            self._fake_id += 1
            print(f"  [dry-run] {method} {path} {json.dumps(params)} {json.dumps(body)[:160] if body else ''}")
            fid = f"dry{self._fake_id}"
            return {"code": 0, "item": {"item_id": fid}, "contact": {"contact_id": fid},
                    "salesorder": {"salesorder_id": fid, "line_items": [{"line_item_id": f"{fid}-{i}"} for i in range(5)]},
                    "package": {"package_id": fid}, "shipmentorder": {"shipment_id": fid},
                    "salesorders": [{"salesorder_id": fid}]}
        time.sleep(PACE_SECONDS)
        self.calls += 1
        resp = self.http.request(method, f"{self.api}/inventory/v1{path}", params=params, json=body)
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400 or data.get("code", 0) != 0:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status_code}: {data.get('message', resp.text[:200])}")
        return data


def exchange_self_client_code(settings: Settings, code: str) -> dict[str, Any]:
    resp = httpx.post(f"{settings.resolved_accounts_url()}/oauth/v2/token", data={
        "grant_type": "authorization_code", "client_id": settings.client_id,
        "client_secret": settings.client_secret, "code": code,
    })
    data = resp.json()
    if "access_token" not in data:
        hint = ""
        if data.get("error") == "invalid_client":
            hint = "\nUse the Self Client's own Client ID/Secret (--client-id/--client-secret), not the Server-based app's."
        elif data.get("error") == "invalid_code":
            hint = "\nThe code expired or was already used. Generate a new one (codes are single-use, max 10 minutes)."
        sys.exit(f"Code exchange failed: {data.get('error', resp.text)}{hint}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code", help="Self Client code with ZohoInventory.FullAccess.all scope")
    parser.add_argument("--client-id", help="Self Client client id (default: ZOHO_SEED_CLIENT_ID, then ZOHO_CLIENT_ID)")
    parser.add_argument("--client-secret", help="Self Client client secret (default: ZOHO_SEED_CLIENT_SECRET, then ZOHO_CLIENT_SECRET)")
    parser.add_argument("--org-id", help="Organization to seed (default: the first one)")
    parser.add_argument("--dry-run", action="store_true", help="Print the requests without calling Zoho")
    parser.add_argument("--fulfilment-only", action="store_true",
                        help="Orders already exist: only add packages + shipments to SO-00101..SO-00106 (resume after a partial run)")
    args = parser.parse_args()

    settings = Settings.from_env()
    settings.client_id = args.client_id or os.environ.get("ZOHO_SEED_CLIENT_ID") or settings.client_id
    settings.client_secret = args.client_secret or os.environ.get("ZOHO_SEED_CLIENT_SECRET") or settings.client_secret
    data = build_dataset()
    if args.dry_run:
        seeder, refresh = Seeder("https://dry-run", "x", args.org_id or "ORG", True), None
    else:
        if not args.code:
            sys.exit("--code is required (or use --dry-run)")
        tokens = exchange_self_client_code(settings, args.code)
        api = tokens.get("api_domain", settings.resolved_api_domain())
        orgs = httpx.get(f"{api}/inventory/v1/organizations", headers={"Authorization": f"Zoho-oauthtoken {tokens['access_token']}"}).json()["organizations"]
        org_id = args.org_id or orgs[0]["organization_id"]
        print(f"Seeding organization {org_id} ({next((o['name'] for o in orgs if o['organization_id'] == org_id), '?')})")
        seeder, refresh = Seeder(api, tokens["access_token"], org_id, False), tokens.get("refresh_token")

    try:
        if args.fulfilment_only:
            seed_fulfilment_only(seeder, data)
        else:
            seed(seeder, data)
    finally:
        if refresh and not args.dry_run:
            httpx.post(f"{settings.resolved_accounts_url()}/oauth/v2/token/revoke", params={"token": refresh})
            print("Write grant revoked.")
    print(f"Done. {seeder.calls} API calls.")


def seed(s: Seeder, data: dict[str, Any]) -> None:
    item_ids: dict[str, str] = {}
    print(f"Items ({len(data['items'])})")
    for it in data["items"]:
        body = {
            "name": it["name"], "sku": it["sku"], "rate": it["rate"], "purchase_rate": it["purchase_rate"],
            "unit": it["unit"], "description": it["description"], "item_type": "inventory", "product_type": "goods",
            "reorder_level": it["reorder_level"], "initial_stock": it["stock_on_hand"], "initial_stock_rate": it["purchase_rate"],
        }
        try:
            item_ids[it["sku"]] = s.call("POST", "/items", body)["item"]["item_id"]
            if it["status"] == "inactive":
                s.call("POST", f"/items/{item_ids[it['sku']]}/inactive")
        except RuntimeError as exc:
            print(f"  ! {it['sku']}: {exc}")

    orders = [o for o in data["salesorders"] if o["salesorder_number"] in DEMO_ORDERS]
    customers: dict[str, str] = {}
    print(f"Customers + demo orders ({len(orders)})")
    for so in orders:
        name = so["customer_name"]
        if name not in customers:
            first, _, last = name.partition(" ")
            customers[name] = s.call("POST", "/contacts", {
                "contact_name": name, "contact_type": "customer", "shipping_address": so["shipping_address"],
                "contact_persons": [{"first_name": first, "last_name": last, "email": so["email"], "mobile": so["mobile"], "is_primary_contact": True}],
            })["contact"]["contact_id"]
        try:
            created = s.call("POST", "/salesorders", {
                "customer_id": customers[name], "salesorder_number": so["salesorder_number"],
                "date": so["date"], "reference_number": so["reference_number"],
                "notes": so["notes"], "shipment_date": so["date"],
                "line_items": [{"item_id": item_ids[li["sku"]], "quantity": li["quantity"], "rate": li["rate"]} for li in so["line_items"] if li["sku"] in item_ids],
            }, params={"ignore_auto_number_generation": "true"})["salesorder"]  # keep SO-00101.. so docs/evals match
        except RuntimeError as exc:
            print(f"  ! {so['salesorder_number']}: {exc}")
            continue
        so_id = created["salesorder_id"]
        if so["status"] == "void":
            s.call("POST", f"/salesorders/{so_id}/status/void")
            print(f"  {so['salesorder_number']} -> {so['reference_number']} (voided)")
            continue
        s.call("POST", f"/salesorders/{so_id}/status/confirmed")
        add_fulfilment(s, data, so, so_id, created.get("line_items", []))
        print(f"  {so['salesorder_number']} -> {so['reference_number']}")



def _line_ids(so: dict[str, Any], zoho_lines: list[dict[str, Any]]) -> dict[str, str]:
    """Map our SKUs to Zoho's line_item_ids (by SKU, then name, then position)."""
    out: dict[str, str] = {}
    for i, li in enumerate(so["line_items"]):
        match = next((z for z in zoho_lines if (z.get("sku") or "").lower() == li["sku"].lower()), None)             or next((z for z in zoho_lines if z.get("name") == li["name"]), None)             or (zoho_lines[i] if i < len(zoho_lines) else None)
        if match:
            out[li["sku"]] = match["line_item_id"]
    return out


def add_fulfilment(s: Seeder, data: dict[str, Any], so: dict[str, Any], so_id: str, zoho_lines: list[dict[str, Any]]) -> None:
    line_ids = _line_ids(so, zoho_lines)
    for p in [p for p in data["packages"] if p["salesorder_id"] == so["salesorder_id"]]:
        try:
            # Orgs with auto-numbering off reject packages/shipments without an explicit number.
            pkg_id = s.call("POST", "/packages", {
                "package_number": p["package_number"],
                "date": p["date"],
                "line_items": [{"so_line_item_id": line_ids[li["sku"]], "quantity": li["quantity"]} for li in p["line_items"]],
            }, params={"salesorder_id": so_id, "ignore_auto_number_generation": "true"})["package"]["package_id"]
            ship = p["shipment_order"]
            if ship:
                sh_id = s.call("POST", "/shipmentorders", {
                    "shipment_number": ship["shipment_number"],
                    "date": ship["shipment_date"], "delivery_method": ship["carrier"], "tracking_number": ship["tracking_number"] or "",
                }, params={"package_ids": pkg_id, "salesorder_id": so_id, "ignore_auto_number_generation": "true"})["shipmentorder"]["shipment_id"]
                if ship["status"] == "delivered":
                    s.call("POST", f"/shipmentorders/{sh_id}/status/delivered")
            print(f"    {p['package_number']}: {p['status']}")
        except (RuntimeError, KeyError) as exc:
            print(f"  ! {so['salesorder_number']} {p['package_number']}: {exc}")


def seed_fulfilment_only(s: Seeder, data: dict[str, Any]) -> None:
    print("Packages + shipments for existing demo orders")
    for so in [o for o in data["salesorders"] if o["salesorder_number"] in DEMO_ORDERS and o["status"] != "void"]:
        found = s.call("GET", "/salesorders", params={"salesorder_number": so["salesorder_number"]}).get("salesorders", [])
        if not found:
            print(f"  ! {so['salesorder_number']} not found in Zoho; run the full seed first")
            continue
        so_id = found[0]["salesorder_id"]
        detail = s.call("GET", f"/salesorders/{so_id}").get("salesorder", {})
        if detail.get("packages"):
            print(f"  {so['salesorder_number']}: already has packages, skipping")
            continue
        print(f"  {so['salesorder_number']}")
        add_fulfilment(s, data, so, so_id, detail.get("line_items", []))


if __name__ == "__main__":
    main()
