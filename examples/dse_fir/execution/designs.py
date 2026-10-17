"""Registered CPU execution adapters; no synthesis or RTL evidence is invented."""
from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from examples.dse_fir.contracts import Candidate, Evaluation

FIR_METRICS = {
    "stopband_atten_db": {"unit": "dB", "kind": "numerical_quality", "definition": "20 log10(mean passband magnitude / worst inclusive stopband FFT-bin magnitude); finite [-300,300] dB convention."},
    "passband_ripple_db": {"unit": "dB", "kind": "numerical_quality", "definition": "20 log10(max/min inclusive passband FFT-bin magnitude)."},
    "stopband_rej_db": {"unit": "dB", "kind": "numerical_quality", "definition": "10 log10(stopband input power * mean passband magnitude squared / output AC power)."},
    "passband_sndr_db": {"unit": "dB", "kind": "numerical_quality", "definition": "10 log10(positive least-squares delayed-input fitted power / residual AC power)."},
    "dc_bias": {"unit": "sample_amplitude", "kind": "numerical_quality", "definition": "Mean fixed-point output for the stopband waveform."},
    "throughput_samp_per_cyc": {"unit": "sample/cycle", "kind": "analytic_estimate", "definition": "Steady-state compute-only lane estimate, excluding transport/startup; NOT measured Python or RTL cycles."},
}


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


@dataclass(frozen=True)
class Design:
    id: str
    _conditions: str
    _identity: str

    @property
    def identity(self):
        return json.loads(self._identity)

    def describe(self):
        schema = Candidate.model_json_schema() if self.id == "fir" else {
            "type": "object", "additionalProperties": False,
            "properties": {"mem_dwidth": {"type": "integer", "enum": [32, 64], "default": 64}}}
        return json.loads(_json({
            "design_id": self.id, "parameter_schema": schema,
            "conditions": self.conditions,
            "metric_definitions": FIR_METRICS if self.id == "fir" else MEM_METRICS,
            "capabilities": {
                "get_param_list": {"available": True}, "pysim": {"available": True},
                "synth": {"available": False, "reason": "Live synthesis not yet integrated; no replay substitution."},
                "rtlsim": {"available": False, "reason": "Live RTL simulation not yet integrated; Python is not RTL proof."}}}))

    @property
    def conditions(self):
        return json.loads(self._conditions)

    def validate(self, params):
        if type(params) is not dict:
            raise ValueError("params must be an object")
        if self.id == "mem_copy":
            if set(params) - {"mem_dwidth"}:
                raise ValueError("unknown candidate parameters")
            width = params.get("mem_dwidth", 64)
            if type(width) is not int or width not in (32, 64):
                raise ValueError("mem_dwidth must be integer 32 or 64")
            return {"mem_dwidth": width}
        return Candidate.model_validate(params).model_dump()

    def pysim(self, params, run_dir: Path):
        from examples.dse_fir import fir_metrics

        params = self.validate(params)
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        if self.id == "mem_copy":
            return self._mem_copy(params, run_dir)
        raw = fir_metrics.evaluate(params, self.conditions)
        (run_dir / "fir_evaluation.json").write_text(_json(raw), encoding="utf-8")
        return {"status": "ok", "metrics": {k: {"value": raw[k], **v} for k, v in FIR_METRICS.items()},
                "checks": [{"name": "rtl_equivalence", "status": "not_checked", "reason": "Numerical quality is not proof of RTL correctness."}],
                "artifacts": {"evaluation": "fir_evaluation.json"}, "evidence_kind": "python_golden"}


    def _mem_copy(self, params, run_dir):
        from examples.mem_copy.mem_copy_build import PySimStep
        from waveflow.build.build import BuildConfig, BuildDag, SourceStep

        dag = BuildDag()
        dag.add(SourceStep(artifact="mem_copy_source", path=Path(__file__).resolve().parents[2] / "mem_copy/mem_copy.py"))
        dag.add(PySimStep(name="pysim"))
        cfg = self.conditions
        build = BuildConfig(root_dir=run_dir, params={**params, "n_words": cfg["bytes_per_job"] // (params["mem_dwidth"] // 8), "num_cmds": cfg["num_cmds"]})
        result = dag.run(build, force=True)["pysim"]
        if not result.success:
            return {"status": "failed", "metrics": {}, "checks": [{"name": "destination_and_completion", "status": "failed", "reason": result.message}], "artifacts": {}, "evidence_kind": "python_simulation", "error": result.message}
        raw = json.loads((run_dir / "results/pysim.json").read_text())
        values = {"copied_bytes": cfg["bytes_per_job"] * cfg["num_cmds"], "done_tokens": raw["done_tokens"], "end_cycles": raw["end_cycles"]}
        return {"status": "ok", "metrics": {k: {"value": v, **MEM_METRICS[k]} for k, v in values.items()},
                "checks": [{"name": "destination_matches_source", "status": "passed"}, {"name": "completion_count", "status": "passed"}, {"name": "completion_ids", "status": "not_checked", "reason": "Existing PySimStep checks count, not echoed transaction IDs."}, {"name": "rtl_equivalence", "status": "not_checked"}],
                "artifacts": {"evaluation": "results/pysim.json", **{
                    str(p.relative_to(run_dir)): str(p.relative_to(run_dir))
                    for p in sorted((run_dir / "vectors").rglob("*")) if p.is_file()}},
                "evidence_kind": "python_simulation"}


MEM_METRICS = {
    "copied_bytes": {"unit": "byte", "kind": "checked_workload", "definition": "Total destination bytes checked bit-exact against source: bytes_per_job * num_cmds, fixed across bus widths."},
    "done_tokens": {"unit": "completion", "kind": "functional_observation", "definition": "Number of captured framed completion bursts; must equal number of copy jobs."},
    "end_cycles": {"unit": "cycle", "kind": "modeled_timing", "definition": "SimPy end-of-simulation time times 100 MHz, rounded; uncalibrated shared-crossbar model, NOT last-completion latency, RTL cycles, or achieved hardware timing."},
}


def get_design(name, conditions=None):
    if name == "mem_copy":
        cfg = {"bytes_per_job": 1024, "num_cmds": 16}
        if conditions is not None:
            if type(conditions) is not dict or set(conditions) - set(cfg):
                raise ValueError("unknown host conditions")
            cfg.update(conditions)
        for key, lo, hi in (("bytes_per_job", 8, 16384), ("num_cmds", 1, 64)):
            if type(cfg[key]) is not int or not lo <= cfg[key] <= hi:
                raise ValueError(f"{key} must be an integer in [{lo}, {hi}]")
        if cfg["bytes_per_job"] % 8:
            raise ValueError("bytes_per_job must be divisible by 8 for both bus widths")
    elif name == "fir":
        cfg = Evaluation.model_validate({} if conditions is None else conditions).model_dump()
    else:
        raise ValueError(f"Unknown design: {name}")
    root = Path(__file__).resolve().parents[3]
    # Conservative source closure: shared Python implementation plus both reused
    # examples and this surface. Content, not only package version, binds evidence.
    sources = set((root / "waveflow").rglob("*.py"))
    for directory in ("examples/fir_block", "examples/mem_copy"):
        sources.update((root / directory).rglob("*.py"))
    sources.update((root / "examples/dse_fir").glob("*.py"))
    sources.update((root / "examples/dse_fir/execution").glob("*.py"))
    identity = {
        "design_id": name, "python": platform.python_version(),
        "sources": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(sources)},
        "dependencies": {package: version(package) for package in
                         ("numpy", "scipy", "simpy", "pydantic", "mcp")}}
    return Design(name, _json(cfg), _json(identity))
