"""Narrated 60-second walkthrough of the connector, no account or LLM needed.

Talks to the real MCP server (demo data) through a real MCP client session, exactly like an
Agent Studio agent would, and prints what the agent would see.

  uv run python scripts/demo.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp import Client  # noqa: E402

from zoho_connector.config import Settings  # noqa: E402
from zoho_connector.server import create_server  # noqa: E402

BOLD, DIM, GREEN, RED, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = DIM = GREEN = RED = YELLOW = RESET = ""


def step(title: str, why: str) -> None:
    print(f"\n{BOLD}== {title}{RESET}\n{DIM}{why}{RESET}")


def call_line(name: str, args: dict) -> None:
    print(f"{YELLOW}> {name}({json.dumps(args)}){RESET}")


async def main() -> None:
    logging.disable(logging.CRITICAL)
    server, holder = create_server(Settings(state_dir=Path(tempfile.mkdtemp())), demo=True)
    async with Client(server) as client:
        async def call(name: str, args: dict) -> dict:
            call_line(name, args)
            res = await client.call_tool(name, args)
            if res.is_error:
                return json.loads(res.content[0].text.split(": ", 1)[1])
            return res.structured_content

        step("1. Connect", "OAuth code flow against the mock Zoho accounts server (consent simulated), read-only scopes.")
        status = await call("connector_status", {})
        print(f"   org: {status['organization']['name']}  | access: {status['access']}  | scopes: {len(status['scopes'])} x *.READ")
        tools = (await client.list_tools()).tools
        print(f"   tools: {', '.join(t.name for t in tools)}")
        print(f"   every tool readOnlyHint={all(t.annotations.read_only_hint for t in tools)}; no create/update/delete tool exists")

        step("2. Cart recovery: what can the agent still pitch?",
             "Shopper left 2x black tee, 1x olive tee and a terracotta mug (ships from Bengaluru) in the cart.")
        r = await call("check_availability", {"lines": [{"sku": "ST-TEE-BLK-M", "quantity": 2}, {"sku": "ST-TEE-OLV-M"}, {"sku": "ST-MUG-TERRA"}],
                                              "location_name": "Bengaluru WH"})
        for x in r["results"]:
            mark = f"{GREEN}pitch{RESET}" if x["sellable"] else f"{RED}skip {RESET}"
            print(f"   {mark}  {x['sku']:<14} {x['reason']}")

        step("3. Dispute: customer says 'never received' on a Razorpay order",
             "The agent looks the order up by the Razorpay order ID stored as Zoho's reference number.")
        e = await call("get_fulfilment_evidence", {"reference_number": "order_DemoQ7Hk2pXa01"})
        print(f"   verdict: {GREEN}{e['verdict']}{RESET}\n   summary: {e['summary']}\n   gaps: {e['evidence_gaps'] or 'none'}")
        e = await call("get_fulfilment_evidence", {"salesorder_number": "SO-00103"})
        print(f"   verdict: {YELLOW}{e['verdict']}{RESET}\n   summary: {e['summary']}\n   gaps: {e['evidence_gaps']}")

        step("4. Guardrails: PII and prompt injection",
             "Customer contact is masked unless the operator opts in; merchant free text is labelled untrusted.")
        so = await call("get_sales_order", {"salesorder_number": "SO-00102"})
        print(f"   customer: {so['customer']['name']}  email: {so['customer']['email']}  phone: {so['customer']['phone']}  street: {so['shipping_address']['street']}")
        item = await call("get_item", {"sku": "ST-CANDLE-SANDAL"})
        print(f"   description: \"{item['description']['text'][:70]}...\"\n   note: {item['description']['note']}")

        step("5. Rate limits", "Zoho throttles (429 + Retry-After) twice; the connector backs off and the agent never notices.")
        runtime = await holder.get()
        runtime.mock.fail_next = [429, 429]
        r = await call("search_items", {"query": "kurta"})
        print(f"   {r['returned']} kurtas returned after {runtime.client.stats['retries']} automatic retries")
        print(f"   {DIM}Burst: per-minute budget is {runtime.client.limiter.limit}; when it is spent the agent gets a structured error:{RESET}")
        runtime.client.limiter._stamps.extend([runtime.client.limiter.clock()] * runtime.client.limiter.limit)
        err = await call("list_sales_orders", {"status": "open", "page_size": 5})
        print(f"   {RED}{err['error']['code']}{RESET} retry_after_s={err['error']['retry_after_s']}  hint: {err['error']['hint']}")

        step("6. Budget", "connector_status costs zero Zoho calls and tells the agent how much quota is left today.")
        s = await call("connector_status", {})
        b = s["budget"]
        print(f"   daily: {b['daily_used']} used / {b['daily_limit']}  | cache hits: {s['cache']['hits']}  | upstream 429s absorbed: {s['stats']['upstream_429']}")
    print(f"\n{BOLD}Done.{RESET} Same server, real Zoho: `zoho-connector connect` then `zoho-connector serve`.\n")


if __name__ == "__main__":
    asyncio.run(main())
