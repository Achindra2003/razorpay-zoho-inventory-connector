# Zoho Inventory connector for Razorpay Agent Studio

A private, **read-only** connector that gives Agent Studio agents a merchant's stock, orders and proof of delivery from Zoho Inventory. It is an MCP server with OAuth 2.0, list/get/search primitives, three-layer rate limiting and a published tool spec. It runs fully offline in demo mode, so you can try it in two minutes without a Zoho account.

## The merchant problem

> **Saffron Threads** is a D2C apparel and home brand. It takes payments on Razorpay and runs stock and fulfilment in Zoho Inventory, with warehouses in Bengaluru and Delhi NCR. It has switched on Agent Studio.
>
> - The **cart recovery agent** keeps calling shoppers about an olive tee that has been out of stock for a week.
> - The **dispute responder** can't answer a "never received" chargeback. The carrier, AWB and delivery date live in Zoho, and someone on the ops team copies them out by hand.
>
> The ask was *"let the agent read inventory and orders."* The real need is **"let agents check fulfilment facts before they talk to a customer or answer a dispute."**

So on top of the list/get/search primitives, the connector has two tools built for those jobs:

- **`check_availability`**: up to 25 SKUs in one call, optionally for one warehouse. Answers *sellable: yes/no* with a reason.
- **`get_fulfilment_evidence`**: looks up an order by its Razorpay order ID. Returns carrier, AWB, ship and delivery dates, a verdict, a sentence the agent can quote, and the gaps that weaken the evidence.

## Try it in 2 minutes (no Zoho account)

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> && cd zoho-inventory-connector
uv sync
uv run python scripts/demo.py        # narrated walkthrough through a real MCP client session
uv run pytest -q                     # 66 tests, ~10 s, no network
```

`scripts/demo.py` connects through the OAuth code path, then runs the cart and dispute scenarios. It also shows PII masking, a prompt-injection record, recovery from Zoho 429s, and the structured error an agent gets when the budget runs out.

**Use it from an MCP host** (Claude Desktop, Claude Code, MCP Inspector) with fictional demo data:

```bash
uv run zoho-connector serve --demo                                              # stdio
npx -y @modelcontextprotocol/inspector uv run zoho-connector serve --demo       # click through every tool
```

Claude Code picks up the bundled `.mcp.json` automatically. For Claude Desktop, add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "zoho-inventory": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/zoho-inventory-connector", "run", "zoho-connector", "serve", "--demo"]
    }
  }
}
```

Then ask, for example: *"A shopper abandoned ST-TEE-OLV-M and ST-TEE-BLK-M. What can we still pitch?"* or *"Customer disputes Razorpay order order_DemoQ7Hk2pXa01 as not received. What's our evidence?"*

## Connect a real Zoho Inventory org

Everything below is free: Zoho Inventory's free plan includes API access at 1,000 calls/day.

1. **Org.** Sign up at [zoho.com/in/inventory](https://www.zoho.com/in/inventory/) (free plan).
2. **OAuth client.** At [api-console.zoho.in](https://api-console.zoho.in), choose **Add Client → Server-based Applications** and set:
   - Homepage URL: `http://localhost`
   - Authorized Redirect URI: `http://localhost:8765/callback`
3. **Configure.** `cp .env.example .env` and set `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET` and `ZOHO_DC` (`in` for zoho.in accounts).
4. **Connect.**
   ```bash
   uv run zoho-connector connect      # opens Zoho's consent screen (5 read-only scopes) → pick org → tokens go to the OS keyring
   uv run zoho-connector status       # verifies with one API call, shows budget
   ```
   On a server without a browser, create a **Self Client** in the API console, put *its* client ID/secret in `.env`, generate a code with the same read scopes, and run `zoho-connector connect --code 1000.xxxx`.
5. **Demo data (optional).** Seed the same fictional records into your org with `scripts/seed_zoho.py`. It uses a separate, short-lived write grant and revokes it at the end, because the connector itself never gets write access.
   ```bash
   uv run python scripts/seed_zoho.py --dry-run         # see the ~55 requests first
   uv run python scripts/seed_zoho.py --code 1000.xxxx --client-id <self client id> --client-secret <self client secret>
   ```
6. **Serve.** `uv run zoho-connector serve`, or use the Claude Desktop config above without `--demo`. Add `--transport http --port 8000` to serve over streamable HTTP.
7. **Disconnect.** `uv run zoho-connector disconnect` revokes the grant at Zoho and deletes the local tokens.

| Command | What it does |
|---|---|
| `zoho-connector connect [--code X] [--org-id Y] [--no-browser]` | OAuth consent, bind to one organization |
| `zoho-connector status` | Connection, scopes, token expiry, per-minute and daily budget |
| `zoho-connector serve [--demo] [--transport stdio\|http]` | Run the MCP server |
| `zoho-connector disconnect` | Revoke and forget |
| `zoho-connector mock-server` | Mock Zoho on :8900, to try the browser OAuth flow without an account |
| `zoho-connector spec` | Regenerate `docs/tools.json` |

`--connection <name>` (or `ZOHO_CONNECTION`) keeps one named connection per merchant org.

## Tools

The full JSON Schemas, including output schemas, are in **[`docs/tools.json`](docs/tools.json)**. A test keeps that file in sync with the code.

| Tool | Use it when | Zoho calls |
|---|---|---|
| `check_availability` | Before pitching or promising a product. ≤25 SKUs, optional warehouse | 1 per SKU, or 1 catalogue page for ≥6 SKUs |
| `get_fulfilment_evidence` | Disputes, "where is my order". By Razorpay ref, SO number or id | 2 + 1 per package |
| `list_items` / `search_items` / `get_item` | Catalogue, low stock, per-warehouse stock | 1–2 |
| `list_sales_orders` / `search_sales_orders` / `get_sales_order` | Orders by status, date, ref, customer | 1–2 |
| `connector_status` | Which org, scopes, budget left | 0 |

All tools are annotated `readOnlyHint: true, destructiveHint: false`. Pagination uses opaque `next_cursor` tokens bound to the original query. Errors are JSON with a stable `code`, an optional `retry_after_s` and a `hint` telling the agent what to do next.

**[`docs/CAPABILITIES.md`](docs/CAPABILITIES.md)** is the one-page summary of what an agent can and cannot do.

## How it handles the hard parts

- **OAuth.** Authorization-code flow with offline access, a loopback redirect, a `state` check, and validation of Zoho's multi-DC `accounts-server`. Token refresh is single-flight, and a 401 triggers one refresh and retry. Tokens are stored in the OS keyring and revoked on disconnect.
- **Rate limits.** Zoho enforces 100 requests/min, 5–10 concurrent and 1,000–10,000/day per org. The connector:
  - keeps a sliding 90/min window and a concurrency semaphore;
  - persists a daily quota counter;
  - honours `Retry-After` and otherwise backs off with jitter, within a bounded total wait;
  - fails fast with `RATE_LIMITED` or `DAILY_QUOTA_EXHAUSTED` instead of hanging an agent;
  - caches reads (items 60s, orders 30s) to save quota.
- **Safety.**
  - Read-only, enforced by OAuth scope, by the tool surface and by MCP annotations.
  - Customer email, phone and street are masked unless the operator sets `ZOHO_PII_MODE=full`.
  - Merchant free text is returned as `UntrustedText`.
  - Every call writes a JSON audit line with arguments (names redacted), status, latency, Zoho calls spent and quota left.

The reasoning behind these choices is in [`docs/DESIGN.md`](docs/DESIGN.md). How to tell whether the connector is worth deploying is in [`docs/IMPACT.md`](docs/IMPACT.md).

## Tests and evals

```bash
uv run pytest -q                    # 66 tests
uv run python evals/run_eval.py     # LLM eval; needs EVAL_API_KEY (free Groq key works; Anthropic/Ollama via env)
```

The tests cover:
- the full browser OAuth flow on real sockets;
- `state` mismatch and untrusted `accounts-server`;
- expired-token refresh, including concurrent callers sharing one refresh;
- revoked grants;
- 429 with `Retry-After`, the 1070 concurrency error and 5xx retries;
- a 150-call burst throttled client-side with zero upstream 429s;
- the daily quota persisting across restarts and rolling over at IST midnight;
- every fulfilment verdict;
- cursor misuse, PII masking and the prompt-injection record;
- the MCP schema, plus a real stdio subprocess launched the way Claude Desktop launches it.

**Eval result (gpt-oss-120b on Groq, demo data): 12/12 scenarios**, with tool choice 12/12 and grounded answers 12/12. Groq rejected one malformed tool call (`tool_use_failed`); the harness resampled it once and counts that in the report. The scenarios cover cart recovery, disputes, order tracking, inventory, prompt injection and write refusal.

The first run scored 10/12, and the misses were useful:
- **A real product gap.** For "is the mug available in Bengaluru?" the tool returned only "out of stock", so the model couldn't offer Delhi. `check_availability` now returns `other_locations`, and the model answers "not from Bengaluru; Delhi NCR WH has 26".
- **Two harness bugs.** The model wrote `SO‑00104` with a non-breaking hyphen and `can’t` with a curly apostrophe, so the scorer now normalises punctuation. A provider error used to crash the whole run.

On the injection record, the model described the candle and noted that *"the description field contains internal notes; no action is required"*. It listed no customer data and attempted no refund.

Re-run with `make eval` (free Groq key) after changing any tool description. A scripted oracle in the tests shows every scenario is answerable from the data.

## Assumptions

- The Razorpay order or payment ID is stored in the Zoho sales order's **`reference_number`**. This is common for storefront syncs. If a merchant uses a custom field, that becomes a per-connection setting (see DESIGN "next steps").
- One connection = one Zoho organization. Multi-org merchants create one connection each.
- Delivery facts are what the merchant recorded in Zoho. The connector does not query courier APIs.
- The daily quota resets at midnight IST. Zoho resets per org day, and `ZOHO_QUOTA_UTC_OFFSET_MINUTES` adjusts this.

## Limitations

- **Verified against a live Zoho Inventory org** (free plan, India DC) seeded with `scripts/seed_zoho.py`. Run `scripts/verify_live.py` to check yours. That run turned up four differences from Zoho's docs, now handled and covered by tests:
  - contact details live on `contact_person_details`;
  - the ship date is `shipping_date` on package and shipment records;
  - `customer_name` is exact-match, so search uses `customer_name_contains`;
  - the best stock figure is `actual_available_for_sale_stock`.
- **Delivery dates can be missing.** When a shipment is marked delivered through the API (or without a date), Zoho leaves `shipment_delivered_date` empty. The connector reports this as an evidence gap rather than guessing a date.
- **Single-warehouse orgs** (Zoho's free plan) return no per-warehouse stock, so `location_name` checks explain that and point to total stock.
- **Stock can be stale** by up to 60s because of the read cache. Order data can be up to 30s old.
- **Pull only.** No webhooks.
- **Search is limited** to Zoho's exact, contains and starts-with matching.
- **The daily quota counter is per process host.** Two servers on different machines for the same org would each count separately. A shared store such as Redis would fix that in production.
- **The demo mode is a test double**, not a Zoho emulator. It implements only what this connector uses.

## Project layout

```
src/zoho_connector/
  config.py      settings, data centres, read-only scopes, plan quotas
  auth.py        OAuth: consent URL, loopback callback, code exchange, single-flight refresh, revoke
  store.py       token storage: OS keyring / file / memory
  ratelimit.py   sliding window, daily quota, backoff
  client.py      Zoho HTTP client: auth, retries, error mapping, cache
  models.py      agent-facing output models, PII masking, untrusted text
  tools.py       the 9 capabilities (transport-agnostic)
  server.py      MCP adapter, tool descriptions, audit log
  cli.py         connect / status / serve / disconnect / spec / mock-server
  mock/          mock Zoho accounts + Inventory API, fictional dataset
docs/            CAPABILITIES.md · DESIGN.md · IMPACT.md · tools.json
evals/           scenarios.json + run_eval.py
scripts/         demo.py · seed_zoho.py
tests/           66 tests
```

No real customer data, passwords or keys are included. All demo records are fictional, and emails use the reserved `example.com` domain.
