# Design notes

## Starting from the merchant problem

The request was "let an agent read inventory/orders from Zoho". A plain CRUD-to-MCP mapping would satisfy that request without helping anyone. So the starting question was: what does an Agent Studio agent get wrong today because it can't see Zoho?

- **Cart recovery agents pitch products that can't ship.** A recovery call for an out-of-stock SKU costs money and the customer's trust. → `check_availability`: a batch call that answers yes/no with a reason, optionally per warehouse.
- **Dispute responders need proof of fulfilment.** In practice someone collects the carrier, AWB and delivery date from Zoho by hand. → `get_fulfilment_evidence`: a single lookup by the Razorpay order ID. It returns a verdict, a sentence the agent can quote, and an explicit list of gaps, so the agent never overclaims delivery.
- **Everything else** (list/get/search on items and orders) covers ad-hoc questions and gives the agent a way to recover when an identifier is partial.

## Tool surface

- **9 tools, not 30.** Each tool is something a merchant would actually ask about, not a mirror of an endpoint. Fewer tools also makes it easier for the model to choose the right one, and the eval harness checks that choice.
- **Composite tools where the model would otherwise loop.** `check_availability` replaces N `get_item` calls. `get_fulfilment_evidence` replaces 1 order lookup + 1 order fetch + N package fetches.
- **Exactly-one identifiers** (`item_id | sku`, `salesorder_id | salesorder_number | reference_number`). If a reference number matches more than one order, the tool returns the candidates instead of guessing.
- **Opaque cursors bound to their query.** If a cursor is reused with different filters, the tool rejects it rather than silently returning the wrong page.
- **Normalised, compact output.** Money is `{amount, currency}`. Stock is `available_for_sale`, which hides Zoho's three stock fields and its warehouses/locations naming split. Ids are strings.
- **Structured output.** Each tool declares a JSON output schema (from Pydantic), so hosts that support `structuredContent` don't need to parse text.

## Auth

- **OAuth 2.0 authorization code with `access_type=offline`**, plus a loopback redirect (`http://localhost:8765/callback`) for the merchant's consent. `prompt=consent` makes the read-only scopes visible every time.
- **Multiple data centres.** Zoho returns `accounts-server` on the redirect. It is accepted only if it is a known Zoho accounts host, so the client secret can't be sent to a host injected into the redirect. API calls go to the `api_domain` returned by the token response.
- **CSRF protection** through a random `state` value. The `code` is never logged.
- **Single-flight refresh.** Concurrent tool calls that find an expired token trigger exactly one refresh (tested). A 401 mid-call refreshes once and retries. A failed refresh becomes `AUTH_EXPIRED` ("ask the merchant to reconnect").
- **Token storage.** Tokens live in the OS keyring (Windows Credential Manager / macOS Keychain / Secret Service), with an owner-only file as fallback. They never go in `.env`, logs or tool output, and `Connection.__repr__` hides them.
- **Headless setup.** `connect --code` accepts a Self Client code from the Zoho API console. `disconnect` revokes the refresh token at Zoho.

## Rate limits

Zoho Inventory enforces three limits per org: 100 req/min, 5 (free) or 10 (paid) concurrent requests, and 1,000–10,000 req/day by plan.

| Layer | Behaviour |
|---|---|
| Sliding 60s window | Defaults to 90/min, leaving room for the merchant's other integrations. Callers wait when the window is full, but only up to `max_wait_seconds`; past that the call fails fast with `RATE_LIMITED` + `retry_after_s`, so an agent conversation never hangs. |
| Semaphore | Caps in-flight requests at the plan's concurrency limit, so this connector alone can't trigger Zoho's `1070` error (other apps on the same org still can, so 1070 is retried too). |
| Daily quota counter | Persisted per org per day (IST by default), so it survives restarts. The connector refuses locally once the quota is spent, because every rejected call would still burn quota. |
| Upstream 429 / 1070 / 5xx / timeout | Honours `Retry-After`, otherwise uses exponential backoff with full jitter, bounded by retry count *and* total wait. A 429 penalises the shared window, so every concurrent caller backs off, not just the one that got it. A 429 that mentions the daily limit becomes `DAILY_QUOTA_EXHAUSTED` with a reset time. |
| TTL cache | Items 60s, orders 30s. Saves quota on repeated questions. The availability report says how stale it may be. |

Only GETs are retried. Since the connector never writes, every retry is safe.

## Safety

- **Read-only is enforced in three places:** the OAuth scopes (Zoho refuses writes), the tool surface (no write tools), and the MCP annotations (`readOnlyHint: true, destructiveHint: false`), which let hosts auto-approve the tools.
- **PII is minimised by default**, and the setting is controlled by the operator, never by a tool argument.
- **Untrusted text is wrapped.** Merchant free text is wrapped, truncated to 500 characters and stripped of control characters. The demo data includes a real injection attempt (`ST-CANDLE-SANDAL`), and the eval checks the model doesn't act on it.
- **Audit log.** Every tool call produces one JSON audit line on stderr and in `~/.zoho-connector/audit.jsonl`: tool, arguments (customer names redacted), status, error code, latency, Zoho calls spent and quota left. This matches Agent Studio's audit-trail principle.
- **Unexpected exceptions** become a generic `UPSTREAM_ERROR`. Stack traces and upstream payloads never reach the model.

## Code layout

`tools.py` holds all the behaviour and doesn't depend on any transport. `server.py` is a thin MCP adapter. If Agent Studio's runtime prefers in-process tools via the Claude Agent SDK over MCP, `InventoryTools` can be registered directly.

## Testing approach

- **Mock Zoho.** `mock/server.py` is a Starlette app implementing the accounts server and the Inventory endpoints this connector uses: envelopes, `page_context`, 401 on expired tokens, 429 with `Retry-After`, the daily limit, the 1070 concurrency error, and fault injection. The demo mode, the unit tests and the `mock-server` command all use the same app, in-process through `httpx.ASGITransport` or on a real port.
- **End-to-end OAuth test.** One test runs the full browser OAuth flow on real sockets: consent page → Accept → redirect to the loopback listener → code exchange → org binding → token stored.
- **Real stdio launch.** One test launches `zoho-connector serve --demo` as a subprocess over stdio, exactly as Claude Desktop does. If anything prints to stdout, that test fails.
- **Eval harness.** `evals/run_eval.py` runs 12 merchant questions through any OpenAI-compatible LLM and checks the tool choice, the arguments and grounded facts in the answer. A scripted "oracle" model shows every scenario is answerable from the data, so a failure points at the model or the tool descriptions, not at the test.

## What I'd do next with a design-partner merchant

1. **Webhooks** (Zoho workflow → Agent Studio trigger) so agents react to "order shipped" or "stock below reorder" instead of polling.
2. **A courier tracking lookup** (via Shiprocket, already an Agent Studio connector) to close the "shipped but not marked delivered" gap automatically.
3. **Write tools behind merchant approval**, e.g. "hold order", "add note", using Agent Studio's review-first mode, with separate write scopes on a separate connection.
4. **Org-specific mapping.** Some merchants store the Razorpay payment ID in a custom field rather than `reference_number`, so make that a per-connection setting.
5. **The same tool contract over Unicommerce / WooCommerce**, so agents don't care which OMS a merchant uses.
