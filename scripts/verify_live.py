"""Check every tool against a REAL connected Zoho org seeded with scripts/seed_zoho.py.

  uv run zoho-connector connect      (once)
  uv run python scripts/verify_live.py

Prints PASS/FAIL per check plus the raw Zoho fields behind any failure, so field-name or
filter differences between Zoho's docs and a real org show up immediately. Read-only, ~30 API calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zoho_connector.config import Settings  # noqa: E402
from zoho_connector.errors import ConnectorError  # noqa: E402
from zoho_connector.runtime import live_runtime  # noqa: E402
from zoho_connector.tools import AvailabilityLine  # noqa: E402

results: list[tuple[str, bool, str]] = []


async def check(name: str, fn: Callable[[], Awaitable[Any]], ok: Callable[[Any], bool], show: Callable[[Any], str]) -> Any:
    try:
        value = await fn()
        passed = bool(ok(value))
        detail = show(value)
    except ConnectorError as exc:
        value, passed, detail = None, False, f"{exc.code.value}: {exc.message}"
    except Exception as exc:  # noqa: BLE001
        value, passed, detail = None, False, f"{type(exc).__name__}: {exc}"
    results.append((name, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'}  {name:<44} {detail}")
    return value


def _pick(d: dict[str, Any], words: tuple[str, ...]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if any(w in k.lower() for w in words) and not isinstance(v, list)}


async def probe(t, c) -> None:
    """Print the raw Zoho fields behind dates, contacts and customer search (read-only, ~12 calls)."""
    date_words = ("date", "ship", "deliver", "track", "carrier", "status", "time")
    so_id = (await c.get("/salesorders", {"salesorder_number": "SO-00101"}, cache_ttl=0))["salesorders"][0]["salesorder_id"]
    so = (await c.get(f"/salesorders/{so_id}", cache_ttl=0))["salesorder"]
    print("SO contact fields:", json.dumps({k: v for k, v in so.items() if any(w in k.lower() for w in ("email", "phone", "mobile", "contact"))}, default=str)[:800])
    print("SO packages[0]:", json.dumps(so.get("packages", [{}])[0], default=str)[:800])
    print("SO shipments key:", json.dumps({k: v for k, v in so.items() if "ship" in k.lower()}, default=str)[:800])
    pkg_id = so["packages"][0]["package_id"]
    pkg = (await c.get(f"/packages/{pkg_id}", cache_ttl=0))["package"]
    print("package top-level:", json.dumps(_pick(pkg, date_words), default=str)[:900])
    print("package.shipment_order:", json.dumps(pkg.get("shipment_order"), default=str)[:900])
    sh_id = (pkg.get("shipment_order") or {}).get("shipment_id") or so["packages"][0].get("shipment_id")
    if sh_id:
        try:
            sh = await c.get(f"/shipmentorders/{sh_id}", cache_ttl=0)
            body = sh.get("shipmentorder") or sh.get("shipment_order") or sh
            print("shipmentorder:", json.dumps(_pick(body, date_words), default=str)[:900])
        except ConnectorError as exc:
            print("shipmentorder fetch:", exc)
    for params in ({"customer_name": "Meera Nair"}, {"customer_name": "Meera"}, {"customer_name_contains": "Meera"},
                   {"customer_name_startswith": "Meera"}, {"search_text": "Meera"}):
        try:
            rows = (await c.get("/salesorders", params, cache_ttl=0)).get("salesorders", [])
            print(f"salesorders {params}: {[r['salesorder_number'] for r in rows]}")
        except ConnectorError as exc:
            print(f"salesorders {params}: {exc.code.value} {exc.message}")


async def main() -> None:
    logging.disable(logging.CRITICAL)
    rt = await live_runtime(Settings.from_env())
    t, c = rt.tools, rt.client
    if "--probe" in sys.argv:
        try:
            await probe(t, c)
        finally:
            await rt.aclose()
        return
    try:
        status = t.connector_status()
        print(f"Connected to {status['organization']['name']} ({status['organization']['id']}) via {status['api_domain']}\n")

        # Items
        await check("list_items", lambda: t.list_items(page_size=50), lambda p: p.returned >= 20, lambda p: f"{p.returned} items")
        await check("list_items(low_stock_only)", lambda: t.list_items(low_stock_only=True, page_size=50),
                    lambda p: "ST-TEE-OLV-M" in {i.sku for i in p.results}, lambda p: f"{sorted(i.sku for i in p.results)}")
        await check("search_items('kurta')", lambda: t.search_items("kurta"), lambda p: p.returned == 3, lambda p: f"{[i.sku for i in p.results]}")
        await check("search_items(sku startswith ST-MUG)", lambda: t.search_items("ST-MUG", field="sku", match="startswith"),
                    lambda p: p.returned == 2, lambda p: f"{[i.sku for i in p.results]}")
        item = await check("get_item(sku=ST-TEE-BLK-M) stock fields", lambda: t.get_item(sku="ST-TEE-BLK-M"),
                           lambda i: i.available_for_sale and i.available_for_sale > 0,  # locations are empty on single-warehouse orgs
                           lambda i: f"avail={i.available_for_sale} on_hand={i.stock_on_hand} locations={[(l.name, l.available_for_sale) for l in i.locations]}")
        if item is not None and not item.locations:
            raw = await c.get(f"/items/{item.item_id}", cache_ttl=0)
            print("      raw item stock keys:", {k: v for k, v in raw.get("item", {}).items() if "stock" in k or k in ("locations", "warehouses")})
        await check("get_item description is UntrustedText", lambda: t.get_item(sku="ST-CANDLE-SANDAL"),
                    lambda i: i.description and "IGNORE" in i.description.text, lambda i: "wrapped" if i.description else "no description")
        await check("check_availability (cart)", lambda: t.check_availability([AvailabilityLine(sku="ST-TEE-BLK-M", quantity=2), AvailabilityLine(sku="ST-TEE-OLV-M"), AvailabilityLine(sku="ST-TEE-WHT-M")]),
                    lambda r: [x.sellable for x in r.results] == [True, False, False], lambda r: f"{[(x.sku, x.reason) for x in r.results]}")

        # Orders
        for status_name in ("all", "open", "confirmed", "fulfilled", "draft", "void", "closed"):
            await check(f"list_sales_orders(status={status_name})", lambda s=status_name: t.list_sales_orders(status=s, page_size=50),
                        lambda p: True if status_name != "all" else p.returned >= 6, lambda p: f"{sorted(o.salesorder_number for o in p.results)}")
        await check("list_sales_orders(date range)", lambda: t.list_sales_orders(date_from="2026-09-10", date_to="2026-09-20", page_size=50),
                    lambda p: {o.salesorder_number for o in p.results} >= {"SO-00103", "SO-00104"}, lambda p: f"{sorted(o.salesorder_number for o in p.results)}")
        await check("search_sales_orders(reference_number)", lambda: t.search_sales_orders(reference_number="order_DemoR2mN8sKb02"),
                    lambda p: [o.salesorder_number for o in p.results] == ["SO-00102"], lambda p: f"{[o.salesorder_number for o in p.results]}")
        await check("search_sales_orders(customer_name)", lambda: t.search_sales_orders(customer_name="Meera"),
                    lambda p: "SO-00104" in {o.salesorder_number for o in p.results}, lambda p: f"{[o.salesorder_number for o in p.results]}")
        await check("get_sales_order(salesorder_number) + PII masked", lambda: t.get_sales_order(salesorder_number="SO-00102"),
                    lambda o: o.line_items and o.packages and all(v is None or "*" in v for v in (o.customer.email, o.customer.phone)),
                    lambda o: f"lines={len(o.line_items)} packages={len(o.packages)} email={o.customer.email} phone={o.customer.phone}")

        # Evidence
        expected = {"SO-00101": "DELIVERED", "SO-00102": "SHIPPED_NOT_DELIVERED", "SO-00103": "PARTIALLY_DELIVERED",
                    "SO-00104": "DELIVERED", "SO-00105": "NOT_SHIPPED", "SO-00106": "CANCELLED"}
        for number, verdict in expected.items():
            ev = await check(f"get_fulfilment_evidence({number})", lambda n=number: t.get_fulfilment_evidence(salesorder_number=n),
                             lambda e, v=verdict: e.verdict == v, lambda e: f"{e.verdict} | {e.summary} | gaps={e.evidence_gaps}")
            if ev is not None and ev.verdict != verdict:
                so = await c.get(f"/salesorders/{ev.salesorder_id}", cache_ttl=0)
                for p in so.get("salesorder", {}).get("packages", [])[:2]:
                    pkg = await c.get(f"/packages/{p['package_id']}", cache_ttl=0)
                    raw = pkg.get("package", {})
                    print("      raw package:", json.dumps({k: raw.get(k) for k in ("status", "shipment_order", "delivered_date", "shipment_date")}, default=str)[:600])
        await check("get_fulfilment_evidence(reference_number)", lambda: t.get_fulfilment_evidence(reference_number="order_DemoQ7Hk2pXa01"),
                    lambda e: e.salesorder_number == "SO-00101" and e.shipments and e.shipments[0].tracking_number == "2849710036514",
                    lambda e: f"carrier={e.shipments[0].carrier if e.shipments else None} awb={e.shipments[0].tracking_number if e.shipments else None} delivered={e.shipments[0].delivered_date if e.shipments else None}")
    finally:
        s = t.connector_status()
        await rt.aclose()
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed. Zoho calls: {s['stats']['requests']}, daily remaining: {s['budget']['daily_remaining']}")


if __name__ == "__main__":
    asyncio.run(main())
