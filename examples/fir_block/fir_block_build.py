"""fir_block_build.py — codegen driver for the block FIR: headers, the composite top, and its TCL.

Two kinds of artifact end up in ``include/``, and the split is the point of this file:

* **Generated** — the schema headers, the framework stream/mem-stream bodies, and (the interesting one)
  ``fir_compute_state.inc`` + ``fir_compute_types.h``, which come from the *same* ``add_state``
  registrations and ``FixedField`` formats the pysim twin uses.  Storage declarations, extents, the
  ``ARRAY_PARTITION`` pragma, and the derived accumulator format are all single-sourced from Python.
* **Hand-written and sticky** — ``fir_cmd_rx_task.h`` and ``fir_compute_task.h``, which live at the
  example root under version control and are *copied* into ``include/`` (never regenerated over).  They
  are hand-written for the same reason every other in-band framer in this tree is: constructing
  descriptors and driving a ``framed_word`` channel is not in the extractor's vocabulary.

So the arithmetic is hand-written but the **state is not**: ``fir_compute_task.h`` declares no storage
of its own, it ``#include``s what :func:`~waveflow.build.hwgen.state_decls_to_cpp` emits.  That is what
keeps the RTL's ``static`` from drifting from the ``HwState`` the Python model mutates.

The steps are a :class:`BuildDag` driven by :func:`run_dag_cli`, like the other examples'
``*_build.py`` — so this runs through the standard CLI with ``--through`` / ``--list-steps`` /
``--status`` rather than a bespoke ``__main__``::

    python -m examples.fir_block.fir_block_build --through pysim       # default; no toolchain
    python -m examples.fir_block.fir_block_build --list-steps
    python -m examples.fir_block.fir_block_build --through codegen_tb  # DUT + XSI harness
    python -m examples.fir_block.fir_block_build --through csynth      # needs Vitis
    python -m examples.fir_block.fir_block_build --through csynth --unroll-lane --samp-w 8

The RTL rung (drive the synthesized top through XSI and check bit-exactness) is the ``-m xsi`` gate in
``tests/examples/test_fir_block_xsi.py``: it needs Vivado xsim plus a prior ``csynth``, and is
orchestrated there rather than duplicated here.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.toolchain import toolchain
from waveflow.build.composite_gen import (
    GEN_DIR,
    INCLUDE_DIR,
    composite_top_spec,
    render_ports_h,
    render_rtl_f,
    render_tcl,
    render_top,
)
from waveflow.build.elaborate import elaborate
from waveflow.build.hwgen import state_decls_to_cpp
from waveflow.build.streamutils import MemMgrStep, MemStreamStep, StreamUtilsStep, XsiHarnessStep
from waveflow.hw.arrayutils import _array_utils_namespace, gen_array_utils
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.hw.fixpoint import fixed_sum, mult
from waveflow.hw.mem_stream import WORD_BW_SUPPORTED

from examples.fir_block.fir_block import (
    DEFAULT_NTAP,
    DEFAULT_SAMP_I,
    DEFAULT_SAMP_W,
    FRAMED_SCHEMAS,
    MEM_DW,
    SCHEMA_CLASSES,
    FirBlock,
    FirCompute,
    FirOp,
    _as_fixed,
    samp_type,
)

HERE = Path(__file__).resolve().parent

#: Hand-written task bodies, sticky at the example root.  Copied into ``include/``; never generated.
HAND_WRITTEN_TASKS = ("fir_cmd_rx_task.h", "fir_compute_serial_task.h",
                      "fir_compute_unroll_task.h")

TOP = "fir_block"


def acc_format(ntap: int, samp_w: int, samp_i: int):
    """The accumulator format, **derived** by running the format algebra — never hand-typed.

    ``mult`` gives ``<2W, 2I>`` and ``fixed_sum`` over the ``T``-sample window adds ``ceil(log2 T)``
    integer bits.  Asking the Python types for the answer (rather than writing ``2*W + 5`` into a C++
    header) is what makes the RTL accumulator bit-exact with the pysim one by construction: if the
    algebra ever changes, this moves with it."""
    samp = samp_type(samp_w, samp_i)
    prod = mult(_as_fixed(np.zeros((1, int(ntap)), dtype=np.int64), samp),
                _as_fixed(np.zeros(int(ntap), dtype=np.int64), samp))
    return fixed_sum(prod, axis=1).element_type.get_format()


def render_types_h(ntap: int, samp_w: int, samp_i: int) -> str:
    """``fir_types.h`` — the accumulator type, the tap count, and the array-utils namespace alias.

    The generated array-utils namespace is named after the *specialized element* (``Fixed16_2`` ->
    ``fixed16_2_array_utils``), so it moves whenever the width parameters move.  Aliasing it here to a
    stable ``fir_au`` is what lets the two hand-written bodies name the (de)serialization routines
    without being regenerated per configuration."""
    samp = samp_type(samp_w, samp_i)
    acc = acc_format(ntap, samp_w, samp_i)
    acc_cpp = (f"ap_fixed<{acc.W}, {acc.int_bits}, {acc.q_mode.value}, {acc.o_mode.value}>"
               if acc.signed else
               f"ap_ufixed<{acc.W}, {acc.int_bits}, {acc.q_mode.value}, {acc.o_mode.value}>")
    au_ns = _array_utils_namespace(samp)
    au_header = f"{au_ns}.h"
    return f"""\
#ifndef WAVEFLOW_FIR_TYPES_H
#define WAVEFLOW_FIR_TYPES_H
// GENERATED by waveflow (examples/fir_block/fir_block_build.py::render_types_h).
// DO NOT EDIT: regenerate instead.
//
// The sample format is one HwParam pair (W, I); the accumulator is DERIVED from it by the same
// format algebra the pysim twin runs -- fixpoint.mult gives <2W, 2I> and fixpoint.fixed_sum over the
// {ntap}-sample window grows the integer bits by ceil(log2 {ntap}).  Nothing here is hand-sized, which is
// why the RTL accumulator is bit-exact with the Python one rather than approximately agreeing.
#include <ap_fixed.h>
#include "{au_header}"

//: The generated (de)serialization routines for this sample format, under a name the
//: hand-written task bodies can rely on across width changes.
namespace fir_au = {au_ns};

namespace fir_types {{
    typedef {acc_cpp} acc_t;    // full precision: no rounding until the final quantize
    static const int NTAP   = {int(ntap)};
    static const int SAMP_W = {int(samp_w)};
}}

#endif  // WAVEFLOW_FIR_TYPES_H
"""


def render_state_inc(comp) -> str:
    """``fir_compute_state.inc`` — the ``add_state`` storage, included **inside** the task body.

    Function scope is not an accident: it is the site ``add_state`` emits for in Flow 2 (a task has no
    "before the loop", so the declaration *is* the initialization site), and a ``static`` there is what
    survives the runtime's re-firings.  No include guard, because it is spliced at one point."""
    return ("// GENERATED by waveflow (build/hwgen.py::state_decls_to_cpp) from FirCompute.add_state.\n"
            "// DO NOT EDIT: regenerate instead.  Included INSIDE the task body -- that is the site\n"
            "// declared state is emitted at for a free-running hls::task, and `static` there is what\n"
            "// persists across firings.  No include guard: this is spliced at exactly one point.\n"
            + state_decls_to_cpp(comp) + "\n")


def gen_headers(config: BuildConfig, mem_dwidth: int = MEM_DW, samp_w: int = DEFAULT_SAMP_W,
                samp_i: int = DEFAULT_SAMP_I) -> None:
    """Schema headers + streamutils + memmgr + the framed mem-stream bodies + the sample element's
    **array-utils** into ``include/``.

    The array-utils header is what supplies ``lane_capacity`` / ``read_framed_stream_lane`` /
    ``write_framed_stream_lane`` for the sample format, so the task bodies never hand-roll the
    packing — and its layout is the same contract ``DataArray.serialize`` uses on the Python side."""
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
    dag.add(MemStreamStep(output_dir=INCLUDE_DIR))
    for cls in SCHEMA_CLASSES:
        dag.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED, include_dir=INCLUDE_DIR,
                               framed=(cls in FRAMED_SCHEMAS)))
    dag.add(XsiHarnessStep(output_dir="xsi"))
    results = dag.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")
    gen_array_utils(samp_type(samp_w, samp_i), [int(mem_dwidth)], cfg=config,
                    streamutils_dir=INCLUDE_DIR)


def generate_dut(out_dir: Path = HERE, mem_dwidth: int = MEM_DW, ntap: int = DEFAULT_NTAP,
                 samp_w: int = DEFAULT_SAMP_W, samp_i: int = DEFAULT_SAMP_I,
                 unroll_lane: bool = False) -> Path:
    """Generate the whole DUT side: headers, the state/type headers, the composite top, its csynth
    TCL, and the XSI port map.  Runnable without the toolchain up to the csynth call."""
    out_dir = Path(out_dir)
    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config, mem_dwidth=mem_dwidth, samp_w=samp_w, samp_i=samp_i)

    inc = out_dir / INCLUDE_DIR
    inc.mkdir(parents=True, exist_ok=True)

    # The two derived-from-Python headers the hand-written body leans on.
    compute = elaborate(FirCompute, {"mem_dwidth": mem_dwidth, "ntap": ntap, "samp_w": samp_w,
                                     "samp_i": samp_i, "unroll_lane": unroll_lane},
                        name="fir_compute")
    (inc / "fir_types.h").write_text(render_types_h(ntap, samp_w, samp_i), encoding="utf-8")
    (inc / "fir_compute_state.inc").write_text(render_state_inc(compute), encoding="utf-8")

    # The sticky hand-written bodies: copied, never generated over.
    for name in HAND_WRITTEN_TASKS:
        src = out_dir / name
        if not src.exists():
            raise FileNotFoundError(f"hand-written task body missing: {src}")
        shutil.copyfile(src, inc / name)

    comp = elaborate(FirBlock, {"mem_dwidth": mem_dwidth, "ntap": ntap, "samp_w": samp_w,
                                "samp_i": samp_i, "unroll_lane": unroll_lane}, name=TOP)
    spec = composite_top_spec(comp, width=mem_dwidth)
    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part="xc7z020clg484-1", period_ns=10), encoding="utf-8")
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    print(f"generated {cpp.relative_to(out_dir)} + {spec.top_name}.tcl + xsi/{spec.top_name}_ports.h")
    return cpp


# ---------------------------------------------------------------------------
# XSI testbench codegen — the BFM harness derived from the FirBlockTB graph
# ---------------------------------------------------------------------------

from waveflow.build.composite_gen import (  # noqa: E402
    render_tb_harness,
    render_tb_main,
    render_vectors_h,
    tb_top_spec,
)


def make_xsi_tb(program=None, blk: int | None = None, ntap: int = DEFAULT_NTAP,
                samp_w: int = DEFAULT_SAMP_W, samp_i: int = DEFAULT_SAMP_I,
                mem_dwidth: int = MEM_DW, n_cycles: "int | None" = None):
    """The :class:`~examples.fir_block.fir_block_sim.FirBlockTB` the XSI testbench is generated from.

    Lazy import: the sim module imports the design, and this one imports both — keeping the import
    here means the design can be generated without pulling in the harness."""
    from waveflow.simulation.simulation import Simulation
    from examples.fir_block.fir_block_sim import DEFAULT_BLK, DEFAULT_PROGRAM, FirBlockTB

    kw = {} if n_cycles is None else {"n_cycles": int(n_cycles)}
    return FirBlockTB(name="xsi_tb", sim=Simulation(),
                      program=tuple(program or DEFAULT_PROGRAM),
                      blk=int(blk if blk is not None else DEFAULT_BLK),
                      ntap=int(ntap), samp_w=int(samp_w), samp_i=int(samp_i),
                      mem_dwidth=int(mem_dwidth), **kw)


def render_xsi_vectors(tb) -> str:
    """``fir_block_vectors.h`` — the arena size and command count the harness needs, read off the very
    graph the pysim golden runs.  The per-job offsets ride the ``vectors/s_cmd`` bundle instead of
    being baked, so the RTL stays scenario-independent."""
    from examples.fir_block.fir_block import FirDesc

    return render_vectors_h(
        f"{TOP}_vectors",
        scalars={
            "MEM_DW": int(tb.mem_dwidth),
            "MEM_NW": int(tb._nwords_tot),
            "NUM_CMDS": len(tb._steps),
            "DONE_WORDS": FirDesc.nwords_per_inst(int(tb.mem_dwidth)),
        },
        note=("Derived from the FirBlockTB graph (examples/fir_block/fir_block_sim.py) -- the same\n"
              "class the pysim golden runs.  The command words, the taps/input arena, and the golden\n"
              "ride the burst bundles under xsi/vectors/, written by write_xsi_bundles."),
    )


def write_xsi_bundles(xsi_dir: Path, program=None, blk: int | None = None, ntap: int = DEFAULT_NTAP,
                      samp_w: int = DEFAULT_SAMP_W, samp_i: int = DEFAULT_SAMP_I,
                      mem_dwidth: int = MEM_DW) -> None:
    """Write the XSI input + golden bundles into ``<xsi_dir>/vectors/`` — the single scenario writer
    both backends share, so pysim and RTL cannot be running different tests."""
    from examples.fir_block.fir_block_sim import DEFAULT_BLK, DEFAULT_PROGRAM, FirBlockSim

    FirBlockSim(program=tuple(program or DEFAULT_PROGRAM),
                blk=int(blk if blk is not None else DEFAULT_BLK), ntap=int(ntap),
                samp_w=int(samp_w), samp_i=int(samp_i),
                mem_dwidth=int(mem_dwidth)).write_scenario(Path(xsi_dir))


def check_xsi_outputs(xsi_dir: Path, program=None, blk: int | None = None, ntap: int = DEFAULT_NTAP,
                      samp_w: int = DEFAULT_SAMP_W, samp_i: int = DEFAULT_SAMP_I,
                      mem_dwidth: int = MEM_DW) -> None:
    """Check the XSI run from the dumped bundles: every output block equals the golden, and one
    completion landed per command — ``LOAD_TAPS`` included."""
    from waveflow.utils.burst_io import read_burst_bundle
    from examples.fir_block.fir_block import FirDesc

    tb = make_xsi_tb(program, blk, ntap, samp_w, samp_i, mem_dwidth)
    vdir = Path(xsi_dir) / "vectors"
    out = read_burst_bundle(vdir / "out")[0]
    golden = read_burst_bundle(vdir / "golden")[0]
    for j, step in enumerate(s for s in tb._steps if s["op"] == FirOp.FILTER):
        # WORDS, not samples: at LW samples per word an n-sample block occupies ceil(n/LW) words, and
        # comparing `n` of them would run off the end of the region into untouched arena.
        dst, nw = step["dst"], step["nw"]
        got, want = out[dst:dst + nw], golden[dst:dst + nw]
        if not np.array_equal(got, want):
            bad = int(np.argmax(got != want))
            raise AssertionError(
                f"fir_block RTL block {j} word {bad}: 0x{int(got[bad]):08x} != "
                f"golden 0x{int(want[bad]):08x}")
    s_done = read_burst_bundle(vdir / "s_done")[0]
    dw = FirDesc.nwords_per_inst(int(mem_dwidth))
    assert len(s_done) == len(tb._steps) * dw, (
        f"fir_block RTL: s_done has {len(s_done)} words, expected {len(tb._steps)}*{dw} "
        f"(one FirDesc echo per command, LOAD_TAPS included)")


def generate_tb(out_dir: Path = HERE, program=None, blk: int | None = None, ntap: int = DEFAULT_NTAP,
                samp_w: int = DEFAULT_SAMP_W, samp_i: int = DEFAULT_SAMP_I,
                mem_dwidth: int = MEM_DW, n_cycles: "int | None" = None) -> dict:
    """Generate the XSI testbench — scenario constants, BFM harness, and the two-line main — all
    derived from the FirBlockTB graph, plus the scenario bundles themselves."""
    out_dir = Path(out_dir)
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    tb = make_xsi_tb(program, blk, ntap, samp_w, samp_i, mem_dwidth, n_cycles)
    (xsi / f"{TOP}_vectors.h").write_text(render_xsi_vectors(tb), encoding="utf-8")
    spec = tb_top_spec(tb)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{TOP}_bfm_tb.cpp").write_text(render_tb_main(spec, tb.n_cycles), encoding="utf-8")
    write_xsi_bundles(xsi, program, blk, ntap, samp_w, samp_i, mem_dwidth)
    # The xvlog file list, regenerated from the RTL that is actually on disk.  Never hand-maintain it:
    # a stale .f plus a cached xsimk.dll is how an XSI run goes green while proving nothing.
    if (out_dir / f"{TOP}_proj").is_dir():
        (xsi / f"rtl_{TOP}.f").write_text(render_rtl_f(TOP, out_dir), encoding="utf-8")
    else:
        print(f"  (no {TOP}_proj yet — run csynth, then regenerate to get rtl_{TOP}.f)")
    print(f"generated TB xsi/{TOP}_vectors.h + xsi/{TOP}_tb_harness.h + xsi/{TOP}_bfm_tb.cpp")
    return {"harness": xsi / f"{TOP}_tb_harness.h", "main": xsi / f"{TOP}_bfm_tb.cpp"}


def generate(out_dir: Path = HERE, **kw) -> None:
    """Both codegen halves in order — the DUT first, since the TB harness includes its port map."""
    generate_dut(out_dir, **kw)
    generate_tb(out_dir, **{k: v for k, v in kw.items() if k != "unroll_lane"})


# ---------------------------------------------------------------------------
# The build DAG — the steps, in the order the flow teaches them
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the FirBlockTB graph in SimPy and check every output block against the stateless golden.

    The **first checkpoint**: no toolchain, seconds not minutes, and where any state bug that is not
    schedule-dependent shows up.  Runs the ``LOAD -> FILTER x2 -> LOAD -> FILTER`` program, so the
    carry is exercised past the first block and the held taps are proved replaceable mid-stream.
    Raises on any mismatch, so "it ran" is never mistaken for "it is correct".
    """

    description = "Run the FirBlockTB pysim golden (block-wise output == global convolution)."
    consumes = ["fir_block_source"]
    produces = {"pysim_results": Path("results/pysim.json")}
    params = {"ntap": DEFAULT_NTAP, "samp_w": DEFAULT_SAMP_W, "samp_i": DEFAULT_SAMP_I,
              "unroll_lane": False}

    def run(self, config: BuildConfig, ntap, samp_w, samp_i, unroll_lane, **_) -> dict:
        from examples.fir_block.fir_block_sim import DEFAULT_PROGRAM, FirBlockSim

        sim = FirBlockSim(program=DEFAULT_PROGRAM, ntap=int(ntap), samp_w=int(samp_w),
                          samp_i=int(samp_i), unroll_lane=bool(unroll_lane))
        dut = sim.run()                   # raises on any block mismatch or completion miscount
        out = config.root_dir / "results" / "pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "program": list(sim.tb._program),
            "blk": int(sim.tb.blk), "ntap": int(ntap),
            "samp_w": int(samp_w), "samp_i": int(samp_i),
            "lane_width": int(sim.tb.lw),
            "realization": dut.compute.variant,
            "blocks_checked": len(sim.expected),
            "completions": len(sim.tb.done_sink.words),
        }, indent=2), encoding="utf-8")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    """Lower the ``FirBlock`` graph to the ``ap_ctrl_none`` composite top, plus its headers.

    Emits the schema headers, the sample element's array-utils, ``fir_types.h`` (the derived
    accumulator and the array-utils namespace alias) and ``fir_compute_state.inc`` (the ``add_state``
    storage), then copies the sticky hand-written task bodies into ``include/``.
    """

    description = "Lower the FirBlock graph to the composite top (+ tcl, ports, headers, state)."
    consumes = ["fir_block_source"]
    produces = {"fir_block_cpp": Path("gen/fir_block.cpp"), "run_tcl": Path("fir_block.tcl"),
                "dut_ports": Path("xsi/fir_block_ports.h")}
    params = {"mem_dwidth": MEM_DW, "ntap": DEFAULT_NTAP, "samp_w": DEFAULT_SAMP_W,
              "samp_i": DEFAULT_SAMP_I, "unroll_lane": False}

    def run(self, config: BuildConfig, mem_dwidth, ntap, samp_w, samp_i, unroll_lane, **_) -> dict:
        generate_dut(config.root_dir, mem_dwidth=int(mem_dwidth), ntap=int(ntap),
                     samp_w=int(samp_w), samp_i=int(samp_i), unroll_lane=bool(unroll_lane))
        return {"fir_block_cpp": config.root_dir / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": config.root_dir / f"{TOP}.tcl",
                "dut_ports": config.root_dir / "xsi" / f"{TOP}_ports.h"}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    """Lower the ``FirBlockTB`` graph to the XSI BFM harness, main, scenario constants, and bundles.

    Runs after the DUT step because the harness ``#include``s the DUT's port map.
    """

    description = "Lower the FirBlockTB graph to the XSI harness + main + scenario bundles."
    consumes = ["fir_block_source", "dut_ports"]
    produces = {"tb_harness": Path("xsi/fir_block_tb_harness.h"),
                "tb_main": Path("xsi/fir_block_bfm_tb.cpp")}
    params = {"mem_dwidth": MEM_DW, "ntap": DEFAULT_NTAP, "samp_w": DEFAULT_SAMP_W,
              "samp_i": DEFAULT_SAMP_I}

    def run(self, config: BuildConfig, mem_dwidth, ntap, samp_w, samp_i, **_) -> dict:
        generate_tb(config.root_dir, ntap=int(ntap), samp_w=int(samp_w), samp_i=int(samp_i),
                    mem_dwidth=int(mem_dwidth))
        return {"tb_harness": config.root_dir / "xsi" / f"{TOP}_tb_harness.h",
                "tb_main": config.root_dir / "xsi" / f"{TOP}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of the generated top — produces the RTL the ``-m xsi`` gate drives.

    Toolchain-gated (needs Vitis).  Re-emits ``rtl_fir_block.f`` afterwards so the file list always
    names the RTL that was just built: a stale ``.f`` plus a cached ``xsimk.dll`` is how an XSI run
    goes green while proving nothing.
    """

    description = "Run Vitis HLS C-synthesis of the generated composite top."
    consumes = ["fir_block_cpp", "run_tcl"]
    produces = {"report_dir": Path("fir_block_proj/solution1")}
    params = {"live_output": False}

    def run(self, config: BuildConfig, live_output, **_) -> dict:
        result = toolchain.run_vitis_hls(config.root_dir / f"{TOP}.tcl",
                                         work_dir=config.root_dir,
                                         capture_output=not live_output)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        xsi = config.root_dir / "xsi"
        xsi.mkdir(parents=True, exist_ok=True)
        (xsi / f"rtl_{TOP}.f").write_text(render_rtl_f(TOP, config.root_dir), encoding="utf-8")
        return {"report_dir": config.root_dir / f"{TOP}_proj" / "solution1"}


def build_fir_block_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="fir_block_source", path=HERE / "fir_block.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    return dag


def main() -> None:
    run_dag_cli(
        build_fir_block_dag,
        description="Build and check the fir_block example.",
        default_through="pysim",
        root_dir=HERE,
        extra_args=[
            (("--samp-w",), {"type": int, "default": DEFAULT_SAMP_W,
                             "help": "Sample/coefficient width in bits (default 16)."}),
            (("--samp-i",), {"type": int, "default": DEFAULT_SAMP_I,
                             "help": "Integer bits of the sample format (default 2)."}),
            (("--ntap",), {"type": int, "default": DEFAULT_NTAP,
                           "help": "Number of filter taps (default 32)."}),
            (("--unroll-lane",), {"action": "store_true",
                                  "help": "Emit the LW-outputs-per-iteration kernel instead of "
                                          "the one-output-per-iteration one."}),
            (("--live-output",), {"action": "store_true",
                                  "help": "Stream Vitis output live (csynth)."}),
        ],
        params_from_args=lambda a: {"mem_dwidth": MEM_DW, "ntap": a.ntap, "samp_w": a.samp_w,
                                    "samp_i": a.samp_i, "unroll_lane": a.unroll_lane,
                                    "live_output": a.live_output},
    )


if __name__ == "__main__":
    main()
