"""The connector's capabilities, independent of MCP.

Every public coroutine here is one agent tool. server.py only adapts them to MCP, so the same
logic could be exposed through any other agent tool protocol.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .client import ZohoClient
from .errors import ConnectorError, ErrorCode
from .models import (
    Availability,
    AvailabilityReport,
    FulfilmentEvidence,
    ItemDetail,
    ItemSummary,
    Page,
    SalesOrderDetail,
    SalesOrderSummary,
    ShipmentEvidence,
    ShippedItem,
    address,
    item_detail,
    item_summary,
    salesorder_detail,
    salesorder_summary,
)

log = logging.getLogger(__name__)

# Cache TTLs per resource (seconds). Stock moves faster than catalogue metadata, and order state
# matters most right before an agent acts, so orders get the shortest TTL.
TTL_ITEMS = 60.0
TTL_ORDERS = 30.0
TTL_PACKAGES = 60.0

MAX_PAGE = 100
CATALOG_PREFETCH_THRESHOLD = 6  # above this many SKUs, one catalogue page beats N lookups
CATALOG_PREFETCH_PAGES = 2

ItemStatus = Literal["active", "inactive", "all"]
OrderStatus = Literal["all", "draft", "open", "confirmed", "fulfilled", "closed", "void"]
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AvailabilityLine(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: float = Field(1, gt=0, le=10000)


# ---------- cursors ----------

def _fingerprint(filters: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(filters, sort_keys=True, default=str).encode()).hexdigest()[:10]


def encode_cursor(page: int, filters: dict[str, Any]) -> str:
    raw = json.dumps({"p": page, "f": _fingerprint(filters)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, filters: dict[str, Any]) -> int:
    if not cursor:
        return 1
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        page = int(data["p"])
    except Exception:
        raise ConnectorError(ErrorCode.INVALID_INPUT, "cursor is malformed; omit it to start from the first page")
    if data.get("f") != _fingerprint(filters):
        raise ConnectorError(ErrorCode.INVALID_INPUT, "cursor belongs to a different query; omit it or repeat the original filters")
    return max(1, page)


def _check_date(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not _DATE.match(value):
        raise ConnectorError(ErrorCode.INVALID_INPUT, f"{name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ConnectorError(ErrorCode.INVALID_INPUT, f"{name} is not a real date")
    return value


def _page_size(n: int) -> int:
    if not 1 <= n <= MAX_PAGE:
        raise ConnectorError(ErrorCode.INVALID_INPUT, f"page_size must be between 1 and {MAX_PAGE}")
    return n


def _exactly_one(**kwargs: Any) -> tuple[str, str]:
    given = [(k, v.strip()) for k, v in kwargs.items() if v is not None and str(v).strip()]
    if len(given) != 1:
        raise ConnectorError(ErrorCode.INVALID_INPUT, f"Provide exactly one of: {', '.join(kwargs)}")
    return given[0]


def _has_more(data: dict[str, Any]) -> bool:
    return bool((data.get("page_context") or {}).get("has_more_page"))


class InventoryTools:
    def __init__(self, client: ZohoClient, *, pii_full: bool = False) -> None:
        self.client = client
        self.pii_full = pii_full

    @property
    def currency(self) -> str:
        return self.client.tokens.connection.currency_code or "INR"

    # ----- items -----

    async def list_items(
        self,
        status: ItemStatus = "active",
        low_stock_only: bool = False,
        sort_by: Literal["name", "sku", "stock_on_hand", "last_modified_time"] = "name",
        page_size: int = 25,
        cursor: str | None = None,
    ) -> Page[ItemSummary]:
        filters = {"status": status, "low_stock_only": low_stock_only, "sort_by": sort_by, "page_size": page_size}
        page = decode_cursor(cursor, filters)
        filter_by = "Status.Lowstock" if low_stock_only else {"active": "Status.Active", "inactive": "Status.Inactive", "all": "Status.All"}[status]
        data = await self.client.get(
            "/items",
            {"filter_by": filter_by, "sort_column": sort_by, "page": page, "per_page": _page_size(page_size)},
            cache_ttl=TTL_ITEMS,
        )
        items = [item_summary(i, self.currency) for i in data.get("items", [])]
        if low_stock_only and status != "all":
            items = [i for i in items if i.status == status]
        more = _has_more(data)
        return Page[ItemSummary](results=items, returned=len(items), has_more=more, next_cursor=encode_cursor(page + 1, filters) if more else None)

    async def _item_by_sku(self, sku: str) -> dict[str, Any] | None:
        data = await self.client.get("/items", {"sku": sku, "filter_by": "Status.All"}, cache_ttl=TTL_ITEMS)
        matches = [i for i in data.get("items", []) if (i.get("sku") or "").lower() == sku.lower()]
        return matches[0] if matches else None

    async def get_item(self, item_id: str | None = None, sku: str | None = None) -> ItemDetail:
        key, value = _exactly_one(item_id=item_id, sku=sku)
        if key == "sku":
            found = await self._item_by_sku(value)
            if not found:
                raise ConnectorError(ErrorCode.NOT_FOUND, f"No item with SKU {value!r}")
            value = str(found["item_id"])
        data = await self.client.get(f"/items/{value}", cache_ttl=TTL_ITEMS)
        return item_detail(data.get("item") or {}, self.currency)

    async def search_items(
        self,
        query: str,
        field: Literal["any", "name", "sku"] = "any",
        match: Literal["contains", "startswith"] = "contains",
        page_size: int = 25,
        cursor: str | None = None,
    ) -> Page[ItemSummary]:
        query = query.strip()
        if not 2 <= len(query) <= 100:
            raise ConnectorError(ErrorCode.INVALID_INPUT, "query must be 2-100 characters")
        filters = {"query": query, "field": field, "match": match, "page_size": page_size}
        page = decode_cursor(cursor, filters)
        params: dict[str, Any] = {"filter_by": "Status.All", "page": page, "per_page": _page_size(page_size)}
        if field == "any":
            params["search_text"] = query
        else:
            params[f"{field}_{match}"] = query
        data = await self.client.get("/items", params, cache_ttl=TTL_ITEMS)
        items = [item_summary(i, self.currency) for i in data.get("items", [])]
        more = _has_more(data)
        return Page[ItemSummary](results=items, returned=len(items), has_more=more, next_cursor=encode_cursor(page + 1, filters) if more else None)

    async def check_availability(self, lines: list[AvailabilityLine], location_name: str | None = None) -> AvailabilityReport:
        if not 1 <= len(lines) <= 25:
            raise ConnectorError(ErrorCode.INVALID_INPUT, "lines must contain 1-25 SKUs")
        wanted: dict[str, float] = {}
        for line in lines:  # merge duplicate SKUs
            key = line.sku.strip()
            wanted[key] = wanted.get(key, 0) + line.quantity

        found: dict[str, dict[str, Any]] = {}
        if len(wanted) >= CATALOG_PREFETCH_THRESHOLD and not location_name:
            # One or two catalogue pages usually cover a small merchant's whole catalogue
            # and cost far less quota than one lookup per SKU.
            for page in range(1, CATALOG_PREFETCH_PAGES + 1):
                data = await self.client.get("/items", {"filter_by": "Status.All", "page": page, "per_page": 200}, cache_ttl=TTL_ITEMS)
                for raw in data.get("items", []):
                    if raw.get("sku"):
                        found.setdefault(raw["sku"].lower(), raw)
                if not _has_more(data) or all(s.lower() in found for s in wanted):
                    break

        async def lookup(sku: str) -> tuple[str, dict[str, Any] | None]:
            if sku.lower() in found:
                return sku, found[sku.lower()]
            return sku, await self._item_by_sku(sku)

        resolved = dict(await asyncio.gather(*(lookup(s) for s in wanted)))

        details: dict[str, dict[str, Any]] = {}
        if location_name:
            ids = {s: str(r["item_id"]) for s, r in resolved.items() if r}
            fetched = await asyncio.gather(*(self.client.get(f"/items/{i}", cache_ttl=TTL_ITEMS) for i in ids.values()))
            details = {s: d.get("item") or {} for s, d in zip(ids, fetched)}

        results: list[Availability] = []
        for sku, qty in wanted.items():
            raw = resolved.get(sku)
            if not raw:
                results.append(Availability(sku=sku, requested_qty=qty, found=False, sellable=False, reason="SKU not found in Zoho Inventory"))
                continue
            summary = item_summary(raw, self.currency)
            avail = summary.available_for_sale
            loc_label = None
            alternatives: list = []
            if location_name:
                det = item_detail(details.get(sku) or raw, self.currency)
                loc = next((l for l in det.locations if l.name.lower() == location_name.strip().lower()), None)
                if loc is None:
                    if det.locations:
                        reason = f"Location {location_name!r} not found (known: {', '.join(l.name for l in det.locations)})"
                    else:
                        reason = "This organization does not track stock per warehouse; call again without location_name for total stock"
                    results.append(Availability(sku=sku, requested_qty=qty, found=True, item_id=summary.item_id, name=summary.name, sellable=False, reason=reason))
                    continue
                avail, loc_label = loc.available_for_sale, loc.name
                alternatives = [l for l in det.locations if l is not loc and (l.available_for_sale or 0) >= qty]
            if summary.status != "active":
                ok, reason = False, f"Item is {summary.status}"
            elif avail is None:
                ok, reason = False, "Stock level is not tracked for this item"
            elif avail >= qty:
                ok, reason = True, f"{avail:g} available for sale"
            elif avail > 0:
                ok, reason = False, f"Only {avail:g} available, {qty:g} requested"
            else:
                ok, reason = False, "Out of stock"
            others = None
            if location_name and not ok and summary.status == "active" and alternatives:
                others = alternatives
                reason += f" at {loc_label}; can ship from " + ", ".join(f"{l.name} ({l.available_for_sale:g} available)" for l in alternatives)
            results.append(Availability(sku=sku, requested_qty=qty, found=True, item_id=summary.item_id, name=summary.name, location=loc_label, available_for_sale=avail, sellable=ok, reason=reason, other_locations=others))

        return AvailabilityReport(
            all_sellable=all(r.sellable for r in results),
            results=results,
            as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    # ----- sales orders -----

    def _order_status_param(self, status: OrderStatus) -> str:
        return "Status." + {"all": "All", "draft": "Draft", "open": "Open", "confirmed": "Confirmed", "fulfilled": "Fulfilled", "closed": "Closed", "void": "Void"}[status]

    async def list_sales_orders(
        self,
        status: OrderStatus = "all",
        date_from: str | None = None,
        date_to: str | None = None,
        page_size: int = 25,
        cursor: str | None = None,
    ) -> Page[SalesOrderSummary]:
        filters = {"status": status, "date_from": date_from, "date_to": date_to, "page_size": page_size}
        page = decode_cursor(cursor, filters)
        data = await self.client.get(
            "/salesorders",
            {
                "filter_by": self._order_status_param(status),
                "date_start": _check_date("date_from", date_from),
                "date_end": _check_date("date_to", date_to),
                "sort_column": "date",
                "sort_order": "D",
                "page": page,
                "per_page": _page_size(page_size),
            },
            cache_ttl=TTL_ORDERS,
        )
        orders = [salesorder_summary(o) for o in data.get("salesorders", [])]
        more = _has_more(data)
        return Page[SalesOrderSummary](results=orders, returned=len(orders), has_more=more, next_cursor=encode_cursor(page + 1, filters) if more else None)

    async def search_sales_orders(
        self,
        reference_number: str | None = None,
        salesorder_number: str | None = None,
        customer_name: str | None = None,
        text: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page_size: int = 25,
        cursor: str | None = None,
    ) -> Page[SalesOrderSummary]:
        # Zoho's `customer_name` is an exact match; `customer_name_contains` matches part of a name (verified on a live org).
        criteria = {"reference_number": reference_number, "salesorder_number": salesorder_number, "customer_name_contains": customer_name, "search_text": text}
        criteria = {k: v.strip() for k, v in criteria.items() if v and v.strip()}
        if not criteria:
            raise ConnectorError(ErrorCode.INVALID_INPUT, "Give at least one of reference_number, salesorder_number, customer_name, text")
        filters = {**criteria, "date_from": date_from, "date_to": date_to, "page_size": page_size}
        page = decode_cursor(cursor, filters)
        params: dict[str, Any] = {
            **criteria,
            "date_start": _check_date("date_from", date_from),
            "date_end": _check_date("date_to", date_to),
            "sort_column": "date",
            "sort_order": "D",
            "page": page,
            "per_page": _page_size(page_size),
        }
        data = await self.client.get("/salesorders", params, cache_ttl=TTL_ORDERS)
        orders = [salesorder_summary(o) for o in data.get("salesorders", [])]
        more = _has_more(data)
        return Page[SalesOrderSummary](results=orders, returned=len(orders), has_more=more, next_cursor=encode_cursor(page + 1, filters) if more else None)

    async def _resolve_order_id(self, salesorder_id: str | None, salesorder_number: str | None, reference_number: str | None) -> str:
        key, value = _exactly_one(salesorder_id=salesorder_id, salesorder_number=salesorder_number, reference_number=reference_number)
        if key == "salesorder_id":
            return value
        data = await self.client.get("/salesorders", {key: value, "filter_by": "Status.All"}, cache_ttl=TTL_ORDERS)
        matches = [o for o in data.get("salesorders", []) if (o.get(key) or "").lower() == value.lower()]
        if not matches:
            raise ConnectorError(ErrorCode.NOT_FOUND, f"No sales order with {key} {value!r}")
        if len(matches) > 1:
            raise ConnectorError(
                ErrorCode.INVALID_INPUT,
                f"{len(matches)} sales orders share {key} {value!r}; call again with one salesorder_id",
                details={"candidates": [salesorder_summary(o).model_dump() for o in matches[:10]]},
            )
        return str(matches[0]["salesorder_id"])

    async def _order_raw(self, order_id: str) -> dict[str, Any]:
        data = await self.client.get(f"/salesorders/{order_id}", cache_ttl=TTL_ORDERS)
        return data.get("salesorder") or {}

    async def get_sales_order(self, salesorder_id: str | None = None, salesorder_number: str | None = None, reference_number: str | None = None) -> SalesOrderDetail:
        order_id = await self._resolve_order_id(salesorder_id, salesorder_number, reference_number)
        return salesorder_detail(await self._order_raw(order_id), self.pii_full)

    async def get_fulfilment_evidence(self, salesorder_id: str | None = None, salesorder_number: str | None = None, reference_number: str | None = None) -> FulfilmentEvidence:
        order_id = await self._resolve_order_id(salesorder_id, salesorder_number, reference_number)
        raw = await self._order_raw(order_id)
        order = salesorder_detail(raw, self.pii_full)
        package_ids = [p.package_id for p in order.packages if p.package_id]
        packages = await asyncio.gather(*(self.client.get(f"/packages/{pid}", cache_ttl=TTL_PACKAGES) for pid in package_ids))

        shipments: list[ShipmentEvidence] = []
        gaps: list[str] = []
        qty_shipped = qty_delivered = 0.0
        for pdata in packages:
            pkg = pdata.get("package") or {}
            ship = pkg.get("shipment_order") or {}
            status = (pkg.get("status") or ship.get("status") or "").lower() or None
            items = [ShippedItem(sku=li.get("sku") or None, name=li.get("name", ""), quantity=float(li.get("quantity") or 0)) for li in pkg.get("line_items") or []]
            qty = sum(i.quantity for i in items)
            # Live orgs: package/shipment use `shipping_date` and `shipment_delivered_date`; the order's
            # package summary uses `shipment_date` / `delivery_date`. Accept all spellings.
            delivered_date = (ship.get("shipment_delivered_date") or pkg.get("shipment_delivered_date")
                              or ship.get("delivery_date") or ship.get("delivered_date") or pkg.get("delivered_date")) or None
            shipped_on = (ship.get("shipping_date") or ship.get("shipment_date")
                          or pkg.get("shipping_date") or pkg.get("shipment_date")) or None
            ev = ShipmentEvidence(
                package_number=pkg.get("package_number"),
                shipment_number=ship.get("shipment_number") or pkg.get("shipment_number"),
                status=status,
                carrier=ship.get("carrier") or ship.get("delivery_method") or pkg.get("carrier"),
                tracking_number=ship.get("tracking_number") or pkg.get("tracking_number"),
                shipment_date=shipped_on,
                delivered_date=delivered_date,
                ship_to=address(pkg.get("shipping_address") or raw.get("shipping_address"), self.pii_full),
                items=items,
            )
            shipments.append(ev)
            label = ev.package_number or "a package"
            if status in ("shipped", "delivered"):
                qty_shipped += qty
                if not ev.tracking_number:
                    gaps.append(f"{label} has no tracking number")
                if not ev.carrier:
                    gaps.append(f"{label} has no carrier recorded")
            if status == "delivered":
                qty_delivered += qty
                if not delivered_date:
                    gaps.append(f"{label} is marked delivered but has no delivery date")
            elif status == "shipped":
                gaps.append(f"{label} is shipped but not marked delivered in Zoho")

        qty_ordered = sum(li.quantity for li in order.line_items)
        cancelled = (order.status or "").lower() == "void"
        if cancelled:
            pass  # nothing should ship for a voided order, so missing shipments are not a gap
        elif not shipments:
            gaps.append("No packages or shipments exist for this order")
        elif qty_shipped < qty_ordered:
            gaps.append(f"{qty_ordered - qty_shipped:g} of {qty_ordered:g} units were never shipped")

        if cancelled:
            verdict = "CANCELLED"
        elif qty_ordered and qty_delivered >= qty_ordered:
            verdict = "DELIVERED"
        elif qty_delivered > 0:
            verdict = "PARTIALLY_DELIVERED"
        elif qty_shipped > 0:
            verdict = "SHIPPED_NOT_DELIVERED"
        else:
            verdict = "NOT_SHIPPED"

        ref = f" (ref {order.reference_number})" if order.reference_number else ""
        delivered = [s for s in shipments if s.status == "delivered"]
        if verdict in ("DELIVERED", "PARTIALLY_DELIVERED"):
            last = max(delivered, key=lambda s: s.delivered_date or "")
            via = ", ".join(f"{s.carrier or 'unknown carrier'} AWB {s.tracking_number or 'n/a'}" for s in delivered)
            summary = f"Order {order.salesorder_number}{ref} dated {order.date}: {qty_delivered:g} of {qty_ordered:g} units delivered" + (f", last on {last.delivered_date}" if last.delivered_date else "") + f" via {via}."
        elif verdict == "SHIPPED_NOT_DELIVERED":
            via = ", ".join(f"{s.carrier or 'unknown carrier'} AWB {s.tracking_number or 'n/a'} on {s.shipment_date or 'unknown date'}" for s in shipments if s.status == "shipped")
            summary = f"Order {order.salesorder_number}{ref} dated {order.date}: {qty_shipped:g} of {qty_ordered:g} units shipped ({via}); no delivery confirmation recorded."
        elif verdict == "CANCELLED":
            summary = f"Order {order.salesorder_number}{ref} was voided in Zoho Inventory."
        else:
            summary = f"Order {order.salesorder_number}{ref} dated {order.date} has not been shipped."

        return FulfilmentEvidence(
            salesorder_id=order.salesorder_id,
            salesorder_number=order.salesorder_number,
            reference_number=order.reference_number,
            order_date=order.date,
            order_status=order.status,
            customer_name=order.customer_name,
            order_total=order.total,
            verdict=verdict,
            quantity_ordered=qty_ordered,
            quantity_shipped=qty_shipped,
            quantity_delivered=qty_delivered,
            shipments=shipments,
            evidence_gaps=gaps,
            summary=summary,
        )

    # ----- status -----

    def connector_status(self) -> dict[str, Any]:
        c = self.client
        conn = c.tokens.connection
        return {
            "connected": True,
            "connection": conn.name,
            "organization": {"id": conn.organization_id, "name": conn.organization_name, "currency": conn.currency_code},
            "data_centre": conn.dc,
            "api_domain": conn.api_domain,
            "scopes": conn.scopes.split(",") if conn.scopes else [],
            "access": "read-only",
            "access_token_valid_for_s": max(0, int(conn.access_expires_at - time.time())),
            "token_store": c.tokens.store.description,
            "budget": {
                "per_minute_limit": c.limiter.limit,
                "per_minute_remaining": c.limiter.remaining,
                "daily_limit": c.quota.limit,
                "daily_used": c.quota.used,
                "daily_remaining": c.quota.remaining,
                "daily_resets_at": c.quota.resets_at(),
                "max_concurrency": c.settings.max_concurrency,
            },
            "cache": {"ttl_s": c.cache.ttl, "hits": c.cache.hits, "misses": c.cache.misses},
            "stats": dict(c.stats, throttled_waits=c.limiter.throttled_waits, token_refreshes=c.tokens.refresh_count),
            "pii_mode": "full" if self.pii_full else "masked",
        }
