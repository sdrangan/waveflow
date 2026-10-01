"""poly_build.py -- the build pipeline for the streaming polynomial accelerator.

    python poly_build.py --list-steps
    python poly_build.py --through check_pysim       # Python only, no Vitis
    python poly_build.py --through summary           # everything, Vitis included

The pipeline, in reading order:

    scenarios        write each scenario's stimulus and expected response (scenarios.py)
    py_model         run the pure bit-exact model on every scenario
    check_model      ...and compare it with the expected response
    py_sim           run pysim (the timing model) on the well-formed scenarios
    check_pysim      ...compare
    extract_py_timing  pysim's cycle count for the timing scenario
    gen_include      schema headers, serializers and the stream/testbench helpers
    sources          the hand-written C++ in place (a build outside this directory)
    gen_kernel       the kernel boundary (gen/poly.hpp, gen/poly.cpp); the body is
                     the hand-written poly_body_impl.tpp
    csim             Vitis C simulation, every scenario, hand-written poly_tb.cpp
    check_csim       ...compare
    csynth           C synthesis, then RTL co-simulation of the timing scenario
    inspect_synth    loops, II and resources from the synthesis report
    check_cosim      the co-simulated response, compared
    extract_cosim_timing / validate_timing   cosim cycles vs the pysim estimate
    summary          every check, the synthesis report and the timing verdict

Every check compares against the same expected response, computed from each scenario's
intent (scenarios.py) rather than from any implementation.
"""
from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.build.cosim_steps import ExtractCosimTimingStep, ValidateTimingStep
from waveflow.build.hwcodegen_steps import HlsCodegenStep
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.simulation.logger import Logger
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import read_bursts, write_bursts

try:
    from examples.stream_inband import scenarios as S
    from examples.stream_inband.poly import (
        SCHEMA_CLASSES, WORD_BW_SUPPORTED, CoeffArray, Float32, PolyAccel, PolyTB,
        connect, poly_stream_model,
    )
except ModuleNotFoundError:  # run from inside the example directory
    import scenarios as S  # type: ignore[no-redef]
    from poly import (  # type: ignore[no-redef]
        SCHEMA_CLASSES, WORD_BW_SUPPORTED, CoeffArray, Float32, PolyAccel, PolyTB,
        connect, poly_stream_model,
    )

_SOURCE_DIR = Path(__file__).resolve().parent

#: The hand-written sources Vitis needs beside the generated ones.  A build in another
#: directory (the tests build in a temporary one) gets a copy.
HAND_WRITTEN = ("run.tcl", "poly_tb.cpp", "poly_body_impl.tpp")


def _ensure_sources(root: Path) -> None:
    for name in HAND_WRITTEN:
        if not (root / name).exists():
            shutil.copy(_SOURCE_DIR / name, root / name)


_STUB_MARK = "TODO: implement body"


def _require_real_body(root: Path) -> None:
    """Refuse to simulate the generated stub.

    The generator writes ``poly_body_impl.tpp`` only when it is missing, as a ``// TODO``
    stub with an empty body.  Simulating that is not an error to Vitis -- the kernel just
    does nothing -- so the symptom is every scenario failing at once.  Say what it is.
    """
    body = root / "poly_body_impl.tpp"
    if _STUB_MARK in body.read_text(encoding="utf-8"):
        raise RuntimeError(f"{body} is the generated stub, not the kernel body: copy the "
                           f"hand-written poly_body_impl.tpp into the build directory")


@dataclass(kw_only=True)
class SourcesStep(BuildStep):
    """Place the hand-written C++ in the build directory, before anything is generated.

    In the example's own directory they are already there.  A build elsewhere (the tests
    build in a temporary directory) gets a copy -- and it must come before ``gen_kernel``,
    or the generator writes its stub in place of the body and the copy then (correctly)
    refuses to overwrite it.
    """
    description = "Copy the hand-written body, testbench and Tcl into the build directory."
    consumes = ["poly_source"]
    produces = {"kernel_sources": Path("poly_body_impl.tpp")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        _ensure_sources(config.root_dir)
        return {"kernel_sources": config.root_dir / "poly_body_impl.tpp"}


def _write_status(path: Path, status: dict) -> None:
    path.write_text(json.dumps(status) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Python: scenarios, the pure model, pysim
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class ScenariosStep(BuildStep):
    description = "Write every scenario's stimulus and expected response (scenarios.py)."
    consumes = ["poly_source", "scenarios_source"]
    produces = {"data_dir": Path("data"), "scenario_list": Path("data/scenarios.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        data = config.root_dir / "data"
        S.write_scenarios(data)
        return {"data_dir": data, "scenario_list": data / "scenarios.txt"}


@dataclass(kw_only=True)
class ModelStep(BuildStep):
    description = "Run the pure bit-exact model (poly_stream_model) on every scenario."
    consumes = ["scenario_list"]
    produces = {"model_done": Path("results/model_done.txt")}
    params = {}

    def run(self, config: BuildConfig, scenario_list, **_) -> dict:
        data = config.root_dir / "data"
        names = Path(scenario_list).read_text(encoding="utf-8").split()
        for name in names:
            d = data / name
            coeffs = CoeffArray().read_uint32_file(d / "coeffs.bin").val
            res = poly_stream_model(read_bursts(d / "in"), coeffs)
            write_bursts(res.out, d / "model")
            _write_status(d / "model" / "status.json", res.status())
        done = config.root_dir / "results" / "model_done.txt"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("\n".join(names) + "\n", encoding="utf-8")
        return {"model_done": done}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """pysim: the module's Python body with its timing model, on the well-formed scenarios.

    The timing scenario's event log becomes the cycle estimate cosim is checked against.
    """
    description = "Run pysim on the well-formed scenarios; log the timing scenario."
    consumes = ["scenario_list", "poly_source"]
    produces = {"pysim_done": Path("results/pysim_done.txt"), "log": Path("results/sim_log.csv")}
    params = {"clk_freq": 100e6, "unroll_factor": 1}

    def run(self, config: BuildConfig, clk_freq, unroll_factor, **_) -> dict:
        data = config.root_dir / "data"
        log_path = config.root_dir / "results" / "sim_log.csv"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        for name in S.WELL_FORMED:
            d = data / name
            sim = Simulation()
            clk = Clock(freq=clk_freq)
            logger = (Logger(name="poly_log", sim=sim, file_path=log_path, fields=["event", "job"])
                      if name == "timing" else None)
            accel = PolyAccel(name="poly_accel", sim=sim, clk=clk, unroll_factor=unroll_factor,
                              **({"logger": logger} if logger else {}))
            tb = PolyTB(name="poly_tb", sim=sim, stimulus=d / "in",
                        coeffs=CoeffArray().read_uint32_file(d / "coeffs.bin").val,
                        n_out=len(read_bursts(d / "expected")))
            connect(sim, tb, accel, clk)
            sim.run_sim()
            write_bursts(tb.out, d / "pysim")
            _write_status(d / "pysim" / "status.json", tb.status)
        done = config.root_dir / "results" / "pysim_done.txt"
        done.write_text("\n".join(S.WELL_FORMED) + "\n", encoding="utf-8")
        return {"pysim_done": done, "log": log_path}


@dataclass(kw_only=True)
class ExtractPyTimingStep(BuildStep):
    """pysim's cycle count for the timing scenario: first sample read to last sample written."""
    description = "Extract the timing scenario's cycle count from the pysim event log."
    consumes = ["log"]
    produces = {"py_timing": Path("results/py_timing.json")}
    params = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, log, clk_freq, **_) -> dict:
        events: dict[str, float] = {}
        with open(log, newline="") as f:
            for row in csv.DictReader(f):
                events.setdefault(row["event"], float(row["time"]))
        t0, t1 = events.get("samp_read_begin"), events.get("samp_out_write_end")
        if t0 is None or t1 is None:
            raise RuntimeError(f"missing timing events in {log}: {sorted(events)}")
        out = config.root_dir / "results" / "py_timing.json"
        out.write_text(json.dumps({
            "transaction_cycles": int(round((t1 - t0) * clk_freq)),
            "transaction_seconds": t1 - t0,
            "clk_freq": float(clk_freq),
            "source": "py_sim",
            "events": {"samp_read_begin": t0, "samp_out_write_end": t1},
        }, indent=2), encoding="utf-8")
        return {"py_timing": out}


@dataclass(kw_only=True)
class CheckStep(BuildStep):
    """Compare one stage's recorded responses with the expected ones (scenarios.check)."""
    stage: str
    done_artifact: str
    only: tuple[str, ...] | None = None
    description = "Compare a stage's responses with the expected responses."
    params = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.done_artifact, "scenario_list"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"check_{self.stage}": Path(f"results/check_{self.stage}.json")}

    def run(self, config: BuildConfig, **_) -> dict:
        report = S.check(config.root_dir / "data", self.stage,
                         list(self.only) if self.only else None)
        out = config.root_dir / "results" / f"check_{self.stage}.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        failed = {k: v for k, v in report.items() if v}
        for name, problems in report.items():
            print(f"    {self.stage:6s} {name:12s} {'PASS' if not problems else 'FAIL'}")
            for p in problems:
                print(f"        {p}")
        if failed:
            raise RuntimeError(f"{self.stage}: {len(failed)} scenario(s) differ from the "
                               f"expected response: {sorted(failed)}")
        return {f"check_{self.stage}": out}


# ---------------------------------------------------------------------------
# Vitis: headers, the kernel boundary, csim, csynth + cosim
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class HlsGenIncludeStep(BuildStep):
    description = "Generate the schema headers, serializers, and stream/testbench helpers."
    consumes = ["poly_source"]
    params = {}
    include_dir: str = "include"

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"include_dir": Path(self.include_dir)}

    def run(self, config: BuildConfig, **_) -> dict:
        inner = BuildDag()
        inner.add(StreamUtilsStep(output_dir=self.include_dir))
        for cls in SCHEMA_CLASSES:
            inner.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED,
                                     include_dir=self.include_dir))
        inner.add(ArrayUtilsStep(Float32, WORD_BW_SUPPORTED))
        failed = [n for n, r in inner.run(config).items() if not r.success]
        if failed:
            raise RuntimeError(f"header generation failed: {failed}")
        return {"include_dir": config.root_dir / self.include_dir}


def _run_vitis(config: BuildConfig, stage: str, live_output: bool, clk_freq: float) -> None:
    _ensure_sources(config.root_dir)
    # The stage goes in the environment: vitis-run 2025.1 has no --tclargs.
    env = {"WAVEFLOW_POLY_STAGE": stage, "WAVEFLOW_POLY_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
    try:
        result = toolchain.run_vitis_hls(config.root_dir / "run.tcl", work_dir=config.root_dir,
                                         capture_output=not live_output, env=env)
    except Exception as exc:  # CalledProcessError carries the Vitis log
        out = getattr(exc, "stdout", "") or ""
        raise RuntimeError(f"Vitis {stage} failed: {exc}\n{out[-3000:]}") from exc
    if not live_output and result.stdout:
        print(result.stdout[-2000:])


@dataclass(kw_only=True)
class CSimStep(BuildStep):
    description = "Vitis C simulation of every scenario, with the hand-written poly_tb.cpp."
    consumes = ["poly_cpp", "poly_hpp", "poly_body_impl", "include_dir", "scenario_list"]
    produces = {"csim_done": Path("results/csim_done.txt")}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, scenario_list, live_output, clk_freq, **_) -> dict:
        _require_real_body(config.root_dir)
        names = Path(scenario_list).read_text(encoding="utf-8").split()
        for name in names:
            (config.root_dir / "data" / name / "csim").mkdir(parents=True, exist_ok=True)
        _run_vitis(config, "csim", live_output, clk_freq)
        done = config.root_dir / "results" / "csim_done.txt"
        done.write_text("\n".join(names) + "\n", encoding="utf-8")
        return {"csim_done": done}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    description = "Vitis C synthesis, then RTL co-simulation of the timing scenario."
    consumes = ["poly_cpp", "poly_hpp", "poly_body_impl", "include_dir", "check_csim"]
    produces = {"report_dir": Path("waveflow_poly_proj/solution1"),
                "cosim_done": Path("results/cosim_done.txt")}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        (config.root_dir / "data" / "timing" / "cosim").mkdir(parents=True, exist_ok=True)
        _run_vitis(config, "synth", live_output, clk_freq)
        done = config.root_dir / "results" / "cosim_done.txt"
        done.write_text("timing\n", encoding="utf-8")
        return {"report_dir": config.root_dir / "waveflow_poly_proj" / "solution1",
                "cosim_done": done}


@dataclass(kw_only=True)
class InspectSynthStep(BuildStep):
    description = "Parse the C-synthesis report: loop II, latency and resources."
    consumes = ["report_dir"]
    produces = {"loop_df": Path("results/loop_df.csv"), "res_df": Path("results/res_df.csv")}
    params = {}

    def run(self, config: BuildConfig, report_dir, **_) -> dict:
        from waveflow.utils.csynthparse import CsynthParser

        parser = CsynthParser(sol_path=str(report_dir))
        parser.get_loop_pipeline_info()
        parser.get_resources()
        print(parser.loop_df.to_string() if not parser.loop_df.empty else "(no loops)")
        print(parser.res_df.to_string() if not parser.res_df.empty else "(no resources)")
        bad = parser.loop_df[parser.loop_df["PipelineII"].apply(
            lambda v: isinstance(v, (int, np.integer)) and v > 1)] if not parser.loop_df.empty else []
        if len(bad):
            raise RuntimeError(f"loops with PipelineII > 1:\n{bad.to_string()}")
        out = config.root_dir / "results"
        parser.loop_df.to_csv(out / "loop_df.csv", index=False)
        parser.res_df.to_csv(out / "res_df.csv", index=False)
        return {"loop_df": out / "loop_df.csv", "res_df": out / "res_df.csv"}


@dataclass(kw_only=True)
class SummaryStep(BuildStep):
    """Everything in one place, and the target that runs every check.

    ``--through`` runs only a step's ancestors, so a pipeline ending at the timing check
    would skip the cosim response check and the synthesis report.  This step depends on
    all of them and writes ``results/summary.json``.
    """
    description = "Collect every check, the synthesis report and the timing verdict."
    consumes = ["check_model", "check_pysim", "check_csim", "check_cosim", "loop_df", "res_df",
                "timing_verdict"]
    produces = {"summary": Path("results/summary.json")}
    params = {}

    def run(self, config: BuildConfig, **art) -> dict:
        def load(key):
            return json.loads(Path(art[key]).read_text(encoding="utf-8"))
        summary = {
            "checks": {k.removeprefix("check_"): {n: (not v) for n, v in load(k).items()}
                       for k in ("check_model", "check_pysim", "check_csim", "check_cosim")},
            "timing": load("timing_verdict"),
            "loops": Path(art["loop_df"]).read_text(encoding="utf-8"),
            "resources": Path(art["res_df"]).read_text(encoding="utf-8"),
        }
        out = config.root_dir / "results" / "summary.json"
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return {"summary": out}


def build_poly_dag() -> BuildDag:
    """The whole pipeline.  Parameters come from ``BuildConfig.params``: ``clk_freq``,
    ``unroll_factor`` and ``live_output``."""
    dag = BuildDag()
    dag.add(SourceStep(artifact="poly_source", path=_SOURCE_DIR / "poly.py",
                       description="Schemas, the pure model, the module and the pysim testbench."))
    dag.add(SourceStep(artifact="scenarios_source", path=_SOURCE_DIR / "scenarios.py",
                       description="The scenarios, their expected responses, and the checker."))

    # Python: the model and pysim, checked against the expected responses.
    dag.add(ScenariosStep(name="scenarios"))
    dag.add(ModelStep(name="py_model"))
    dag.add(CheckStep(name="check_model", stage="model", done_artifact="model_done"))
    dag.add(PySimStep(name="py_sim"))
    dag.add(CheckStep(name="check_pysim", stage="pysim", done_artifact="pysim_done",
                      only=S.WELL_FORMED))
    dag.add(ExtractPyTimingStep(name="extract_py_timing"))

    # Code generation: headers, and the kernel boundary around the hand-written body.
    dag.add(HlsGenIncludeStep(name="gen_include"))
    dag.add(SourcesStep(name="sources"))
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=PolyAccel,
                           source_artifact="kernel_sources", output_dir="gen", impl_dir="."))

    # Vitis: csim on every scenario, then synthesis and cosim of the timing scenario.
    dag.add(CSimStep(name="csim"))
    dag.add(CheckStep(name="check_csim", stage="csim", done_artifact="csim_done"))
    dag.add(CSynthStep(name="csynth"))
    dag.add(InspectSynthStep(name="inspect_synth"))
    dag.add(CheckStep(name="check_cosim", stage="cosim", done_artifact="cosim_done",
                      only=("timing",)))
    dag.add(ExtractCosimTimingStep(name="extract_cosim_timing", top="poly",
                                   report_dir_artifact="report_dir"))
    dag.add(ValidateTimingStep(name="validate_timing", py_timing_artifact="py_timing",
                               cosim_timing_artifact="cosim_timing", tolerance_cycles=20))
    dag.add(SummaryStep(name="summary"))
    return dag


def main() -> None:
    run_dag_cli(
        build_poly_dag,
        description="Build the streaming polynomial accelerator.",
        default_through="check_pysim",
        root_dir=_SOURCE_DIR,
        extra_args=[
            (("--clk-freq",), {"type": float, "default": 100e6, "metavar": "HZ"}),
            (("--unroll-factor",), {"type": int, "default": 1}),
            (("--live-output",), {"action": "store_true"}),
        ],
        params_from_args=lambda a: {"clk_freq": a.clk_freq, "unroll_factor": a.unroll_factor,
                                    "live_output": a.live_output},
    )


if __name__ == "__main__":
    main()
