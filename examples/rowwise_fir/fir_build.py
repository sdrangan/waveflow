"""fir_build.py — assemble + Vitis-validate the matrix-LT FIR m_axi top.

This is the codegen side of the matrix-LT FIR accelerator (the
``exec_model=hook`` path of plans/load_compute_store.md).  The top / hpp / tb / tcl
have **no codegen-time parameters** — they are fixed for this kernel — so they live as
**plain static source files** (``fir_top.cpp`` / ``fir.hpp`` / ``fir_tb.cpp`` /
``run.tcl``, authored alongside ``fir_dataflow.tpp``) and the ``codegen`` step simply
**copies** them into ``gen/`` (exactly the way ``fir_dataflow.tpp`` is copied — no
templating, no substitution, no Jinja2).  The thin m_axi top ``void fir(...)`` calls the
hand-written DATAFLOW kernel core ``fir_dataflow::fir_accel_core`` (``fir_dataflow.tpp`` =
the validated Phase 1 sandbox ``fir_accel``).  Control rides an AXI-stream (the command on
``s_in``, the response on ``m_out``), like ``shared_mem``; the data stays on ``m_mem``.

The generated kernel is csim'd (and optionally csynth/cosim'd) **bit-exact**
against the ONE shared ``fir_golden``, reusing the Phase-1 fixtures.

The build is a discoverable :class:`~waveflow.build.build.BuildDag` of named steps —
``codegen`` / ``pysim`` / ``pytiming`` / ``csim`` / ``cosim`` / ``calibrate`` — driven by
the shared introspection CLI (:func:`~waveflow.build.cli.run_dag_cli`), mirroring the
regmap / shared_mem examples (no hand-rolled per-example ``main()``).  (FIR is the first
example with static-file codegen; mmqueue / shared_mem still render their tops — an
intended, transitional divergence.)

Run with the project venv::

    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_build.py --list-steps
    # gates (no Vitis):  ... fir_build.py --through calibrate
    # C-sim / cosim (Vitis):  ... fir_build.py --through csim   /   --through cosim
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep  # noqa: E402
from waveflow.build.cli import run_dag_cli  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

try:
    from examples.rowwise_fir.fir_golden import T, fir_golden
except ModuleNotFoundError:
    sys.path.insert(0, str(HERE))
    from fir_golden import T, fir_golden  # type: ignore[no-redef]

GEN_DIR = HERE / "gen"
PART = "xc7z020clg484-1"

# --- static codegen sources (copied verbatim into gen/) ----------------------
# The FIR top / hpp / tb / tcl have no codegen-time parameters, so they are plain
# static source files (authored alongside fir_dataflow.tpp) that the codegen step
# copies into gen/ — no templating, no substitution.  Keys are the gen/ filenames.
STATIC_SOURCES = {
    "fir.cpp": HERE / "fir_top.cpp",
    "fir.hpp": HERE / "fir.hpp",
    "fir_tb.cpp": HERE / "fir_tb.cpp",
    "run.tcl": HERE / "run.tcl",
    "fir_dataflow.tpp": HERE / "fir_dataflow.tpp",
    "fir_respond_impl.tpp": HERE / "fir_respond_impl.tpp",
}


def write_fixture(data_dir: Path, n_rows: int, n_cols: int, seed: int = 0) -> dict:
    """Generate [X, h, Y_golden] fixtures + meta.json + dims.tcl (exact mem depth)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((n_rows, n_cols)).astype(np.float32)
    h = rng.standard_normal(T).astype(np.float32)
    Y = fir_golden(X, h)
    X.astype("<f4").tofile(data_dir / "X.bin")
    h.astype("<f4").tofile(data_dir / "h.bin")
    Y.astype("<f4").tofile(data_dir / "Y_golden.bin")
    out_len = n_cols - T + 1
    meta = {"n_rows": n_rows, "n_cols": n_cols, "T": T, "out_len": out_len,
            "dtype": "float32", "byte_order": "little", "layout": "row_major"}
    (data_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    depth = n_rows * n_cols + T + n_rows * out_len
    (data_dir / "dims.tcl").write_text(f"set WF_FIR_MEM_DEPTH {depth}\n", encoding="utf-8")
    return meta


def generate(n_rows: int = 4, n_cols: int = 64, seed: int = 0) -> Path:
    """Copy the static codegen sources into ``gen/`` + write the C-sim fixtures."""
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    for dst_name, src in STATIC_SOURCES.items():
        shutil.copy(src, GEN_DIR / dst_name)
    write_fixture(GEN_DIR / "data", n_rows, n_cols, seed)
    return GEN_DIR


# --- Vitis runner (shared by the csim / cosim steps) -------------------------
def _run_vitis(env: dict | None) -> tuple[bool, str]:
    """Drive ``gen/run.tcl`` once; return ``(passed, combined_output)``."""
    res = toolchain.run_vitis_hls_result(GEN_DIR / "run.tcl", work_dir=GEN_DIR,
                                         capture_output=True, env=env or None)
    out = (res.get("stdout") or "") + (res.get("stderr") or "")
    return ("WAVEFLOW_SUCCESS" in out), out


# --- BuildDag steps ----------------------------------------------------------
@dataclass(kw_only=True)
class CodegenStep(BuildStep):
    """Copy the static m_axi FIR top + TB + tcl + hpp (+ the ``fir_dataflow.tpp`` core)
    into ``gen/`` and write the C-sim fixtures (plain file copy — no templating)."""

    description = "Copy gen/fir.{hpp,cpp}, gen/fir_tb.cpp, gen/run.tcl + write the C-sim fixtures."
    consumes = ["fir_source", "fir_dataflow_tpp", "fir_top_cpp", "fir_hpp", "fir_tb_cpp",
                "fir_run_tcl", "fir_respond_tpp"]
    produces = {
        "gen_dir": Path("gen"),
        "kernel_hpp": Path("gen/fir.hpp"),
        "kernel_cpp": Path("gen/fir.cpp"),
        "tb_cpp": Path("gen/fir_tb.cpp"),
        "run_tcl": Path("gen/run.tcl"),
    }
    params = {"n_rows": 4, "n_cols": 64, "seed": 0}

    def run(self, config: BuildConfig, n_rows, n_cols, seed, **_) -> dict[str, Any]:
        gen = generate(n_rows, n_cols, seed)
        return {"gen_dir": gen, "kernel_hpp": gen / "fir.hpp", "kernel_cpp": gen / "fir.cpp",
                "tb_cpp": gen / "fir_tb.cpp", "run_tcl": gen / "run.tcl"}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the block-fidelity :class:`~examples.rowwise_fir.fir_sim.FIRSim` and check the
    output Y **bit-exact** against the ONE shared ``fir_golden``."""

    description = "Run the block-fidelity FIRSim and check Y bit-exact vs the shared golden."
    consumes = ["fir_source"]
    produces = {"pysim_summary": Path("results/pysim_summary.json")}
    params = {"n_rows": 4, "n_cols": 64, "seed": 0}

    def run(self, config: BuildConfig, n_rows, n_cols, seed, **_) -> dict[str, Any]:
        from examples.rowwise_fir.fir_sim import FIRSim, check_golden, make_specs

        specs = make_specs([(n_rows, n_cols)], seed=seed)
        ok = check_golden(specs, FIRSim(specs).run())
        out = config.root_dir / "results" / "pysim_summary.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"n_rows": n_rows, "n_cols": n_cols, "bit_exact": bool(ok)},
                                  indent=2) + "\n", encoding="utf-8")
        if not ok:
            raise RuntimeError(f"FIR pysim golden mismatch at {n_rows}x{n_cols}")
        return {"pysim_summary": out}


@dataclass(kw_only=True)
class PyTimingStep(BuildStep):
    """Extract the sim-only bus-visible stage timeline (anchored at ``cmd_arrive``) — the
    sim side of the fir_validate residual, with no Vitis dependency."""

    description = "Extract the sim-only bus-visible stage timeline (cycles, anchored at cmd_arrive)."
    consumes = ["fir_source"]
    produces = {"py_timing": Path("results/py_timing.json")}
    params = {"n_rows": 4, "n_cols": 64}

    def run(self, config: BuildConfig, n_rows, n_cols, **_) -> dict[str, Any]:
        from examples.rowwise_fir.fir_sim import FIRSim, make_specs

        sim = FIRSim(make_specs([(n_rows, n_cols)]))
        sim.run()
        ev = {e["event"]: e["t"] for e in sim.accel.events if e["tx_id"] == 0}
        clk_ns = 10.0

        def off(e):  # offset from cmd_arrive, in cycles
            return (ev[e] - ev["cmd_arrive"]) / (clk_ns * 1e-9)

        def span(a, b):  # a->b duration, in cycles
            return (ev[b] - ev[a]) / (clk_ns * 1e-9)

        timing = {
            "n_rows": n_rows, "n_cols": n_cols,
            "load_span_cyc": span("load_begin", "load_end"),
            "store_span_cyc": span("store_begin", "store_end"),
            "whole_kernel_cyc": off("store_end"),
            "store_begin_off_cyc": off("store_begin"),
            "load_end_off_cyc": off("load_end"),
        }
        out = config.root_dir / "results" / "py_timing.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(timing, indent=2) + "\n", encoding="utf-8")
        return {"py_timing": out}


@dataclass(kw_only=True)
class CSimStep(BuildStep):
    """Vitis HLS C-simulation of the generated FIR top, bit-exact vs ``Y_golden.bin``."""

    description = "Vitis HLS C-sim of the generated FIR top (bit-exact vs Y_golden)."
    consumes = ["kernel_cpp", "tb_cpp", "run_tcl", "kernel_hpp", "fir_dataflow_tpp",
                "fir_respond_tpp"]
    produces = {"csim_verdict": Path("results/csim_verdict.json")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        ok, out = _run_vitis(env=None)
        verdict = config.root_dir / "results" / "csim_verdict.json"
        verdict.parent.mkdir(parents=True, exist_ok=True)
        verdict.write_text(json.dumps({"passed": ok}, indent=2) + "\n", encoding="utf-8")
        if not ok:
            raise RuntimeError("FIR csim failed:\n" + out[-2000:])
        return {"csim_verdict": verdict}


@dataclass(kw_only=True)
class CosimStep(BuildStep):
    """Vitis HLS C-synth + RTL co-simulation of the generated FIR top (gated on csim)."""

    description = "Vitis HLS C-synth + RTL co-simulation of the generated FIR top."
    consumes = ["kernel_cpp", "tb_cpp", "run_tcl", "kernel_hpp", "fir_dataflow_tpp",
                "fir_respond_tpp", "csim_verdict"]
    produces = {"cosim_dir": Path("gen/fir_gen_proj")}
    params = {"trace_level": "none"}

    def run(self, config: BuildConfig, trace_level, **_) -> dict[str, Any]:
        env = {"WAVEFLOW_ROWWISE_FIR_COSIM": "1", "WAVEFLOW_ROWWISE_FIR_TRACE_LEVEL": trace_level}
        ok, out = _run_vitis(env=env)
        if not ok:
            raise RuntimeError("FIR cosim failed:\n" + out[-2000:])
        return {"cosim_dir": GEN_DIR / "fir_gen_proj"}


@dataclass(kw_only=True)
class CalibrateStep(BuildStep):
    """Fit the physical timing model from the committed cosim grid and run the sim-vs-cosim
    gates (the single-command + untrained-n_col residuals).  No Vitis: the grid is committed
    and the gates re-run the block-fidelity sim against it."""

    description = "Fit the timing model from the committed cosim grid + run the sim-vs-cosim gates."
    consumes = ["fir_source", "cosim_grid"]
    produces = {"calibration": Path("results/fir_calibration.json")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        from examples.rowwise_fir import fir_calibrate

        calib = fir_calibrate.fit_and_validate()
        calib["validation"] = fir_calibrate.validate(calib)
        fir_calibrate.CALIB_JSON.write_text(json.dumps(calib, indent=2) + "\n", encoding="utf-8")
        return {"calibration": fir_calibrate.CALIB_JSON}


def build_fir_dag() -> BuildDag:
    """Assemble the matrix-LT FIR BuildDag (codegen / pysim / pytiming / csim / cosim / calibrate)."""
    dag = BuildDag()
    dag.add(SourceStep(artifact="fir_source", path=HERE / "fir.py"))
    dag.add(SourceStep(artifact="fir_dataflow_tpp", path=HERE / "fir_dataflow.tpp"))
    dag.add(SourceStep(artifact="fir_top_cpp", path=HERE / "fir_top.cpp"))
    dag.add(SourceStep(artifact="fir_hpp", path=HERE / "fir.hpp"))
    dag.add(SourceStep(artifact="fir_tb_cpp", path=HERE / "fir_tb.cpp"))
    dag.add(SourceStep(artifact="fir_run_tcl", path=HERE / "run.tcl"))
    dag.add(SourceStep(artifact="fir_respond_tpp", path=HERE / "fir_respond_impl.tpp"))
    dag.add(SourceStep(artifact="cosim_grid", path=HERE / "results" / "cosim_grid.json"))
    dag.add(CodegenStep(name="codegen"))
    dag.add(PySimStep(name="pysim"))
    dag.add(PyTimingStep(name="pytiming"))
    dag.add(CSimStep(name="csim"))
    dag.add(CosimStep(name="cosim"))
    dag.add(CalibrateStep(name="calibrate"))
    return dag


def main() -> None:
    run_dag_cli(
        build_fir_dag,
        description="Run the matrix-LT FIR example.",
        default_through="calibrate",
        root_dir=HERE,
        extra_args=[
            (("--n-rows",), {"type": int, "default": 4}),
            (("--n-cols",), {"type": int, "default": 64}),
            (("--seed",), {"type": int, "default": 0}),
            (("--trace-level",), {"default": "none", "choices": ["none", "port", "all"],
                                  "help": "RTL cosim VCD trace level (Vitis cosim step)."}),
        ],
        params_from_args=lambda a: {
            "n_rows": a.n_rows, "n_cols": a.n_cols, "seed": a.seed, "trace_level": a.trace_level,
        },
    )


if __name__ == "__main__":
    main()
