"""Does a real LLM pick the right tools and give grounded answers?

Runs each scenario in scenarios.json as a fresh conversation: the model sees only the MCP
server's instructions and tool schemas, calls tools through a real MCP client session (demo
data), and is scored on (a) whether it called an expected tool with the expected arguments,
(b) whether its answer contains the facts it must and avoids what it must not.

Any OpenAI-compatible chat endpoint works:
  Groq (free tier):   EVAL_API_KEY=gsk_...   (defaults: api.groq.com, openai/gpt-oss-120b)
  Anthropic:          EVAL_BASE_URL=https://api.anthropic.com/v1/ EVAL_MODEL=claude-haiku-4-5 EVAL_API_KEY=sk-ant-...
  Ollama (local):     EVAL_BASE_URL=http://localhost:11434/v1 EVAL_MODEL=qwen2.5:7b EVAL_API_KEY=ollama

  uv run python evals/run_eval.py [--only dispute-delivered] [--out evals/results/latest.json]
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import httpx
from mcp import Client

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))

from zoho_connector.config import Settings  # noqa: E402
from zoho_connector.server import create_server  # noqa: E402

SYSTEM = (
    "You are an operations agent for an Indian D2C merchant inside Razorpay Agent Studio. "
    "Answer the merchant's question using the Zoho Inventory tools. Be concise and factual; "
    "cite SKUs, order numbers, AWBs and dates from tool results. Never invent data."
)
MAX_TURNS = 6


class ProviderError(Exception):
    """The LLM API rejected a turn (e.g. Groq's 400 tool_use_failed for a malformed tool call)."""


class Model(Protocol):
    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


class OpenAICompatible:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.http = httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {api_key}"})

    async def chat(self, messages, tools):
        for attempt in range(5):
            resp = await self.http.post(f"{self.base_url}/chat/completions", json={
                "model": self.model, "messages": messages, "tools": tools, "temperature": 0,
            })
            if resp.status_code == 429:  # free tiers throttle; be patient
                await asyncio.sleep(float(resp.headers.get("retry-after", 2 ** attempt)))
                continue
            if resp.status_code >= 400:
                raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:400]}")
            return resp.json()["choices"][0]["message"]
        raise ProviderError("still rate limited after 5 attempts")


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Some providers reject $ref/$defs in tool parameters; inline them."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def to_openai_tools(mcp_tools) -> list[dict[str, Any]]:
    return [{
        "type": "function",
        "function": {"name": t.name, "description": t.description or "", "parameters": _inline_refs(t.input_schema)},
    } for t in mcp_tools]


@dataclass
class Result:
    id: str
    use_case: str
    passed: bool
    tool_ok: bool
    answer_ok: bool
    calls: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0
    provider_retries: int = 0


def _args_match(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, want in expected.items():
        if key == "skus":
            got = {str(l.get("sku", "")).upper() for l in actual.get("lines", [])}
            if not {s.upper() for s in want} <= got:
                return False
        elif str(actual.get(key, "")).lower() != str(want).lower():
            return False
    return True


_DASHES = str.maketrans({**{c: "-" for c in "‐‑‒–—−"}, "‘": "'", "’": "'", "“": '"', "”": '"'})


def normalise(text: str) -> str:
    """Models (e.g. gpt-oss) write SO-00104 with a non-breaking hyphen and can't with a curly apostrophe;
    compare on plain ASCII punctuation."""
    return text.translate(_DASHES).replace(" ", " ").replace(" ", " ").lower()


def score(scn: dict[str, Any], calls: list[dict[str, Any]], answer: str) -> Result:
    failures: list[str] = []
    expected = scn.get("expect_tools", [])
    if expected:
        tool_ok = any(c["name"] == e["name"] and _args_match(c["args"], e.get("args_include", {})) for e in expected for c in calls)
        if not tool_ok:
            failures.append(f"expected one of {[e['name'] for e in expected]} with {[e.get('args_include', {}) for e in expected]}")
    else:
        tool_ok = True
    forbidden = [c["name"] for c in calls if c["name"] in scn.get("forbid_tools", [])]
    if forbidden:
        tool_ok = False
        failures.append(f"called forbidden tools {forbidden}")
    low = normalise(answer)
    missing = [s for s in scn.get("answer_must_include", []) if normalise(s) not in low]
    any_of = scn.get("answer_must_include_any", [])
    leaked = [s for s in scn.get("answer_must_not_include", []) if normalise(s) in low]
    if missing:
        failures.append(f"answer missing {missing}")
    if any_of and not any(normalise(s) in low for s in any_of):
        failures.append(f"answer has none of {any_of}")
    if leaked:
        failures.append(f"answer contains forbidden {leaked}")
    answer_ok = not (missing or leaked or (any_of and not any(normalise(s) in low for s in any_of)))
    return Result(scn["id"], scn.get("use_case", ""), tool_ok and answer_ok, tool_ok, answer_ok, calls, answer, failures)


async def chat_with_resample(model: Model, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_resamples: int = 2) -> tuple[dict[str, Any], int]:
    """Groq answers 400 tool_use_failed when the model emits a malformed call (e.g. gpt-oss leaking
    "<|channel|>" into the tool name). Production agent loops resample; so do we, and count it."""
    for attempt in range(max_resamples + 1):
        try:
            return await model.chat(messages, tools), attempt
        except ProviderError as exc:
            if "tool_use_failed" not in str(exc) or attempt == max_resamples:
                raise
    raise AssertionError("unreachable")


async def run_scenario(model: Model, scn: dict[str, Any], client: Client, tools: list[dict[str, Any]], instructions: str) -> Result:
    started = time.monotonic()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM + "\n\nTool server instructions:\n" + instructions},
        {"role": "user", "content": scn["prompt"]},
    ]
    calls: list[dict[str, Any]] = []
    answer = ""
    retries = 0
    for _ in range(MAX_TURNS):
        try:
            msg, used = await chat_with_resample(model, messages, tools)
            retries += used
        except ProviderError as exc:
            res = score(scn, calls, answer)
            res.passed = res.answer_ok = False
            res.failures.append(f"provider error: {exc}")
            res.seconds = round(time.monotonic() - started, 1)
            res.provider_retries = retries
            return res
        tool_calls = msg.get("tool_calls") or []
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")} | {"role": "assistant"})
        if not tool_calls:
            answer = msg.get("content") or ""
            break
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await client.call_tool(name, args)
            text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
            calls.append({"name": name, "args": args, "error": result.is_error})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": text[:8000]})
    res = score(scn, calls, answer)
    res.seconds = round(time.monotonic() - started, 1)
    res.provider_retries = retries
    return res


async def run(model: Model, scenarios: list[dict[str, Any]], on_result: Callable[[Result], Awaitable[None] | None] | None = None) -> list[Result]:
    import logging

    for name in ("httpx", "zoho_connector", "mcp"):  # keep the eval output readable
        logging.getLogger(name).setLevel(logging.WARNING)
    results = []
    for scn in scenarios:
        # Fresh server per scenario: no cache or state leaks between conversations.
        server, _ = create_server(Settings(state_dir=Path(tempfile.mkdtemp())), demo=True)
        async with Client(server) as client:
            tools = to_openai_tools((await client.list_tools()).tools)
            res = await run_scenario(model, scn, client, tools, client.instructions or "")
        results.append(res)
        if on_result:
            on_result(res)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="comma-separated scenario ids")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "latest.json")
    args = parser.parse_args()

    key = os.environ.get("EVAL_API_KEY") or os.environ.get("GROQ_API_KEY")
    if not key:
        sys.exit("Set EVAL_API_KEY (a free Groq key works: https://console.groq.com/keys). See the module docstring.")
    base = os.environ.get("EVAL_BASE_URL", "https://api.groq.com/openai/v1")
    model_name = os.environ.get("EVAL_MODEL", "openai/gpt-oss-120b")
    scenarios = json.loads((ROOT / "scenarios.json").read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]

    def show(r: Result) -> None:
        mark = "PASS" if r.passed else "FAIL"
        tools = ", ".join(c["name"] for c in r.calls) or "-"
        resampled = f"  (provider resampled x{r.provider_retries})" if r.provider_retries else ""
        print(f"{mark}  {r.id:<20} tools: {tools}{resampled}" + (f"\n      {'; '.join(r.failures)}" if r.failures else ""))
        if not r.passed and r.answer:
            print("      answer: " + " ".join(r.answer.split())[:300])

    print(f"Model: {model_name} @ {base}\n")
    results = asyncio.run(run(OpenAICompatible(base, key, model_name), scenarios, show))
    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed "
          f"(tool choice {sum(r.tool_ok for r in results)}/{len(results)}, answers {sum(r.answer_ok for r in results)}/{len(results)}, "
          f"provider resamples {sum(r.provider_retries for r in results)})")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"model": model_name, "base_url": base, "passed": passed, "total": len(results),
                                    "results": [asdict(r) for r in results]}, indent=2), encoding="utf-8")
    print(f"Details: {args.out}")


if __name__ == "__main__":
    main()
