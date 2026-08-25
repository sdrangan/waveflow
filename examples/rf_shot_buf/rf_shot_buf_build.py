"""rf_shot_buf_build.py — build the finite sample buffer: pysim -> codegen -> csynth.

``plans/rf_shot_buf.md`` § *Stage A*.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> the shot loads and plays back byte-identical in SimPy, phases separated
    codegen_dut -> the ap_ctrl_none top (two tasks, two `mode=bram` ports), its tcl, its port map,
                   the memory placed beside it, and the WRAPPER that joins them
    codegen_tb  -> the XSI harness + main + scenario bundle
    csynth      -> Vitis HLS (needs the toolchain); re-emits rtl_<wrapper>.f from the RTL on disk

The RTL rung — the same shot through real Verilog, bit-exact against the pysim golden — is the
``-m xsi`` gate in ``tests/examples/test_rf_shot_buf_xsi.py``.

As with ``bram_toy`` and ``rf_samp_buf_rx``, what a simulator elaborates is the **wrapper**
(``rf_shot_buf_top``), not the kernel: the memory is inside it, which is why the testbench sees only
AXI-Stream and the BFM library needs no memory model.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

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
from waveflow.build.elaborate import elaborate  # noqa: E402
from waveflow.build.rtl_steps import GenRtlStep, GenWrapperStep  # noqa: E402
from waveflow.build.streamutils import (  # noqa: E402
    MemMgrStep,
    RfShotBufStep,
    StreamUtilsStep,
    XsiHarnessStep,
)
from waveflow.hw.rf_shot_buf import RfShotBuf  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_shot_buf.rf_shot_buf import (  # noqa: E402
    DEPTH,
    NWORD,
    WORD,
    RfShotBufTB,
    write_scenario,
)

#: The generated kernel's name, and the wrapper's.  The wrapper is what a simulator elaborates.
TOP = "rf_shot_buf"
WRAPPER = f"{TOP}_top"
INCLUDE_DIR = "include"

#: Hand-written ``hls::task`` bodies — **framework**, shipped from ``waveflow/build/`` by
#: :class:`~waveflow.build.streamutils.RfShotBufStep`, so this example carries no ``src/``.
FIXED_TASK_BODIES = ("rf_shot_buf_load_task.h", "rf_shot_buf_read_task.h")

#: RTL that must land in ``xsi/`` beside the ``.f`` naming it, in elaboration reading order: the
#: memory, then the wrapper that instantiates it.
RTL_FILES = ("bram_t2p.v", f"{WRAPPER}.v")

#: The gated geometry, stated rather than defaulted: the 4x2's 64-bit word, a 1024-word buffer and a
#: 256-word shot.  The recorded XSI cycle count belongs to exactly this configuration.
_ELAB = {"bitwidth": int(WORD.bitwidth), "samp_per_word": int(WORD.samp_per_word),
         "depth": int(DEPTH), "nword": int(NWORD)}


def generate_dut(out_dir: Path = HERE) -> Path:
    """Generate the top, its tcl, the XSI port map, the memory and the wrapper.

    The wrapper is derived from the **same** ``TopSpec`` that emits the kernel's interface pragmas,
    so the two cannot disagree about the kernel's port list — which for a ``bram`` port means
    fourteen signal names nobody typed twice.
    """
    word_bw = int(_ELAB["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(RfShotBufStep(output_dir=INCLUDE_DIR))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    inner.add(GenRtlStep(name="place_memory", comp_class=_memory_class(), output_dir="xsi"))
    inner.add(GenWrapperStep(name="wrapper", comp_class=RfShotBuf, elab_params=dict(_ELAB),
                             width=word_bw, output_dir="xsi"))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    comp = elaborate(RfShotBuf, dict(_ELAB), name=TOP)
    spec = composite_top_spec(comp, width=word_bw)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS),
        encoding="utf-8")
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl + xsi/{spec.top_name}_ports.h "
          f"+ xsi/{WRAPPER}.v (elaborated top: {spec.elab_top})")
    return cpp


def _memory_class():
    """The memory class the design instantiates — read off the elaborated graph, not restated."""
    comp = elaborate(RfShotBuf, dict(_ELAB))
    mems = {type(m) for m in comp.rtl_mods.values()}
    if len(mems) != 1:
        raise RuntimeError(f"expected exactly one RTL module class in {TOP}, got {mems}")
    return mems.pop()


def make_xsi_tb() -> RfShotBufTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs."""
    return RfShotBufTB(name="xsi_tb", sim=Simulation())


def generate_tb(out_dir: Path = HERE) -> None:
    """Generate the XSI harness + main from the TB graph, and write the scenario bundle."""
    tb = make_xsi_tb()
    spec = tb_top_spec(tb)
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{TOP}_bfm_tb.cpp").write_text(render_tb_main(spec, int(tb.n_cycles)), encoding="utf-8")
    write_scenario(xsi)
    print(f"generated TB xsi/{TOP}_tb_harness.h + xsi/{TOP}_bfm_tb.cpp ({NWORD} words)")


def wrapper_text() -> str:
    """The wrapper Verilog for the gated configuration — for tests that want it without a build."""
    from waveflow.build.wrapper_gen import render_wrapper, wrapper_spec

    comp = elaborate(RfShotBuf, dict(_ELAB), name=TOP)
    return render_wrapper(wrapper_spec(comp, composite_top_spec(comp, width=int(WORD.bitwidth))))


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the shot in SimPy: byte-identical playback, and the phase guard actually exercised."""

    description = "Run the RfShotBufTB pysim golden (one shot in, the same shot out)."
    consumes = ["rf_shot_buf_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/rf_shot_buf_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        from examples.rf_shot_buf.rf_shot_buf import captured_words, check_outputs, run_pysim

        tb = run_pysim()
        got = captured_words(tb)
        check_outputs(got, where="pysim: ")
        tb.dut.assert_phases_separated()
        out = config.root_dir / "results" / "rf_shot_buf_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "nword": int(NWORD), "depth": int(DEPTH), "bitwidth": int(WORD.bitwidth),
            "nsamp_shot": int(tb.dut.nsamp_shot), "nsamp_held": int(tb.dut.nsamp_held),
            "n_shots": int(tb.dut.n_shots), "words_out": int(got.size)}, indent=2),
            encoding="utf-8")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfShotBuf to its ap_ctrl_none top + the memory + the wrapper."
    consumes = ["rf_shot_buf_source"]
    produces: ClassVar[dict] = {"rf_shot_buf_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl"),
                                "dut_ports": Path(f"xsi/{TOP}_ports.h"),
                                "wrapper_v": Path(f"xsi/{WRAPPER}.v"),
                                "memory_v": Path("xsi/bram_t2p.v")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"rf_shot_buf_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl",
                "dut_ports": root / "xsi" / f"{TOP}_ports.h",
                "wrapper_v": root / "xsi" / f"{WRAPPER}.v",
                "memory_v": root / "xsi" / "bram_t2p.v"}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfShotBufTB to the XSI harness + main + scenario bundle."
    consumes = ["rf_shot_buf_source", "dut_ports"]
    produces: ClassVar[dict] = {"tb_harness": Path(f"xsi/{TOP}_tb_harness.h"),
                                "tb_main": Path(f"xsi/{TOP}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb(config.root_dir)
        return {"tb_harness": config.root_dir / "xsi" / f"{TOP}_tb_harness.h",
                "tb_main": config.root_dir / "xsi" / f"{TOP}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of the kernel — and the ``.f`` for the **wrapper**.

    Re-emitted from the RTL that is actually on disk, because a stale file list plus a cached
    ``xsimk.dll`` is how an XSI run goes green while proving nothing.
    """

    description = "Run Vitis HLS C-synthesis of the generated top."
    consumes = ["rf_shot_buf_cpp", "run_tcl"]
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
        (xsi / f"rtl_{WRAPPER}.f").write_text(
            render_rtl_f(TOP, config.root_dir, extra=RTL_FILES), encoding="utf-8")
        return {"report_dir": config.root_dir / f"{TOP}_proj" / "solution1"}


def build_rf_shot_buf_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_shot_buf_source", path=HERE / "rf_shot_buf.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_shot_buf_dag,
                description="Build the rf_shot_buf design: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
