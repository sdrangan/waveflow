"""rf_blk_delay_build.py — build the pattern-B loop: pysim -> codegen -> csynth.

``plans/adc_model.md`` § *Two design patterns*, the B case: a user's block reaching the converter
through a sample buffer at each end.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> the loop in SimPy: the measured delay, bit-exactness, and the four loss counters
    codegen_dut -> the ap_ctrl_none top (FIVE tasks, FOUR `mode=bram` ports), its tcl, its port map,
                   the TWO memories placed beside it, and the WRAPPER that joins them
    codegen_tb  -> the XSI harness + main + scenario bundles
    csynth      -> Vitis HLS (needs the toolchain); re-emits rtl_<wrapper>.f from the RTL on disk

The RTL rung is the ``-m xsi`` gate in ``tests/examples/test_rf_blk_delay_xsi.py``.

**This is the first design in the repo whose top is two levels deep.**  ``RfBlkDelayLoop`` contains
``RfSampBufRx`` and ``RfSampBufTx``, which are themselves composites, and ``hls::task`` has no
hierarchy — so the generator flattens it (``composite_gen.kernel_tasks``) into five tasks joined by
six channels, and the wrapper instantiates both buffers' memories.  Nothing here says any of that;
it is the same three-line build the flat examples have, which is the property worth having.

As with ``bram_toy``, what a simulator elaborates is the **wrapper** (``rf_blk_delay_top``), not the
kernel: the memories are inside it, which is why the testbench sees only AXI-Stream.
"""
from __future__ import annotations

import shutil
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
    RfSampBufStep,
    StreamUtilsStep,
    XsiHarnessStep,
)
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.hw.rf_samp_buf import RxCmd, RxResp  # noqa: E402
from waveflow.hw.rf_samp_buf_tx import TxCmd, TxResp  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_blk_delay.rf_blk_delay import (  # noqa: E402
    BLKSIZE,
    DELAY_BLOCKS,
    N_BLK,
    RX_DEPTH,
    SAMP_BW,
    SAMP_PER_WORD,
    TX_DEPTH,
    RfBlkDelayLoop,
    RfBlkDelayTB,
    write_scenario,
)

#: The generated kernel's name, and the wrapper's.  The wrapper is what a simulator elaborates.
TOP = "rf_blk_delay"
WRAPPER = f"{TOP}_top"
INCLUDE_DIR = "include"

#: Hand-written ``hls::task`` bodies this design instantiates, copied verbatim from ``src/``.
#:
#: Only ``blk_delay_task.h`` is here.  The four ``RfSampBuf`` bodies are **framework** and ship from
#: ``waveflow/build/`` via :class:`~waveflow.build.streamutils.RfSampBufStep` — which is the whole
#: economy of pattern B: the example writes the one body that is its own, and the two hard ones
#: (never refuse a write, never miss a deadline) arrive with the modules.
FIXED_TASK_BODIES = ("blk_delay_task.h",)

#: RTL that must land in ``xsi/`` beside the ``.f`` naming it, in elaboration reading order.  One
#: ``bram_t2p.v`` serves both memories — it is a module definition, instantiated twice.
RTL_FILES = ("bram_t2p.v", f"{WRAPPER}.v")

#: The schema classes both directions' commands and responses need C++ headers for.
SCHEMA_CLASSES = (RxCmd, RxResp, TxCmd, TxResp)

#: The gated geometry, stated rather than defaulted: FOUR samples per 64-bit word, which is
#: ``Rfdc``'s ceiling.  The recorded XSI cycle count is for this configuration, so a change here is a
#: change to what the gate measures.
XSI_SAMP_PER_WORD = SAMP_PER_WORD


def elab_params(samp_per_word: int = XSI_SAMP_PER_WORD) -> dict:
    """The elaboration parameters.

    ``bitwidth`` is derived rather than declared: a word is ``samp_per_word`` samples of
    :data:`SAMP_BW` bits, so the two cannot disagree.
    """
    spw = int(samp_per_word)
    return {"bitwidth": SAMP_BW * spw, "samp_per_word": spw, "blksize": BLKSIZE,
            "delay_blocks": DELAY_BLOCKS, "rx_depth": RX_DEPTH, "tx_depth": TX_DEPTH,
            "horizon_margin": 4 * spw}


def copy_fixed_task_bodies(root_dir: Path) -> None:
    """Copy :data:`FIXED_TASK_BODIES` from ``src/`` into ``include/``."""
    dst = root_dir / INCLUDE_DIR
    dst.mkdir(parents=True, exist_ok=True)
    for name in FIXED_TASK_BODIES:
        src = HERE / "src" / name
        if not src.is_file():
            raise FileNotFoundError(f"fixed task body missing: {src}")
        shutil.copyfile(src, dst / name)


def generate_dut(out_dir: Path = HERE, samp_per_word: int = XSI_SAMP_PER_WORD) -> Path:
    """Generate the top, its tcl, the XSI port map, the schema headers, the memories and the wrapper.

    The elaborated graph is two levels deep and the generator flattens it; nothing below distinguishes
    this from a flat design, which is the property the flattening exists to give.
    """
    elab = elab_params(samp_per_word)
    word_bw = int(elab["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(RfSampBufStep(output_dir=INCLUDE_DIR))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    # Both directions' commands and responses at the design's AXIS word width.  Every body reads and
    # writes them with the generated read_stream<W> / write_stream<W> -- never a hand-rolled pack.
    # At W = 64 all three 16-bit fields ride in ONE word, which is exactly the case a hand-rolled
    # slice gets wrong and the generated serializer does not.
    for cls in SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=[word_bw], include_dir=INCLUDE_DIR))
    inner.add(GenRtlStep(name="place_memory", comp_class=_memory_class(samp_per_word),
                         output_dir="xsi"))
    inner.add(GenWrapperStep(name="wrapper", comp_class=RfBlkDelayLoop, elab_params=dict(elab),
                             width=word_bw, output_dir="xsi"))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")
    copy_fixed_task_bodies(out_dir)

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    comp = elaborate(RfBlkDelayLoop, dict(elab), name=TOP)
    spec = composite_top_spec(comp, width=word_bw)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS),
        encoding="utf-8")
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    print(f"generated DUT {cpp.name} ({len(spec.tasks)} tasks, {len(spec.channels)} channels) "
          f"+ {spec.top_name}.tcl + xsi/{spec.top_name}_ports.h + xsi/{WRAPPER}.v "
          f"(elaborated top: {spec.elab_top})")
    return cpp


def _memory_class(samp_per_word: int = XSI_SAMP_PER_WORD):
    """The memory class the design instantiates — read off the elaborated graph, not restated.

    Two memories, one class: ``rtl_mods`` aggregates over the flattened hierarchy, so both buffers'
    BRAMs appear here and their single shared module definition is emitted once.
    """
    comp = elaborate(RfBlkDelayLoop, elab_params(samp_per_word))
    mems = {type(m) for m in comp.rtl_mods.values()}
    if len(mems) != 1:
        raise RuntimeError(f"expected exactly one RTL module class in {TOP}, got {mems}")
    return mems.pop()


def make_xsi_tb() -> RfBlkDelayTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs."""
    return RfBlkDelayTB(name="xsi_tb", sim=Simulation())


def generate_tb(out_dir: Path = HERE) -> None:
    """Generate the XSI harness + main from the TB graph, and write the scenario bundles."""
    tb = make_xsi_tb()
    spec = tb_top_spec(tb)
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{TOP}_bfm_tb.cpp").write_text(render_tb_main(spec, int(tb.n_cycles)), encoding="utf-8")
    write_scenario(xsi)
    print(f"generated TB xsi/{TOP}_tb_harness.h + xsi/{TOP}_bfm_tb.cpp "
          f"({N_BLK} blocks x {BLKSIZE} samples, delayed {DELAY_BLOCKS} blocks)")


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the loop in SimPy — the toolchain-free checkpoint, and the golden the RTL is checked
    against."""

    description = "Run the RfBlkDelayTB pysim golden (the measured delay + the four loss counters)."
    consumes = ["rf_blk_delay_source"]
    produces = {"pysim_results": Path("results/rf_blk_delay_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        import numpy as np

        from examples.rf_blk_delay.rf_blk_delay import (measured_delay, played_samples,
                                                        ramp_samples, run_pysim, rx_responses,
                                                        tx_responses)

        tb = run_pysim()
        shift = measured_delay(tb)
        assert shift == DELAY_BLOCKS * BLKSIZE, f"measured delay {shift}, asked for the same block "
        played, ramp = played_samples(tb), ramp_samples()
        n = min(N_BLK * BLKSIZE, played.size - shift)
        assert np.array_equal(played[shift:shift + n], ramp[:n]), "the played loop is not the ramp"
        assert tb.dac_if.counters()["underrun"] == 0, "the DAC grid underran"
        out = config.root_dir / "results" / "rf_blk_delay_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"measured_delay": int(shift),
                                   "relayed": int(tb.dut.n_relayed),
                                   "rx": rx_responses(tb), "tx": tx_responses(tb),
                                   "adc_dropped": int(tb.adc_axis.dropped),
                                   "too_old": int(tb.rx.n_too_old),
                                   "too_late": int(tb.tx.n_too_late),
                                   "player_underrun": int(tb.tx.n_underrun),
                                   "player_played": int(tb.tx.n_played)}, indent=2),
                       encoding="utf-8")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfBlkDelayLoop to its ap_ctrl_none top + two memories + the wrapper."
    consumes = ["rf_blk_delay_source"]
    produces: ClassVar[dict] = {"rf_blk_delay_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl"),
                                "dut_ports": Path(f"xsi/{TOP}_ports.h"),
                                "wrapper_v": Path(f"xsi/{WRAPPER}.v"),
                                "memory_v": Path("xsi/bram_t2p.v")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"rf_blk_delay_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl",
                "dut_ports": root / "xsi" / f"{TOP}_ports.h",
                "wrapper_v": root / "xsi" / f"{WRAPPER}.v",
                "memory_v": root / "xsi" / "bram_t2p.v"}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfBlkDelayTB to the XSI harness + main + scenario bundles."
    consumes = ["rf_blk_delay_source", "dut_ports"]
    produces: ClassVar[dict] = {"tb_harness": Path(f"xsi/{TOP}_tb_harness.h"),
                                "tb_main": Path(f"xsi/{TOP}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb(config.root_dir)
        return {"tb_harness": config.root_dir / "xsi" / f"{TOP}_tb_harness.h",
                "tb_main": config.root_dir / "xsi" / f"{TOP}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of the kernel — and the ``.f`` for the wrapper."""

    description = "Run Vitis HLS C-synthesis of the generated top."
    consumes = ["rf_blk_delay_cpp", "run_tcl"]
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


def build_rf_blk_delay_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_blk_delay_source", path=HERE / "rf_blk_delay.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_blk_delay_dag,
                description="Build the pattern-B loop: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
