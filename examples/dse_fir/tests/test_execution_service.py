"""Service/transport contracts; adapters are deliberately isolated here."""
import json
import sys
import types
from typing import ClassVar

import pytest


class TransportDesign:
    id = "transport_test"
    identity: ClassVar[dict] = {"source": "test-transport-only"}

    def describe(self):
        return {"design_id": self.id, "parameter_schema": {}, "conditions": {},
                "metric_definitions": {}, "capabilities": {}}

    def validate(self, params):
        if set(params) - {"value"}:
            raise ValueError("unknown parameter")
        return {"value": params.get("value", 1)}

    def pysim(self, params, run_dir):
        (run_dir / "trace.txt").write_text(str(params["value"]))
        return {"status": "success", "metrics": {}, "checks": [],
                "artifacts": ["trace.txt"], "evidence_kind": "transport_test"}


@pytest.fixture
def service(tmp_path, monkeypatch):
    # Registration is the dependency boundary: no real domain calculations mocked.
    module = types.ModuleType("examples.dse_fir.execution.designs")
    module.get_design = lambda name, conditions=None: TransportDesign()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    from examples.dse_fir.execution import ExecutionService
    return ExecutionService(tmp_path)


def test_repeated_requests_retain_fresh_attempts_after_reopen(service):
    from examples.dse_fir.execution import ExecutionService
    first = service.invoke("pysim", {"params": {}})
    second = service.invoke("pysim", {"params": {"value": 1}})
    assert first["status"] == "success"
    assert first["request_id"] == second["request_id"]
    assert first["run_id"] != second["run_id"]
    assert first["schema_version"] == "waveflow-execution-v1"
    assert first["timing"]["wall_seconds"] >= 0
    assert (service.root / "runs" / first["run_id"] / "trace.txt").read_text() == "1"
    reopened = ExecutionService(service.root)
    assert reopened.results() == [first, second]
    assert reopened.describe()["execution"]["cache"] == "disabled; every invocation is a fresh attempt"
    json.dumps(first, allow_nan=False)


@pytest.mark.parametrize("operation,arguments,status", [
    ("pysim", {"params": {"unknown": 2}}, "invalid"),
    ("pysim", {"params": {}, "conditions": {}}, "invalid"),
    ("pysim", {"params": {"value": float("nan")}}, "invalid"),
    ("pysim", [], "invalid"),
    ("unknown", {}, "invalid"),
    ("synth", {"params": {}}, "unavailable"),
    ("rtlsim", {"synthesis_id": "../../etc/passwd"}, "invalid"),
    ("rtlsim", {"synthesis_id": "a" * 32}, "invalid"),
])
def test_failed_attempts_are_durable(service, operation, arguments, status):
    result = service.invoke(operation, arguments)
    assert result["status"] == status
    assert result["error"]
    assert result["metrics"] == {}
    assert service.results() == [result]
    json.dumps(result, allow_nan=False)


def test_discovery_is_an_observation_with_schema(service):
    result = service.invoke("get_param_list", {})
    assert result["status"] == "ok"
    assert result["description"]["design_id"] == "transport_test"
    assert result["evidence_kind"] == "discovery"
    assert service.results() == [result]


@pytest.mark.parametrize("fault", ["exception", "nan", "path", "symlink", "envelope"])
def test_adapter_faults_cannot_corrupt_evidence(service, monkeypatch, fault):
    def broken(params, run_dir):
        print("adapter output must stay out of JSON transport")
        if fault == "exception":
            raise RuntimeError("simulation exploded")
        result = TransportDesign.pysim(service.design, params, run_dir)
        if fault == "nan":
            result["metrics"] = {"bad": {"value": float("nan")}}
        elif fault == "path":
            result["artifacts"] = {"escape": "../experiment.json"}
        elif fault == "symlink":
            (run_dir / "escape").symlink_to(service.root / "experiment.json")
            result["artifacts"] = {"escape": "escape"}
        else:
            result["run_id"] = "forged"
        return result
    monkeypatch.setattr(service.design, "pysim", broken)
    result = service.invoke("pysim", {"params": {}})
    assert result["status"] == "failed"
    assert result["run_id"] != "forged"
    assert result["metrics"] == {}
    assert result["error"]
    assert service.results() == [result]
    assert "adapter output" in (service.root / "runs" / result["run_id"] / "execution.log").read_text()


@pytest.mark.parametrize("design,conditions", [
    ("fir", {"nsamp": 1024, "nfft": 2048}),
    ("mem_copy", {"bytes_per_job": 512, "num_cmds": 4}),
])
def test_real_design_service_artifacts_and_frozen_root(tmp_path, design, conditions):
    from examples.dse_fir.execution import ExecutionService
    engine = ExecutionService(tmp_path, design, conditions)
    run = engine.invoke("pysim", {"params": {}})
    assert run["status"] == "ok", run.get("error")
    assert run["metrics"]
    assert all((tmp_path / "runs" / run["run_id"] / p).is_file()
               for p in run["artifacts"].values())
    assert ExecutionService(tmp_path, design, conditions).results() == [run]
    changed = dict(conditions)
    changed["seed" if design == "fir" else "num_cmds"] = 3
    with pytest.raises(ValueError, match="bound"):
        ExecutionService(tmp_path, design, changed)


@pytest.mark.parametrize("design,conditions,params,metric,value", [
    ("mem_copy", {"bytes_per_job": 64, "num_cmds": 2}, {"mem_dwidth": 32}, "copied_bytes", 128),
    ("fir", {"nsamp": 1024, "nfft": 2048}, {"ntap": 8}, "throughput_samp_per_cyc", 1.0),
])
def test_cli_json_real_execution_and_failed_exit(tmp_path, design, conditions, params, metric, value):
    import subprocess
    base = [sys.executable, "-m", "examples.dse_fir.execution", "--design", design,
            "--root", str(tmp_path), "--conditions", json.dumps(conditions)]
    def call(*args):
        proc = subprocess.run([*base, *args], capture_output=True, text=True, timeout=60, check=False)
        return proc, json.loads(proc.stdout)
    proc, description = call("describe")
    assert proc.returncode == 0 and description["design_id"] == design
    proc, run = call("call", "pysim", "--args", json.dumps({"params": params}))
    assert proc.returncode == 0 and run["metrics"][metric]["value"] == value
    proc, failed = call("call", "synth", "--args", '{"params":{}}')
    assert proc.returncode != 0 and failed["status"] == "unavailable"
    proc, rows = call("results")
    assert proc.returncode == 0 and rows == [run, failed]


@pytest.mark.parametrize("design", ["fir", "mem_copy"])
def test_real_stdio_mcp_four_tools_strict_args_and_persistence(tmp_path, design):
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise():
        server = StdioServerParameters(command=sys.executable, args=[
            "-m", "examples.dse_fir.execution", "--design", design,
            "--root", str(tmp_path), "serve"])
        async with stdio_client(server) as (read, write):  # noqa: SIM117 -- keep transport/session lifetimes visible
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert {t.name for t in tools.tools} == {"get_param_list", "pysim", "synth", "rtlsim"}
                for tool in tools.tools:
                    assert tool.inputSchema["additionalProperties"] is False
                result = await client.call_tool("pysim", {"params": {}})
                assert not result.isError
                run = json.loads(result.content[0].text)
                assert run["status"] == "ok" and run["metrics"]
                for op, args in [("pysim", {"params": {}, "extra": 1}),
                                 ("pysim", {"params": "{}"}),
                                 ("rtlsim", {"synthesis_id": 42})]:
                    invalid = await client.call_tool(op, args)
                    assert invalid.isError
                nested = {"ntap": 8.0} if design == "fir" else {"mem_dwidth": 32.0}
                invalid = await client.call_tool("pysim", {"params": nested})
                assert json.loads(invalid.content[0].text)["status"] == "invalid"
                discovery = await client.call_tool("get_param_list", {})
                assert json.loads(discovery.content[0].text)["description"]["design_id"] == design
                unavailable = await client.call_tool("synth", {"params": {}})
                synthesis = json.loads(unavailable.content[0].text)
                assert synthesis["status"] == "unavailable" and not synthesis["metrics"]
                rtl = await client.call_tool("rtlsim", {"synthesis_id": synthesis["run_id"]})
                assert json.loads(rtl.content[0].text)["status"] == "invalid"
                return run
    run = asyncio.run(exercise())
    from examples.dse_fir.execution import ExecutionService
    rows = ExecutionService(tmp_path, design).results()
    assert rows[0] == run
    assert [r["status"] for r in rows] == ["ok", "invalid", "invalid", "invalid", "invalid",
                                            "ok", "unavailable", "invalid"]


def test_root_binding_rejects_changed_identity(service, monkeypatch):
    from examples.dse_fir.execution import ExecutionService
    monkeypatch.setattr(TransportDesign, "identity", {"source": "changed"})
    with pytest.raises(ValueError, match="bound"):
        ExecutionService(service.root)
    assert json.loads((service.root / "experiment.json").read_text())["identity"] == {
        "source": "test-transport-only"}
