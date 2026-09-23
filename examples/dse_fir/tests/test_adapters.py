"""Transport contracts; domain behavior belongs to the service tests."""
import asyncio
import json
import subprocess
import sys

import pytest
from mcp.server.fastmcp import FastMCP


def test_cli_schema_and_errors_are_json(tmp_path):
    command = [sys.executable, "-m", "examples.dse_fir", "--root", str(tmp_path / "unused")]
    result = subprocess.run(command + ["schemas"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 6
    assert not (tmp_path / "unused").exists()
    result = subprocess.run(command + ["call", "dse_pysim", "--args", "not-json"], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert json.loads(result.stdout)["ok"] is False


def test_workspace_does_not_import_dse():
    result = subprocess.run([sys.executable, "-c", "import sys; from waveflow.mcp.server import build_mcp; build_mcp(); assert 'examples.dse_fir.service' not in sys.modules; assert 'examples.dse_fir.dse_tools' not in sys.modules"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_cli_reports_service_failures_and_exports_csv(tmp_path):
    command = [sys.executable, "-m", "examples.dse_fir", "--root", str(tmp_path)]
    bad = subprocess.run(command + ["call", "dse_pysim", "--args", '{"params":{"unknown":1}}'], capture_output=True, text=True, check=False)
    assert bad.returncode != 0
    assert json.loads(bad.stdout)["status"] == "invalid"
    exported = subprocess.run(command + ["results", "--format", "csv"], capture_output=True, text=True, check=False)
    assert exported.returncode == 0, exported.stdout
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(exported.stdout)))
    assert len(rows) == 1
    assert rows[0]["status"] == "invalid"


def test_mcp_stdio_six_tools_and_live_results(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise():
        server = StdioServerParameters(command=sys.executable, args=["-m", "examples.dse_fir", "--root", str(tmp_path), "serve"])
        async with stdio_client(server) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
            assert len(tools) == 6
            assert all(t.name.startswith("dse_") for t in tools)
            result = await client.call_tool("dse_get_results", {})
            assert not result.isError
            assert json.loads(result.content[0].text)["total"] == 0
            await client.call_tool("dse_pysim", {"params": {}})
            result = await client.call_tool("dse_get_results", {})
            assert json.loads(result.content[0].text)["total"] == 1
    asyncio.run(exercise())


class RecordingService:
    def __init__(self):
        self.calls = []

    def invoke(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True, "data": arguments}


def test_registry_six_tools_match_mcp_and_dispatch():
    from examples.dse_fir.dse_tools import make_dse_registry

    service = RecordingService()
    registry = make_dse_registry(service)
    schemas = registry.tool_schemas("dse")
    assert len(schemas) == 6
    mcp = FastMCP("test")
    registry.register_all(mcp, "dse")
    actual = {tool.name: tool.inputSchema for tool in asyncio.run(mcp.list_tools())}
    for schema in schemas:
        fn = schema["function"]
        assert fn["parameters"] == actual[fn["name"]]
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="validation"):
        asyncio.run(mcp.call_tool("dse_get_results", {"offset": "1"}))
    with pytest.raises(ToolError, match="validation"):
        asyncio.run(mcp.call_tool("dse_get_results", {"command": "bad"}))
    registry.dispatch("dse_get_results", {})
    assert service.calls[-1] == ("dse_get_results", {"offset": 0, "limit": 5})
    registry.dispatch("dse_synth", {"params": [{"x": 2}]})
    assert service.calls[-1][1] == {"params": [{"x": 2}]}
    with pytest.raises((ValueError, TypeError)):
        registry.dispatch("dse_synth", {"params": {}, "command": "bad"})
    with pytest.raises((ValueError, TypeError)):
        registry.dispatch("dse_get_results", {"offset": "1"})
    with pytest.raises((ValueError, TypeError)):
        registry.dispatch("dse_get_results", {"limit": True})


def test_compact_pages_preserve_ids_and_full_store(tmp_path):
    from examples.dse_fir.dse_tools import make_dse_registry
    from examples.dse_fir.service import DseService

    service = DseService(tmp_path)
    registry = make_dse_registry(service)
    point = {"ntap": 32, "samp_w": 16}
    registry.dispatch("dse_pysim", {"params": point})
    registry.dispatch("dse_predict_resource", {"params": point})
    registry.dispatch("dse_synth", {"params": [point, {"ntap": 16}, {"ntap": 8},
                                               {"ntap": 32, "samp_w": 12}]})
    result = registry.dispatch("dse_get_results", {"limit": 100})
    assert result["returned_count"] == 5 and result["has_more"]
    assert result["next_offset"] == 5 and result["total"] == 6
    assert "evaluation_refs" in result["candidates"][0]
    assert len(json.dumps(result, indent=2)) < 30000
    context = registry.dispatch("dse_get_dse_context", {})
    assert context["best_feasible"]["feasible"] is True
    assert len(json.dumps(context, indent=2)) < 15000
    assert "metadata" in service.results()["candidates"][0]["quality"]


def test_checkpoint_transport_preserves_content_address(tmp_path):
    from examples.dse_fir.contracts import identity
    from examples.dse_fir.dse_tools import make_dse_registry
    from examples.dse_fir.service import DseService

    service = DseService(tmp_path)
    expected = service.context()["checkpoint"]
    actual = json.loads(make_dse_registry(service).dispatch("dse_get_dse_context", {})["checkpoint_json"])
    assert actual == expected
    assert actual["checkpoint_id"] == identity({k: v for k, v in actual.items() if k != "checkpoint_id"})


def test_context_carries_lossless_checkpoint_json(tmp_path):
    from examples.dse_fir.contracts import canonical
    from examples.dse_fir.dse_tools import make_dse_registry
    from examples.dse_fir.service import DseService

    service = DseService(tmp_path, {"constraints": {"max_top_lut": 9007199254740993}})
    context = make_dse_registry(service).dispatch("dse_get_dse_context", {})
    assert context.get("checkpoint_json") == canonical(service.context()["checkpoint"])
    assert "checkpoint" not in context  # One wire representation, no duplicated evidence.
    assert "9007199254740993" in context["checkpoint_json"]
