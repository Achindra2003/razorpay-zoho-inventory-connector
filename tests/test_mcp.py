"""The MCP surface an Agent Studio agent actually sees."""

import json
from pathlib import Path

from mcp import Client

from zoho_connector.config import Settings
from zoho_connector.server import create_server

EXPECTED_TOOLS = {
    "list_items", "get_item", "search_items", "check_availability",
    "list_sales_orders", "search_sales_orders", "get_sales_order",
    "get_fulfilment_evidence", "connector_status",
}


def connect(tmp_path) -> Client:
    server, _ = create_server(Settings(state_dir=tmp_path), demo=True)
    return Client(server)


async def test_tool_catalogue_is_read_only(tmp_path):
    async with connect(tmp_path) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == EXPECTED_TOOLS
        for t in tools.values():
            assert t.annotations.read_only_hint is True
            assert t.annotations.destructive_hint is False
            assert t.description and len(t.description) > 40
        assert tools["get_fulfilment_evidence"].output_schema["properties"]["verdict"]["enum"]


async def test_structured_result(tmp_path):
    async with connect(tmp_path) as client:
        result = await client.call_tool("check_availability", {"lines": [{"sku": "ST-TEE-OLV-M"}]})
        assert not result.is_error
        assert result.structured_content["all_sellable"] is False


async def test_errors_are_machine_readable(tmp_path):
    async with connect(tmp_path) as client:
        result = await client.call_tool("get_sales_order", {"salesorder_number": "SO-99999"})
        assert result.is_error
        payload = json.loads(result.content[0].text.split(": ", 1)[1])
        assert payload["error"]["code"] == "NOT_FOUND"
        assert payload["error"]["hint"]


async def test_schema_validation_rejects_bad_arguments(tmp_path):
    async with connect(tmp_path) as client:
        result = await client.call_tool("list_items", {"page_size": 1000})
        assert result.is_error


async def test_published_spec_matches_code(tmp_path):
    async with connect(tmp_path) as client:
        spec = json.loads((Path(__file__).parents[1] / "docs" / "tools.json").read_text(encoding="utf-8"))
        live = {t.name: t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in (await client.list_tools()).tools}
        assert {t["name"]: t for t in spec["tools"]} == live, "docs/tools.json is stale: run `zoho-connector spec`"


async def test_stdio_subprocess_like_claude_desktop(tmp_path):
    """Launch `zoho-connector serve --demo` exactly as an MCP host would and talk over stdio."""
    import sys

    from mcp import StdioServerParameters

    params = StdioServerParameters(command=sys.executable, args=["-m", "zoho_connector.cli", "serve", "--demo"],
                                   env={"ZOHO_CONNECTOR_HOME": str(tmp_path), "PYTHONIOENCODING": "utf-8"}, cwd=str(tmp_path))
    async with Client(params) as client:
        result = await client.call_tool("get_fulfilment_evidence", {"salesorder_number": "SO-00101"})
        assert not result.is_error
        assert result.structured_content["verdict"] == "DELIVERED"
