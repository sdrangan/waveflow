"""fir_build.py — generate + Vitis-validate the matrix-LT FIR m_axi top.

This is the codegen side of the matrix-LT FIR accelerator (the
``exec_model=hook`` path of plans/load_compute_store.md).  Following VMAC's
*primary* codegen pattern (``vmac_build.render_top``, not the run_proc extractor —
see fir.py's docstring for why), it hand-rolls a thin m_axi top

    void fir(float* gmem, int x_off, int y_off, int h_off, int n_rows, int n_cols)

that calls the hand-written DATAFLOW kernel core ``fir_dataflow::fir_accel_core``
(``fir_dataflow.tpp`` = the validated Phase 1 sandbox ``fir_accel``).  The command
fields are s_axilite scalars (the AXIMMQueue ring is sim-only; the synthesized
kernel takes the command as scalars, exactly as VMAC bakes its command).

The generated kernel is csim'd (and optionally csynth/cosim'd) **bit-exact**
against the ONE shared ``fir_golden``, reusing the Phase-1 fixtures.

The build is a discoverable :class:`~waveflow.build.build.BuildDag` of named steps —
``codegen`` / ``pysim`` / ``pytiming`` / ``csim`` / ``cosim`` / ``calibrate`` — driven by
the shared introspection CLI (:func:`~waveflow.build.cli.run_dag_cli`), mirroring the
regmap / shared_mem examples (no hand-rolled per-example ``main()``).  The ``render_*``
functions stay as-is (moving the static wrappers to template files is a platform-wide
codegen decision, deferred to the codegen phase).

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


# --- generated C++ -----------------------------------------------------------
def render_hpp() -> str:
    return "\n".join([
        "#ifndef FIR_HPP",
        "#define FIR_HPP",
        '#include "fir_dataflow.tpp"',
        "typedef fir_dataflow::real_t real_t;",
        "#endif",
        "",
    ])


def render_top() -> str:
    """The m_axi top: single full-duplex ``gmem`` bundle + s_axilite scalar command,
    calling the hand-written DATAFLOW core.  ``*_off`` are element (float) offsets
    into ``gmem`` — X, Y, h are three logical regions of one bundle (Phase 1 proved
    single-bundle read+write is full-duplex, so this matches the row-buffered kernel
    exactly)."""
    return "\n".join([
        "// Generated matrix-LT FIR top (hand-rolled, VMAC render_top style): a single",
        "// full-duplex m_axi gmem bundle + s_axilite scalar command, wrapping the",
        "// hand-written fir_dataflow::fir_accel_core hook (= the Phase 1 sandbox kernel).",
        '#include "fir.hpp"',
        "",
        "#ifndef WF_FIR_MEM_DEPTH",
        "#define WF_FIR_MEM_DEPTH 65536",  # csynth upper bound; cosim overrides via -D
        "#endif",
        "",
        "void fir(real_t* gmem, int x_off, int y_off, int h_off, int n_rows, int n_cols) {",
        "#pragma HLS INTERFACE m_axi port=gmem offset=slave bundle=gmem "
        "max_read_burst_length=256 max_write_burst_length=256 depth=WF_FIR_MEM_DEPTH",
        "#pragma HLS INTERFACE s_axilite port=x_off",
        "#pragma HLS INTERFACE s_axilite port=y_off",
        "#pragma HLS INTERFACE s_axilite port=h_off",
        "#pragma HLS INTERFACE s_axilite port=n_rows",
        "#pragma HLS INTERFACE s_axilite port=n_cols",
        "#pragma HLS INTERFACE s_axilite port=return",
        "    fir_dataflow::fir_accel_core(gmem + x_off, gmem + y_off, gmem + h_off,",
        "                                 n_rows, n_cols);",
        "}",
        "",
    ])


def render_tb() -> str:
    """Mem-image testbench: lay [X | h | Y] into one float buffer, run the top, compare
    the Y region bit-exact against Y_golden.bin (the shared golden's output)."""
    return "\n".join([
        "// Generated-top FIR cosim/csim TB: load the Phase-1 fixtures into one mem image",
        "// (X | h | Y), run the generated fir() top, and check the Y region bit-exact vs",
        "// Y_golden.bin (produced by the ONE shared fir_golden).",
        '#include "fir.hpp"',
        "#include <cstdint>",
        "#include <cstdio>",
        "#include <cstring>",
        "#include <fstream>",
        "#include <string>",
        "#include <vector>",
        "",
        "void fir(real_t* gmem, int x_off, int y_off, int h_off, int n_rows, int n_cols);",
        "",
        "static int meta_int(const std::string& s, const std::string& key) {",
        '    const std::string n = "\\"" + key + "\\"";',
        "    size_t p = s.find(n);",
        '    if (p == std::string::npos) { std::fprintf(stderr, "meta key %s missing\\n", key.c_str()); std::exit(2); }',
        "    p = s.find(':', p);",
        "    return std::atoi(s.c_str() + p + 1);",
        "}",
        "static std::vector<float> rdf(const std::string& p, size_t n) {",
        "    std::ifstream f(p, std::ios::binary);",
        '    if (!f) { std::fprintf(stderr, "open %s\\n", p.c_str()); std::exit(2); }',
        "    std::vector<float> v(n);",
        "    f.read(reinterpret_cast<char*>(v.data()), (std::streamsize)(n * sizeof(float)));",
        "    return v;",
        "}",
        "static std::string rdt(const std::string& p) {",
        "    std::ifstream f(p, std::ios::binary);",
        '    if (!f) { std::fprintf(stderr, "open %s\\n", p.c_str()); std::exit(2); }',
        "    return std::string((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());",
        "}",
        "",
        "int main(int argc, char** argv) {",
        '    const std::string d = (argc > 1) ? argv[1] : "data";',
        '    const std::string meta = rdt(d + "/meta.json");',
        '    const int n_rows = meta_int(meta, "n_rows");',
        '    const int n_cols = meta_int(meta, "n_cols");',
        "    const int t = fir_dataflow::T;",
        "    const int out_len = n_cols - t + 1;",
        "    const int x_off = 0;",
        "    const int h_off = n_rows * n_cols;",
        "    const int y_off = h_off + t;",
        "    const int depth = y_off + n_rows * out_len;",
        "",
        '    std::vector<float> X = rdf(d + "/X.bin", (size_t)n_rows * n_cols);',
        '    std::vector<float> h = rdf(d + "/h.bin", (size_t)t);',
        '    std::vector<float> Yg = rdf(d + "/Y_golden.bin", (size_t)n_rows * out_len);',
        "    std::vector<float> mem((size_t)depth, 0.0f);",
        "    std::memcpy(&mem[x_off], X.data(), X.size() * sizeof(float));",
        "    std::memcpy(&mem[h_off], h.data(), h.size() * sizeof(float));",
        "",
        "    fir(mem.data(), x_off, y_off, h_off, n_rows, n_cols);",
        "",
        "    int bad = 0;",
        "    for (size_t i = 0; i < Yg.size(); ++i) {",
        "        uint32_t a, b;",
        "        std::memcpy(&a, &mem[y_off + i], 4);",
        "        std::memcpy(&b, &Yg[i], 4);",
        "        if (a != b) {",
        '            if (bad < 8) std::fprintf(stderr, "mismatch %zu: got %.9g exp %.9g\\n", i, mem[y_off+i], Yg[i]);',
        "            ++bad;",
        "        }",
        "    }",
        "    if (bad == 0) {",
        '        std::printf("WAVEFLOW_FIR_GEN_OK: Y == Y_golden bit-exact (n_rows=%d n_cols=%d out_len=%d)\\n",',
        "                    n_rows, n_cols, out_len);",
        "        return 0;",
        "    }",
        '    std::fprintf(stderr, "WAVEFLOW_FIR_GEN_FAIL: %d/%zu mismatch\\n", bad, Yg.size());',
        "    return 1;",
        "}",
        "",
    ])


def render_tcl() -> str:
    return "\n".join([
        "# Vitis HLS driver for the generated matrix-LT FIR top.  Mirrors the sandbox",
        "# run.tcl conventions: WAVEFLOW_SUCCESS:/WAVEFLOW_ERROR: sentinels, env-gated",
        "# cosim, exact m_axi depth (-D from dims.tcl) so cosim's mem model matches the TB.",
        "set d [file dirname [file normalize [info script]]]",
        'set data_dir [file join $d "data"]',
        "set part {%s}" % PART,
        'set depthflags ""',
        'set dims [file join $data_dir "dims.tcl"]',
        "if {[file exists $dims]} { source $dims; set depthflags \" -DWF_FIR_MEM_DEPTH=$WF_FIR_MEM_DEPTH\" }",
        "set do_cosim 0",
        "if {[info exists ::env(WAVEFLOW_ROWWISE_FIR_COSIM)]} {",
        "    set do_cosim [expr {$::env(WAVEFLOW_ROWWISE_FIR_COSIM) in {1 true TRUE yes YES}}]",
        "}",
        "set do_csynth 0",
        "if {[info exists ::env(WAVEFLOW_ROWWISE_FIR_CSYNTH)]} {",
        "    set do_csynth [expr {$::env(WAVEFLOW_ROWWISE_FIR_CSYNTH) in {1 true TRUE yes YES}}]",
        "}",
        "open_project -reset fir_gen_proj",
        "set_top fir",
        'add_files [file join $d fir.cpp] -cflags "-I$d$depthflags"',
        'add_files -tb [file join $d fir_tb.cpp] -cflags "-I$d"',
        'open_solution -reset "solution1"',
        "set_part $part",
        "create_clock -period 10",
        'if {[catch {csim_design -argv "$data_dir"} res]} { puts "WAVEFLOW_ERROR: fir csim failed."; puts $res; exit 1 }',
        'if {$do_csynth || $do_cosim} {',
        '    if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: fir csynth failed."; puts $res; exit 1 }',
        "}",
        'set trace_level "none"',
        "if {[info exists ::env(WAVEFLOW_ROWWISE_FIR_TRACE_LEVEL)]} {",
        "    set trace_level $::env(WAVEFLOW_ROWWISE_FIR_TRACE_LEVEL)",
        "}",
        "if {$do_cosim} {",
        '    if {[catch {cosim_design -argv "$data_dir" -trace_level $trace_level} res]} { puts "WAVEFLOW_ERROR: fir cosim failed."; puts $res; exit 1 }',
        '    puts "WAVEFLOW_SUCCESS: fir csim/csynth/cosim passed."',
        "} else {",
        '    puts "WAVEFLOW_SUCCESS: fir csim passed."',
        "}",
        "exit 0",
        "",
    ])


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
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    (GEN_DIR / "fir.hpp").write_text(render_hpp(), encoding="utf-8")
    (GEN_DIR / "fir.cpp").write_text(render_top(), encoding="utf-8")
    (GEN_DIR / "fir_tb.cpp").write_text(render_tb(), encoding="utf-8")
    (GEN_DIR / "run.tcl").write_text(render_tcl(), encoding="utf-8")
    shutil.copy(HERE / "fir_dataflow.tpp", GEN_DIR / "fir_dataflow.tpp")
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
    """Render the m_axi FIR top + TB + tcl + hpp and the C-sim fixtures into ``gen/``
    (the hand-rolled ``render_*`` codegen; the ``fir_dataflow.tpp`` core is copied in)."""

    description = "Render gen/fir.{hpp,cpp}, gen/fir_tb.cpp, gen/run.tcl + the C-sim fixtures."
    consumes = ["fir_source", "fir_dataflow_tpp"]
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
    consumes = ["kernel_cpp", "tb_cpp", "run_tcl", "kernel_hpp", "fir_dataflow_tpp"]
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
    consumes = ["kernel_cpp", "tb_cpp", "run_tcl", "kernel_hpp", "fir_dataflow_tpp", "csim_verdict"]
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
