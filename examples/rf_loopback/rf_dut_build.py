"""rf_dut_build.py — the RF pass-through DUT, synthesized and driven at RTL.

``plans/adc_model.md`` staging item 2, opened at the digital-logic end rather than the converter end.

**The same DUT, cut differently.**  ``RfSampPassThrough`` is the module that sits between the two
converter ports in :mod:`examples.rf_loopback.rf_loopback`.  Here the graph around it is
``StreamDriver -> dut -> StreamSink`` and nothing else: generic AXI-Stream BFMs, no converter, no RF
edges.  That is not a different design — it is *the same module under a different cut*, which is the
property ``plans/design_cut.md`` asserts and this file exercises for real:

    the cut is a build choice, and a module's role follows it.

Doing it this way also keeps the two risks apart.  Whether the DUT synthesizes and runs at RTL is one
question; whether the converter models drive it correctly is another.  Answering the first with
generic BFMs means that when the converter is wired in, a failure has one place to be.

The task body is **generated** from ``run_iter`` — not hand-written.  Getting there cost two
extractor findings, both recorded in ``plans/adc_model.md``: an implicit capture of a pysim counter,
and the raw-word ``get(nwords_max=...)`` form having no extraction rule.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep  # noqa: E402
from waveflow.build.composite_gen import (  # noqa: E402
    GEN_DIR,
    RFSOC4X2_PART,
    RFSOC4X2_PERIOD_NS,
    composite_top_spec,
    render_ports_h,
    render_rtl_f,
    render_tb_harness,
    render_tb_main,
    render_tcl,
    render_top,
    tb_top_spec,
)
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep, XsiHarnessStep  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.hw.hw_freerun import FreeRunMod  # noqa: E402
from waveflow.hw.interface import StreamIF  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.stream_tb import StreamDriver, StreamSink  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402
from waveflow.utils.burst_io import write_burst_bundle  # noqa: E402

from examples.rf_loopback.rf_loopback import RfSampBlockRelay, RfSampPassThrough  # noqa: E402

#: The generated top's name — the DUT's own ``cpp_kernel_name``.
TOP = "rf_pass_through"
INCLUDE_DIR = "include"

#: The gate scenario.  Eight bursts of one block each, back to back — enough firings that a body
#: which worked once and then wedged is visible, and few enough that the run is seconds.
XSI_WORD_BW = 64
XSI_NWORDS_BLK = 64
XSI_NBURST = 8

#: A generous ``h.run(N)``.  One firing reads ``nwords_blk`` words then writes them, so it is at
#: least ``2 * nwords_blk`` cycles; the loop bound only has to clear completion, and the sink
#: timestamps the real number.
XSI_N_CYCLES = 2000


@dataclass
class RfPassThroughTB(FreeRunMod):
    """The DUT alone, between a generic AXI-Stream driver and sink.

    Three nodes, two edges, no RF domain anywhere.  Deliberately the *smallest* graph that can answer
    "does the generated RTL relay a block unchanged?".
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    bitwidth: int = XSI_WORD_BW
    nwords_blk: int = XSI_NWORDS_BLK
    #: Fixed run bound for the generated XSI main.  A testbench constant, not the design's latency —
    #: the sink reports that, and conflating the two is how three hand-written TBs reported a drain
    #: tail as a result.
    n_cycles: int = XSI_N_CYCLES
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.dut = RfSampPassThrough(name=f"{self.name}_dut", sim=self.sim, bitwidth=w,
                                     nwords_blk=int(self.nwords_blk), clk=self.clk)
        # has_tlast=True on both participants because the DUT's endpoints declare it and StreamIF
        # refuses a mismatch.  It is pysim framing only: the generated top carries plain
        # `hls::stream<ap_uint<W>>` ports and the generic BFMs drive no TLAST pin — the same
        # asymmetry mem_copy's s_done already has.
        self.driver = StreamDriver(sim=self.sim, name=f"{self.name}_drv", bitwidth=w,
                                   in_bundle="vectors/s_in", has_tlast=True)
        self.sink = StreamSink(sim=self.sim, name=f"{self.name}_snk", bitwidth=w,
                               out_bundle="vectors/s_out", has_tlast=True)
        for c in (self.dut, self.driver, self.sink):
            self.add_comp(c)

        in_if = StreamIF(name=f"{self.name}_in_if", sim=self.sim, clk=self.clk, bitwidth=w)
        in_if.bind(ep_name="master", endpoint=self.driver.stream_ep)
        in_if.bind(ep_name="slave", endpoint=self.dut.s_in)
        self.add_if(in_if)

        out_if = StreamIF(name=f"{self.name}_out_if", sim=self.sim, clk=self.clk, bitwidth=w)
        out_if.bind(ep_name="master", endpoint=self.dut.s_out)
        out_if.bind(ep_name="slave", endpoint=self.sink.stream_ep)
        self.add_if(out_if)



# ---------------------------------------------------------------------------
# The scenario — one on-disk source, both backends
# ---------------------------------------------------------------------------

def input_bursts(nburst: int = XSI_NBURST, nwords: int = XSI_NWORDS_BLK,
                 seed: int = 0xC0FFEE) -> list[np.ndarray]:
    """``nburst`` bursts of ``nwords`` full-width random words.

    Full-width on purpose: a dropped or truncated high half is invisible in a pattern that never
    sets the top bits, and word packing is exactly what a relay could get wrong.
    """
    rng = np.random.default_rng(seed)
    return [rng.integers(0, 1 << 64, size=nwords, dtype=np.uint64) for _ in range(nburst)]


def write_scenario(root, nburst: int = XSI_NBURST, nwords: int = XSI_NWORDS_BLK) -> None:
    """Materialize ``<root>/vectors/s_in`` — what the driver plays in both backends."""
    write_burst_bundle(input_bursts(nburst, nwords), Path(root) / "vectors" / "s_in")


def check_xsi_outputs(xsi_dir: Path, want_cycles: int, nburst: int = XSI_NBURST,
                      nwords: int = XSI_NWORDS_BLK) -> None:
    """Check the XSI run from the bundles it dumped — the golden, in Python.

    The generated main only runs and dumps. Asserts:

    - **correctness** — the captured word stream is **bit-identical** to what was played;
    - **completion** — every word came back, none extra;
    - **timing** — the cycle the last word landed equals *want_cycles*. Time-to-last-completion,
      not the loop bound.
    """
    from waveflow.utils.burst_io import read_burst_bundle

    vdir = Path(xsi_dir) / "vectors"
    sent = np.concatenate(input_bursts(nburst, nwords))
    got = np.concatenate(read_burst_bundle(vdir / "s_out")) if (vdir / "s_out").is_dir() else None
    assert got is not None, f"no capture bundle at {vdir / 's_out'} — the run did not dump one"

    assert got.size == sent.size, (
        f"rf_pass_through relayed {got.size} words, expected {sent.size} "
        f"({nburst} bursts x {nwords})")
    if not np.array_equal(got, sent):
        bad = int(np.argmax(got != sent))
        raise AssertionError(
            f"rf_pass_through word {bad}: got 0x{int(got[bad]):016x} != "
            f"sent 0x{int(sent[bad]):016x}")

    cycles = np.fromfile(vdir / "s_out" / "cycles.bin", dtype="<u8")
    assert cycles.size == sent.size, (
        f"capture has {cycles.size} arrival cycles for {sent.size} words")
    last = int(cycles[-1])
    assert last == want_cycles, (
        f"rf_pass_through completed at cycle {last}, gate expects {want_cycles}. That is a real "
        f"behaviour change: either a regression or an improvement worth re-recording.")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def gen_headers(config: BuildConfig) -> None:
    """Emit ``streamutils_hls.h`` + ``memmgr.hpp`` + the payload element's array-utils, and copy the
    XSI framework library beside the testbench."""
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    # The generated body reads and writes a whole block through the ARRAY (de)serializers -- never
    # hand-rolled packing -- so the schema the design needs is the DataArray itself (its header is
    # what `#include "u_int64_array.h"` in the generated body names), not its element type.
    blk = RfSampPassThrough(name="probe", sim=Simulation(),
                            bitwidth=XSI_WORD_BW, nwords_blk=XSI_NWORDS_BLK).blk_words
    inner.add(DataSchemaStep(blk, word_bw_supported=[XSI_WORD_BW], include_dir=INCLUDE_DIR))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")
    copy_fixed_task_bodies(config.root_dir)


#: Hand-written ``hls::task`` bodies this design instantiates, copied verbatim from ``src/`` — the
#: example-local twin of ``MemStreamStep``, which does the same for the framework's ``mem_*`` bodies.
#: Kept OUT of ``include/`` in the source tree so nothing there is half-generated: everything in
#: ``include/`` is a build product, and this is the step that puts it there.
FIXED_TASK_BODIES = ("rf_samp_ingress_task.h",)


def copy_fixed_task_bodies(root_dir: Path) -> None:
    """Copy :data:`FIXED_TASK_BODIES` from ``src/`` into ``include/``."""
    import shutil

    dst = Path(root_dir) / INCLUDE_DIR
    dst.mkdir(parents=True, exist_ok=True)
    for name in FIXED_TASK_BODIES:
        src = HERE / "src" / name
        if not src.is_file():
            raise FileNotFoundError(f"fixed task body missing: {src}")
        shutil.copyfile(src, dst / name)


def generate_dut(out_dir: Path = HERE, dut_cls: type = RfSampPassThrough,
                 top: str = TOP) -> Path:
    """Generate the task body, the ``ap_ctrl_none`` top, its csynth TCL and the XSI port map.

    *dut_cls* / *top* are arguments because the DUT has a **top per channel count**: a two-channel
    pass-through has four AXIS ports where the one-channel one has two, so it is a different RTL
    module with a project and a ports header of its own (see
    :class:`~examples.rf_loopback.rf_loopback.RfSampPassThrough2Ch`).  Everything below is the same
    for both — same body, same TCL shape — which is the point of parameterizing rather than forking.
    """
    from waveflow.build.elaborate import elaborate
    from waveflow.build.hwgen import task_files_to_str

    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config)

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    inc = out_dir / INCLUDE_DIR
    inc.mkdir(parents=True, exist_ok=True)
    # ONE task body is generated -- the block relay's, from its run_iter.  The ingress's is
    # hand-written and was copied by gen_headers: a composite's bodies are per CHILD, and the two
    # children answer the "who writes this body?" question differently.  There are no
    # @synthesizable hooks in the generated one, so no sticky impl stub is written (and none would
    # be wanted -- the whole body is derived).
    for name, content in task_files_to_str(RfSampBlockRelay).items():
        if name.endswith("_impl.cpp") or name.endswith("_impl.tpp"):
            continue
        (inc / name).write_text(content, encoding="utf-8")

    comp = elaborate(dut_cls, {"bitwidth": XSI_WORD_BW, "nwords_blk": XSI_NWORDS_BLK},
                     name=top)
    spec = composite_top_spec(comp, width=XSI_WORD_BW)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS),
        encoding="utf-8")
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl + xsi/{spec.top_name}_ports.h")
    return cpp


def make_xsi_tb(n_cycles: int = XSI_N_CYCLES) -> RfPassThroughTB:
    """The graph the XSI testbench is generated from — one statement, two backends."""
    return RfPassThroughTB(name="xsi_tb", sim=Simulation(), n_cycles=n_cycles)


def generate_tb(out_dir: Path = HERE, n_cycles: int = XSI_N_CYCLES) -> None:
    """Generate the XSI harness + main from the TB graph, and write the scenario bundle."""
    tb = make_xsi_tb(n_cycles)
    spec = tb_top_spec(tb)
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{TOP}_bfm_tb.cpp").write_text(render_tb_main(spec, n_cycles), encoding="utf-8")
    write_scenario(xsi)
    print(f"generated TB xsi/{TOP}_tb_harness.h + xsi/{TOP}_bfm_tb.cpp "
          f"({XSI_NBURST} bursts x {XSI_NWORDS_BLK} words)")


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the DUT-alone graph in SimPy and check the relay is bit-identical.

    The first checkpoint, and the one that needs no toolchain: if the pass-through does not pass
    words through in Python, nothing downstream is worth running.
    """

    description = "Run the RfPassThroughTB pysim golden (words in == words out)."
    consumes = ["rf_dut_source"]
    produces = {"pysim_results": Path("results/rf_dut_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import tempfile

        tb = RfPassThroughTB(name="tb", sim=Simulation())
        with tempfile.TemporaryDirectory() as root:
            write_scenario(root)
            tb.driver.root = Path(root)
            tb.sim.run_sim()
        sent = np.concatenate(input_bursts())
        got = np.concatenate(tb.sink.words) if tb.sink.words else np.zeros(0, dtype=np.uint64)
        assert np.array_equal(got, sent), "pysim relay is not bit-identical"
        assert tb.dut.n_blk == XSI_NBURST, f"DUT fired {tb.dut.n_blk} times, expected {XSI_NBURST}"
        out = config.root_dir / "results" / "rf_dut_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"bursts": XSI_NBURST, "nwords_blk": XSI_NWORDS_BLK,
                                   "words": int(sent.size), "firings": int(tb.dut.n_blk)},
                                  indent=2), encoding="utf-8")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfSampPassThrough to its ap_ctrl_none top (+ generated task body, tcl, ports)."
    consumes = ["rf_dut_source"]
    produces: ClassVar[dict] = {"rf_dut_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl"),
                                "dut_ports": Path(f"xsi/{TOP}_ports.h")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        return {"rf_dut_cpp": config.root_dir / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": config.root_dir / f"{TOP}.tcl",
                "dut_ports": config.root_dir / "xsi" / f"{TOP}_ports.h"}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfPassThroughTB to the XSI harness + main + scenario bundle."
    consumes = ["rf_dut_source", "dut_ports"]
    produces: ClassVar[dict] = {"tb_harness": Path(f"xsi/{TOP}_tb_harness.h"),
                                "tb_main": Path(f"xsi/{TOP}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb(config.root_dir)
        return {"tb_harness": config.root_dir / "xsi" / f"{TOP}_tb_harness.h",
                "tb_main": config.root_dir / "xsi" / f"{TOP}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis — produces the RTL the ``-m xsi`` gate drives.

    Re-emits ``rtl_<top>.f`` from the RTL that was just built. A stale file list plus a cached
    ``xsimk.dll`` is how an XSI run goes green while proving nothing.
    """

    description = "Run Vitis HLS C-synthesis of the generated top."
    consumes = ["rf_dut_cpp", "run_tcl"]
    produces: ClassVar[dict] = {"report_dir": Path(f"{TOP}_proj/solution1")}
    params: ClassVar[dict] = {"live_output": False}

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


def build_rf_dut_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_dut_source", path=HERE / "rf_loopback.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_dut_dag,
                description="Build the RF pass-through DUT: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
