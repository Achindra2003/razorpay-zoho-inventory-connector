# Measuring impact

A connector is only worth deploying if it changes an outcome the merchant cares about. These are the metrics I'd agree with a design-partner merchant before turning it on, and how each would be measured. The audit log (`audit.jsonl`, one line per tool call) and Agent Studio's own agent logs make all of them computable without new instrumentation.

## Business outcomes

| Use case | Metric | How | Baseline → target (hypothesis to validate) |
|---|---|---|---|
| Cart recovery | **Wasted recovery attempts**: calls/messages about SKUs that could not ship | Recovery attempts whose cart had `check_availability.sellable=false` for any SKU, before vs after the connector | Unknown today, which is the point. Measure for 2 weeks with the check in shadow mode (logged, not enforced), then enforce |
| Cart recovery | Recovered GMV per attempt | Agent Studio conversion ÷ attempts | Should rise as dead attempts drop |
| Disputes | **Time to assemble delivery evidence** | Ops team stopwatch on 20 disputes manually vs `get_fulfilment_evidence` latency (p95 in audit log) | ~10–15 min manual → seconds |
| Disputes | Win rate on "product not received" disputes | Razorpay dispute outcomes, split by evidence `verdict` and whether `evidence_gaps` was empty | Expect higher win rate where the verdict is DELIVERED and there are no gaps; the gap list also tells ops which fulfilment data to fix |
| Disputes | Data-quality backlog | Count of `evidence_gaps` by type (no AWB, shipped-not-delivered) per week | Should trend down once the merchant sees it |

## Connector health (from the audit log)

| Metric | Why it matters | Alert if |
|---|---|---|
| Zoho calls per agent conversation | Free plan = 1,000/day; this sets how many conversations the merchant can afford | > 8 |
| Daily quota used % | A quota spent by noon means agents go blind for the afternoon | > 70% before 18:00 IST |
| `RATE_LIMITED` / `DAILY_QUOTA_EXHAUSTED` rate | User-visible failures | > 1% of calls |
| Upstream 429s absorbed | Shows whether our client-side budget is set right | Non-zero means another app shares the org's limit → lower `ZOHO_RATE_PER_MINUTE` |
| `AUTH_EXPIRED` | Merchant revoked access or the token expired | Any; notify the merchant to reconnect |
| p95 tool latency | Voice agents need answers mid-call | > 2 s |
| Cache hit rate | Quota efficiency | < 20% suggests TTLs are too short for the traffic pattern |

## Agent quality (eval harness)

`evals/run_eval.py` runs 12 merchant questions across cart recovery, disputes, order tracking, inventory ops and safety (prompt injection, write refusal). It scores:

- **Tool choice**: an expected tool was called with the expected arguments, and no forbidden tool was called.
- **Grounded answer**: the answer contains the facts it must (SKU, AWB, verdict) and none of the things it must not (customer emails, claims of refunds).

The harness is checked in CI-style tests with a scripted oracle model, so every scenario is known to be answerable from the demo data. Run it against a real model with a free Groq key (`make eval`).

| Run | Model | Result | What changed |
|---|---|---|---|
| 1 | gpt-oss-120b (Groq) | 10/12 (tool choice 12/12); crashed on one provider error | Baseline |
| 2 | gpt-oss-120b (Groq) | **12/12** (tool choice 12/12, answers 12/12, 1 provider resample) | `check_availability` returns `other_locations`; the scorer normalises Unicode punctuation; the harness survives and resamples `tool_use_failed` |

The cart-location miss is the useful one. The model chose the right tool, but the tool didn't return enough for it to give a helpful answer. That isn't something a unit test would catch. Re-run it whenever tool descriptions change: description edits are the cheapest lever on tool-choice accuracy, and the easiest one to regress.
