# What an agent can and cannot do with this connector

One connection is bound to **one merchant's Zoho Inventory organization**, with **read-only** OAuth scopes.

## Can

| Ask | Tool |
|---|---|
| "Can I still sell / pitch these SKUs?" (≤25 SKUs per call, optionally per warehouse) | `check_availability` |
| "What's running low?" / browse the catalogue | `list_items` |
| Find an item by partial name or SKU | `search_items` |
| Full item detail with stock per warehouse | `get_item` |
| Browse orders by status or date | `list_sales_orders` |
| Find orders by Razorpay/storefront reference, order number, customer or text | `search_sales_orders` |
| One order with line items, shipped quantities and packages | `get_sales_order` |
| Proof of delivery for a dispute: carrier, AWB, ship/delivery dates, verdict, gaps | `get_fulfilment_evidence` |
| Which org is connected, and how much API budget is left | `connector_status` (costs no API calls) |

## Cannot, by design

- **Change anything.** There is no create, update, cancel, refund or stock-adjust tool, and the OAuth grant only has `*.READ` scopes, so Zoho itself would refuse a write.
- **See customers' full contact details.** Email, phone and street address are masked (`d***@example.com`, `********0037`, city/state/PIN only). Only the operator can turn this off with `ZOHO_PII_MODE=full`; the model can't.
- **Read purchasing, vendors, bills, invoices or accounting data.** Those scopes are not requested.
- **Reach another merchant's data.** Credentials are bound to the connection, never passed as tool arguments.
- **Act on instructions hidden in data.** Item descriptions and order notes come back as `UntrustedText`, labelled as data. (The label is an instruction to the agent, not a guarantee. The real protection is that no tool can do anything harmful.)

## Cannot, because of limits

- **Real-time stock.** Reads are cached for up to 60s (orders 30s) to save quota. Re-check before committing to a customer.
- **Unlimited calls.** Zoho allows 100 requests/min and 1,000–10,000/day per org depending on plan. The connector budgets 90/min and tracks the daily count. When either runs out, tools return `RATE_LIMITED` or `DAILY_QUOTA_EXHAUSTED` with a retry time instead of hanging.
- **Push updates.** It is pull-only (no webhooks), so the agent learns about a new order only when it asks.
- **Fuzzy search.** Zoho matching is exact, "contains" or "starts with". A typo in a SKU means no match.
- **Carrier tracking.** Delivery status is whatever the merchant recorded in Zoho. The connector does not query Delhivery, Blue Dart and the like, and says so in `evidence_gaps`.

## Errors the agent will see

`NOT_CONNECTED`, `AUTH_EXPIRED` (ask the merchant to reconnect) · `RATE_LIMITED`, `DAILY_QUOTA_EXHAUSTED` (wait `retry_after_s`) · `NOT_FOUND`, `INVALID_INPUT` (fix the arguments) · `FORBIDDEN` · `UPSTREAM_ERROR`, `TIMEOUT` (retry once). Each error comes with a one-line `hint` saying what to do next.
