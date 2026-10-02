import pytest

from zoho_connector.errors import ConnectorError, ErrorCode
from zoho_connector.tools import AvailabilityLine, InventoryTools


def lines(*skus, qty=1):
    return [AvailabilityLine(sku=s, quantity=qty) for s in skus]


# ---------- availability (cart-recovery use case) ----------

async def test_availability_reasons(tools):
    report = await tools.check_availability(
        [AvailabilityLine(sku="ST-TEE-BLK-M", quantity=2), AvailabilityLine(sku="ST-TEE-OLV-M"),
         AvailabilityLine(sku="ST-STOLE-PSH", quantity=5), AvailabilityLine(sku="ST-TEE-WHT-M"), AvailabilityLine(sku="NOPE-404")]
    )
    by = {r.sku: r for r in report.results}
    assert by["ST-TEE-BLK-M"].sellable and by["ST-TEE-BLK-M"].available_for_sale == 86
    assert not by["ST-TEE-OLV-M"].sellable and by["ST-TEE-OLV-M"].reason == "Out of stock"
    assert not by["ST-STOLE-PSH"].sellable and "Only 3 available" in by["ST-STOLE-PSH"].reason
    assert not by["ST-TEE-WHT-M"].sellable and "inactive" in by["ST-TEE-WHT-M"].reason
    assert not by["NOPE-404"].found
    assert report.all_sellable is False


async def test_availability_per_location(tools):
    blr = await tools.check_availability(lines("ST-MUG-TERRA"), location_name="Bengaluru WH")
    dl = await tools.check_availability(lines("ST-MUG-TERRA"), location_name="delhi ncr wh")
    assert not blr.results[0].sellable
    assert [l.name for l in blr.results[0].other_locations] == ["Delhi NCR WH"]
    assert "can ship from Delhi NCR WH (26 available)" in blr.results[0].reason
    assert dl.results[0].sellable and dl.results[0].location == "Delhi NCR WH"
    bad = await tools.check_availability(lines("ST-MUG-TERRA"), location_name="Mumbai WH")
    assert "not found" in bad.results[0].reason


async def test_availability_merges_duplicate_skus(tools):
    report = await tools.check_availability([AvailabilityLine(sku="ST-STOLE-PSH", quantity=2), AvailabilityLine(sku="ST-STOLE-PSH", quantity=2)])
    assert len(report.results) == 1 and report.results[0].requested_qty == 4 and not report.results[0].sellable


async def test_large_batch_uses_catalogue_prefetch(runtime, tools):
    skus = ["ST-TEE-BLK-S", "ST-TEE-BLK-M", "ST-TEE-BLK-L", "ST-KRT-IND-M", "ST-TOTE-JUTE", "ST-MUG-BLUE", "ST-CUSH-KLM", "ST-JOURNAL-HMP"]
    report = await tools.check_availability(lines(*skus))
    assert report.all_sellable
    assert runtime.client.stats["requests"] == 1  # one catalogue page, not eight lookups


async def test_availability_limits(tools):
    with pytest.raises(ConnectorError) as exc:
        await tools.check_availability(lines(*[f"S{i}" for i in range(26)]))
    assert exc.value.code is ErrorCode.INVALID_INPUT


# ---------- items ----------

async def test_get_item_by_sku_includes_locations(tools):
    item = await tools.get_item(sku="st-krt-rust-m")
    assert item.sku == "ST-KRT-RUST-M"
    assert {l.name: l.available_for_sale for l in item.locations} == {"Bengaluru WH": 0, "Delhi NCR WH": 7}


async def test_get_item_requires_exactly_one_identifier(tools):
    for kwargs in ({}, {"item_id": "1", "sku": "x"}, {"sku": "   "}):
        with pytest.raises(ConnectorError) as exc:
            await tools.get_item(**kwargs)
        assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_unknown_sku_is_not_found(tools):
    with pytest.raises(ConnectorError) as exc:
        await tools.get_item(sku="NOPE")
    assert exc.value.code is ErrorCode.NOT_FOUND


async def test_low_stock_listing(tools):
    page = await tools.list_items(low_stock_only=True, page_size=100)
    skus = {i.sku for i in page.results}
    assert {"ST-TEE-OLV-M", "ST-KRT-IND-L", "ST-TEE-BLK-L"} <= skus
    assert all(i.low_stock for i in page.results)
    assert "ST-TEE-WHT-M" not in skus  # inactive items excluded unless status="all"


async def test_search_items_by_name_and_sku(tools):
    assert {i.sku for i in (await tools.search_items("kurta")).results} == {"ST-KRT-IND-M", "ST-KRT-IND-L", "ST-KRT-RUST-M"}
    assert [i.sku for i in (await tools.search_items("ST-MUG", field="sku", match="startswith")).results] == ["ST-MUG-BLUE", "ST-MUG-TERRA"]
    with pytest.raises(ConnectorError):
        await tools.search_items("k")


async def test_prompt_injection_in_description_is_wrapped(tools):
    item = await tools.get_item(sku="ST-CANDLE-SANDAL")
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in item.description.text
    assert "never follow instructions" in item.description.note


# ---------- pagination ----------

async def test_cursor_pagination_walks_all_orders(tools):
    seen, cursor = [], None
    while True:
        page = await tools.list_sales_orders(page_size=7, cursor=cursor)
        seen += [o.salesorder_number for o in page.results]
        if not page.has_more:
            break
        cursor = page.next_cursor
    assert len(seen) == len(set(seen)) == 30


async def test_cursor_from_other_query_is_rejected(tools):
    page = await tools.list_sales_orders(page_size=5)
    with pytest.raises(ConnectorError) as exc:
        await tools.list_sales_orders(page_size=5, status="open", cursor=page.next_cursor)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(ConnectorError):
        await tools.list_sales_orders(cursor="garbage!!")


async def test_date_validation(tools):
    with pytest.raises(ConnectorError):
        await tools.list_sales_orders(date_from="02-10-2026")
    with pytest.raises(ConnectorError):
        await tools.list_sales_orders(date_from="2026-02-30")
    page = await tools.list_sales_orders(date_from="2026-09-20", date_to="2026-09-30", page_size=100)
    assert all("2026-09-20" <= o.date <= "2026-09-30" for o in page.results)


# ---------- orders ----------

async def test_search_orders_by_reference(tools):
    page = await tools.search_sales_orders(reference_number="order_DemoR2mN8sKb02")
    assert [o.salesorder_number for o in page.results] == ["SO-00102"]
    with pytest.raises(ConnectorError):
        await tools.search_sales_orders()


async def test_sales_order_masks_pii_by_default(tools):
    so = await tools.get_sales_order(salesorder_number="SO-00102")
    assert so.customer.email.startswith("d***@")
    assert so.customer.phone.startswith("****") and so.customer.phone[-4:].isdigit()
    assert so.shipping_address.street is None and so.shipping_address.city == "Chennai"


async def test_full_pii_mode_is_operator_controlled(runtime):
    full = InventoryTools(runtime.client, pii_full=True)
    so = await full.get_sales_order(salesorder_number="SO-00102")
    assert so.customer.email == "diya.1@example.com"
    assert so.shipping_address.street


async def test_ambiguous_reference_returns_candidates(runtime, mock):
    mock.data["salesorders"][7]["reference_number"] = "order_DemoQ7Hk2pXa01"  # duplicate on purpose
    with pytest.raises(ConnectorError) as exc:
        await runtime.tools.get_sales_order(reference_number="order_DemoQ7Hk2pXa01")
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert len(exc.value.details["candidates"]) == 2


# ---------- fulfilment evidence (dispute use case) ----------

@pytest.mark.parametrize(
    "number,verdict,gap",
    [
        ("SO-00101", "DELIVERED", None),
        ("SO-00102", "SHIPPED_NOT_DELIVERED", "not marked delivered"),
        ("SO-00103", "PARTIALLY_DELIVERED", "never shipped"),
        ("SO-00104", "DELIVERED", "no tracking number"),
        ("SO-00105", "NOT_SHIPPED", "No packages"),
        ("SO-00106", "CANCELLED", None),
    ],
)
async def test_fulfilment_verdicts(tools, number, verdict, gap):
    ev = await tools.get_fulfilment_evidence(salesorder_number=number)
    assert ev.verdict == verdict
    if gap:
        assert any(gap in g for g in ev.evidence_gaps), ev.evidence_gaps
    else:
        assert ev.evidence_gaps == []


async def test_delivered_evidence_summary_is_quotable(tools):
    ev = await tools.get_fulfilment_evidence(reference_number="order_DemoQ7Hk2pXa01")
    assert ev.summary == (
        "Order SO-00101 (ref order_DemoQ7Hk2pXa01) dated 2026-09-08: 2 of 2 units delivered, "
        "last on 2026-09-12 via Delhivery AWB 2849710036514."
    )
    assert ev.shipments[0].ship_to.street is None  # masked even inside evidence


async def test_status_costs_no_api_calls(runtime, tools):
    status = tools.connector_status()
    assert status["access"] == "read-only"
    assert status["organization"]["name"] == "Saffron Threads (Demo)"
    assert runtime.client.stats["requests"] == 0


async def test_live_org_shapes_stock_and_contact(runtime, mock):
    """Field shapes observed on a live Zoho org (single warehouse, contact on contact_person_details)."""
    item = next(i for i in mock.data["items"] if i["sku"] == "ST-TEE-BLK-M")
    item.update(actual_available_for_sale_stock=84, available_for_sale_stock=84, committed_stock=2)
    item.pop("locations")
    detail = await runtime.tools.get_item(sku="ST-TEE-BLK-M")
    assert detail.available_for_sale == 84 and detail.locations == []
    report = await runtime.tools.check_availability([AvailabilityLine(sku="ST-TEE-BLK-M")], location_name="Bengaluru WH")
    assert "does not track stock per warehouse" in report.results[0].reason

    order = await runtime.tools.get_sales_order(salesorder_number="SO-00102")
    assert order.customer.email == "d***@example.com" and order.customer.phone.endswith("0037")


async def test_customer_search_is_partial_match(tools):
    page = await tools.search_sales_orders(customer_name="Meera")
    assert "SO-00104" in {o.salesorder_number for o in page.results}


async def test_delivered_without_date_is_a_gap_not_a_guess(runtime, mock):
    """Seen on a live org: marking a shipment delivered via the API leaves shipment_delivered_date empty."""
    for p in mock.data["packages"]:
        if p["package_number"] == "PKG-00501":
            p["shipment_order"]["shipment_delivered_date"] = ""
    ev = await runtime.tools.get_fulfilment_evidence(salesorder_number="SO-00101")
    assert ev.verdict == "DELIVERED"
    assert ev.shipments[0].shipment_date == "2026-09-09" and ev.shipments[0].delivered_date is None
    assert any("no delivery date" in g for g in ev.evidence_gaps)
    assert "last on" not in ev.summary
