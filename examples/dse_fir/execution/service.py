"""Problem-agnostic execution and run-local evidence. No caching or DSE policy."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _identity(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(_json(value) + "\n")
    temporary.replace(path)


class ExecutionService:
    def __init__(self, root, design="fir", conditions=None):
        from .designs import get_design
        self.root = Path(root).resolve()
        self.design = get_design(design, conditions)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        binding = {"identity": self.design.identity, "description": self.design.describe()}
        self.experiment_id = _identity(binding)
        binding_path = self.root / "experiment.json"
        if binding_path.exists():
            if json.loads(binding_path.read_text()) != binding:
                raise ValueError("root is bound to a different experiment or source identity")
        else:
            _write(binding_path, binding)

    def describe(self):
        return {**self.design.describe(), "experiment_id": self.experiment_id,
                "identity": self.design.identity,
                "execution": {"cache": "disabled; every invocation is a fresh attempt"}}

    def invoke(self, name, arguments):
        start = time.perf_counter()
        run_id = uuid.uuid4().hex
        run_dir = self.root / "runs" / run_id
        run_dir.mkdir()
        params = {}
        try:
            request = json.loads(_json(arguments))
        except (TypeError, ValueError):
            request = {"invalid_input_repr": repr(arguments)}
        observation = {"schema_version": "waveflow-execution-v1", "run_id": run_id,
                       "request_id": _identity([self.experiment_id, name, request]),
                       "experiment_id": self.experiment_id, "design_id": self.design.id,
                       "operation": name, "params": params, "status": "running",
                       "metrics": {}, "checks": [], "artifacts": {},
                       "evidence_kind": "none", "timing": {"wall_seconds": 0}}
        _write(run_dir / "request.json", {"operation": name, "arguments": request})
        _write(run_dir / "observation.json", observation)
        try:
            _json(arguments)
            fields = {"get_param_list": set(), "pysim": {"params"},
                      "synth": {"params"}, "rtlsim": {"synthesis_id"}}
            if name not in fields or type(arguments) is not dict or set(arguments) != fields[name]:
                raise ValueError("unknown operation or invalid argument fields")
            if name in ("pysim", "synth"):
                if type(arguments["params"]) is not dict:
                    raise ValueError("params must be an object")
                params = self.design.validate(arguments["params"])
                _json(params)
                observation["params"] = params
                request = {"params": params}
            if name == "rtlsim":
                synthesis_id = arguments["synthesis_id"]
                if (not isinstance(synthesis_id, str) or len(synthesis_id) != 32
                        or any(c not in "0123456789abcdef" for c in synthesis_id)):
                    raise ValueError("synthesis_id must be a run ID, not a filesystem path")
                source = self.root / "runs" / synthesis_id / "observation.json"
                if not source.is_file():
                    raise ValueError("unknown synthesis_id")
                synthesis = json.loads(source.read_text())
                if (synthesis["operation"] != "synth" or synthesis["status"] not in ("ok", "success")
                        or synthesis["experiment_id"] != self.experiment_id):
                    raise ValueError("synthesis_id does not identify successful bound synthesis")
                observation["params"] = synthesis["params"]
            observation["request_id"] = _identity([self.experiment_id, name, request])
        except (ValueError, TypeError) as exc:
            observation.update(status="invalid", error=str(exc))
        else:
            if name == "get_param_list":
                observation.update(status="ok", evidence_kind="discovery", description=self.describe())
            elif name == "pysim":
                try:
                    with ((run_dir / "execution.log").open("w") as log,
                          redirect_stdout(log), redirect_stderr(log)):
                        payload = self.design.pysim(params, run_dir)
                    _json(payload)
                    required = {"status", "metrics", "checks", "artifacts", "evidence_kind"}
                    if not required <= set(payload) or set(payload) - required - {"error"}:
                        raise ValueError("invalid adapter payload fields")
                    artifacts = payload["artifacts"]
                    paths = artifacts.values() if isinstance(artifacts, dict) else artifacts
                    for reference in paths:
                        path = Path(reference)
                        resolved = (run_dir / path).resolve()
                        if (path.is_absolute() or ".." in path.parts
                                or not resolved.is_relative_to(run_dir) or not resolved.is_file()):
                            raise ValueError("artifact must reference an existing run-local file")
                    observation.update(payload)
                except Exception as exc:  # noqa: BLE001 -- adapter failures are durable observations
                    observation.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            else:
                observation.update(status="unavailable", error="Live synthesis/RTL execution is not integrated in this initial surface")
        observation["timing"] = {"wall_seconds": time.perf_counter() - start}
        _write(run_dir / "observation.json", observation)
        return observation

    def results(self):
        files = sorted((self.root / "runs").glob("*/observation.json"),
                       key=lambda path: path.stat().st_mtime_ns)
        return [json.loads(path.read_text()) for path in files]


def make_execution_registry(service):
    """Expose only transport operations, reusing the existing strict MCP registry."""
    from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata, func_metadata
    from pydantic import ConfigDict, ValidationError, validate_call

    from examples.dse_fir.dse_tools import DseRegistry

    class StrictMetadata(FuncMetadata):
        operation: str

        def pre_parse_json(self, data):
            # FastMCP otherwise JSON-decodes strings before even strict validation.
            # Validate the actual wire values and retain rejected attempts too.
            try:
                self.arg_model.model_validate(data)
            except ValidationError:
                service.invoke(self.operation, data)
                raise
            return data

    class ExecutionRegistry(DseRegistry):
        def register_all(self, mcp, profile=None):
            super().register_all(mcp, profile)
            for name in self._tools:
                tool = mcp._tool_manager.get_tool(name)
                if tool is not None:
                    tool.fn_metadata = StrictMetadata(
                        **dict(tool.fn_metadata), operation=name)

    registry = ExecutionRegistry()

    def get_param_list() -> dict:
        """Discover candidate parameters and fixed conditions; no optimization policy."""
        return service.invoke("get_param_list", {})

    def pysim(params: dict) -> dict:
        """Run fresh Python simulation; preserve quality and correctness evidence."""
        return service.invoke("pysim", {"params": params})

    def synth(params: dict) -> dict:
        """Request synthesis; currently explicitly unavailable, never replayed."""
        return service.invoke("synth", {"params": params})

    def rtlsim(synthesis_id: str) -> dict:
        """Request RTL simulation by bound successful synthesis ID, never a path."""
        return service.invoke("rtlsim", {"synthesis_id": synthesis_id})

    for fn in (get_param_list, pysim, synth, rtlsim):
        model = func_metadata(fn).arg_model
        model.model_config.update(strict=True, extra="forbid")
        model.model_rebuild(force=True)
        registry.add(name=fn.__name__, description=fn.__doc__ or "",
                     parameters=model.model_json_schema(),
                     fn=validate_call(config=ConfigDict(strict=True, extra="forbid"))(fn),
                     profiles={"execution"})
    return registry
