"""Transport-only FIR tools. Candidate validation and policy live in DseService."""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any, Protocol

from mcp.server.fastmcp.utilities.func_metadata import func_metadata
from pydantic import ConfigDict, validate_call

from waveflow.mcp.registry import ToolRegistry


class DseRegistry(ToolRegistry):
    """FastMCP 1.x defaults coerce and drop extras; enforce the direct contract."""

    def register_all(self, mcp, profile=None):
        super().register_all(mcp, profile)
        for name in self._tools:
            tool = mcp._tool_manager.get_tool(name)
            if tool is not None:
                model = tool.fn_metadata.arg_model
                model.model_config.update(strict=True, extra="forbid")
                model.model_rebuild(force=True)
                tool.parameters = model.model_json_schema()


class Service(Protocol):
    def invoke(self, name: str, arguments: dict) -> dict: ...


def compact_evidence(value: Any) -> Any:
    """Transport projection only; complete diagnostics remain in the durable store."""
    if isinstance(value, list):
        return [compact_evidence(item) for item in value]
    if not isinstance(value, dict):
        return value
    output = {}
    for key, item in value.items():
        if key in {"metadata", "composition", "confidence", "fitted_ranges", "source_hashes"}:
            continue
        if key == "observations":
            output["evaluation_refs"] = {
                op: {k: row[k] for k in ("evaluation_id", "status")}
                for op, row in item.items()
            }
        else:
            output[key] = compact_evidence(item)
    return output


def default_root() -> Path:
    from platformdirs import user_cache_path
    return Path(os.environ["WAVEFLOW_DSE_ROOT"]) if os.environ.get("WAVEFLOW_DSE_ROOT") else user_cache_path("waveflow") / "dse_fir"


def make_service(root: Path | str | None = None, config: dict | None = None) -> Service:
    with contextlib.redirect_stdout(sys.stderr):
        from examples.dse_fir.service import DseService
        return DseService(root=default_root() if root is None else root, config=config)


def make_dse_registry(service: Service | None = None) -> ToolRegistry:
    """Build exactly six tools, with identical direct/MCP signatures and schemas."""
    registry = DseRegistry()

    def invoke(name: str, arguments: dict) -> dict:
        if service is None:
            raise RuntimeError("Schema-only registry has no service")
        with contextlib.redirect_stdout(sys.stderr):
            result = compact_evidence(service.invoke(name, arguments))
            result["view"] = "compact evidence; full diagnostics in host-owned SQLite store"
            return result

    def dse_get_dse_context() -> dict:
        """Read live FIR candidate schema, objective, evidence semantics and budgets."""
        return invoke("dse_get_dse_context", {})

    def dse_pysim(params: dict[str, Any]) -> dict:
        """Evaluate one candidate in Python; not hardware timing evidence."""
        return invoke("dse_pysim", {"params": params})

    def dse_predict_resource(params: dict[str, Any]) -> dict:
        """Predict one candidate's resources; predictions are not synthesis evidence."""
        return invoke("dse_predict_resource", {"params": params})

    def dse_synth(params: list[dict[str, Any]]) -> dict:
        """Query a batch of synthesis evidence: currently frozen HLS replay, not a live toolchain."""
        return invoke("dse_synth", {"params": params})

    def dse_rtlsim(params: list[dict[str, Any]]) -> dict:
        """Request candidate-specific RTL evidence; unavailable in this corpus, never replaced by Python."""
        return invoke("dse_rtlsim", {"params": params})

    def dse_get_results(offset: int = 0, limit: int = 5) -> dict:
        """Read compact evidence, at most five rows per page; follow next_offset. Failures are included."""
        effective = min(limit, 5) if 1 <= limit <= 1000 else limit
        result = invoke("dse_get_results", {"offset": offset, "limit": effective})
        if "candidates" in result:
            ids = {row["candidate_id"] for row in result["rows"]}
            result["candidates"] = [c for c in result["candidates"] if c["candidate_id"] in ids]
            result["candidate_summaries_returned"] = len(result["candidates"])
            result["requested_limit"] = limit
        return result

    for fn in (dse_get_dse_context, dse_pysim, dse_predict_resource, dse_synth, dse_rtlsim, dse_get_results):
        # Export the actual FastMCP schema, not a separately maintained approximation.
        model = func_metadata(fn).arg_model
        model.model_config.update(strict=True, extra="forbid")
        model.model_rebuild(force=True)
        parameters = model.model_json_schema()
        registry.add(name=fn.__name__, description=fn.__doc__ or "", parameters=parameters,
                     fn=validate_call(config=ConfigDict(strict=True, extra="forbid"))(fn), profiles={"dse"})
    return registry
