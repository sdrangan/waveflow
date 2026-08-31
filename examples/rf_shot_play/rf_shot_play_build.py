"""rf_shot_play_build.py — build the shot transmitter: pysim -> codegen -> csynth.

``plans/rf_shot_buf.md`` § *Stage B*.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> both scenarios in SimPy: the playout is bit-exact, every header is answered, and
                   the short load is refused with the samples it actually got
    codegen_dut -> the ap_ctrl_none top (five tasks, two ``mode=bram`` ports, a **framed** command
                   port and a framed response port), its tcl, its port map, the memory placed beside
                   it, and the WRAPPER that joins them
    codegen_tb  -> the XSI harness + main + both scenario bundles
    csynth      -> Vitis HLS (needs the toolchain); re-emits ``rtl_<wrapper>.f`` from the RTL on disk

The RTL rung — the same two scenarios through real Verilog, bit-exact against the pysim golden — is
the ``-m xsi`` gate in ``tests/examples/test_rf_shot_play_xsi.py``.

As with ``rf_shot_buf`` and ``rf_samp_buf_tx``, what a simulator elaborates is the **wrapper**
(``rf_shot_tx_top``), not the kernel: the memory is inside it, which is why the testbench sees only
AXI-Stream and the BFM library needs no memory model.

**The kernel is called ``rf_shot_tx``, not ``rf_shot_play``**, because that is
:attr:`~waveflow.hw.rf_shot_tx.RfShotTx.cpp_kernel_name` and the module is framework.  The
*example* is the graph around it — a converter, a driver and two sinks — and it takes the name of
what it demonstrates.
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
from waveflow.hw.arrayutils import ArrayUtilsStep  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.hw.rf_relayout import dense_elem_type, slot_elem_type, slots_per_word  # noqa: E402
from waveflow.hw.rf_shot_tx import SHOT_TX_SCHEMA_CLASSES, RfShotTx  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_shot_play.rf_shot_play import (  # noqa: E402
    DEPTH,
    GATE_FRAMES,
    NWORD,
    SHORT_FRAMES,
    WORD,
    RfShotPlayTB,
    write_scenario,
)

#: The generated kernel's name, and the wrapper's.  The wrapper is what a simulator elaborates.
TOP = "rf_shot_tx"
WRAPPER = f"{TOP}_top"
INCLUDE_DIR = "include"

#: Hand-written ``hls::task`` bodies — **framework**, shipped from ``waveflow/build/`` by
#: :class:`~waveflow.build.streamutils.RfShotBufStep`, so this example carries no ``src/``.
FIXED_TASK_BODIES = ("shot_tx_load_task.h", "shot_tx_play_task.h",
                     "rf_shot_buf_load_task.h", "rf_shot_buf_read_task.h",
                     "rf_relayout_to_slots_task.h")

#: RTL that must land in ``xsi/`` beside the ``.f`` naming it, in elaboration reading order: the
#: memory, then the wrapper that instantiates it.
RTL_FILES = ("bram_t2p.v", f"{WRAPPER}.v")

#: The two scenario bundles, and the mains that drive them.  ``cmd`` is the four-verdict gate;
#: ``cmd_short`` is the truncated transfer, which needs a run of its own because a design that is
#: busy refuses everything after its first accepted load — see ``rf_shot_play.py`` § *Two scenarios*.
SCENARIOS = (("cmd", GATE_FRAMES), ("cmd_short", SHORT_FRAMES))

#: Solution-level tcl.  **The reset trap is closed here and only here.**
#: :class:`~waveflow.hw.rf_shot_tx.ShotTxLoad` holds one ``static`` (``busy``) and opens with a
#: BLOCKING read, which is the safe side of the trap — but ``#pragma HLS reset`` alone did not take
#: under Vitis 2025.1 in ``rf_repeat_play`` (csynth reported "power-on initialization"), and this is
#: the setting that did.  It costs nothing measurable.
SOLUTION_CONFIG = ("config_rtl -reset state",)

#: The gated geometry, stated rather than defaulted: the 4x2's 64-bit word at four samples a beat,
#: a 256-word memory and a 64-word shot.  The recorded XSI cycle counts belong to exactly this.
_ELAB = {"bitwidth": int(WORD.bitwidth), "samp_per_word": slots_per_word(WORD),
         "depth": int(DEPTH), "nword": int(NWORD), "shift": int(WORD.justify_shift())}


def generate_dut(out_dir: Path = HERE) -> Path:
    """Generate the schema headers, the array utils, the top, its tcl, the port map, the memory and
    the wrapper.

    The wrapper is derived from the **same** ``TopSpec`` that emits the kernel's interface pragmas,
    so the two cannot disagree about the kernel's port list — which here includes the two ``TLAST``
    pins the framed command and response ports have and no other design in this repo does.
    """
    word_bw = int(_ELAB["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    # `render_top` includes memmgr.hpp unconditionally, so a generated top needs it beside the
    # sources even when the design has no m_axi port at all.
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(RfShotBufStep(output_dir=INCLUDE_DIR))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    # The command and response ports are `axi4s_word` streams, so the body calls
    # read_axi4_stream / write_axi4_stream -- which DataSchemaStep emits by default, alongside the
    # plain read_stream/write_stream pair.  (`framed=True` would add the INTERNAL framed_word twins;
    # nothing here needs them, and asking for them would ship two vocabularies for one port.)
    for cls in SHOT_TX_SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=[word_bw], include_dir=INCLUDE_DIR))
    # The serializers the re-layout body calls.  The SLOT element is the converter's container width
    # and the DENSE element the effective one; at 14-in-16 they differ, which is what makes the last
    # stage a conversion rather than a pair of wires.
    inner.add(ArrayUtilsStep(slot_elem_type(WORD, INCLUDE_DIR), [word_bw]))
    inner.add(ArrayUtilsStep(dense_elem_type(WORD, INCLUDE_DIR), [word_bw]))
    inner.add(GenRtlStep(name="place_memory", comp_class=_memory_class(), output_dir="xsi"))
    inner.add(GenWrapperStep(name="wrapper", comp_class=RfShotTx, elab_params=dict(_ELAB),
                             width=word_bw, output_dir="xsi"))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    comp = elaborate(RfShotTx, dict(_ELAB), name=TOP)
    if comp.is_identity:
        raise RuntimeError(
            f"{TOP} elaborated with shift=0, which makes the last stage the IDENTITY — the exact "
            f"condition plans/rf_shot_buf.md says must not be what gets measured. Use a word type "
            f"whose bits_per_samp differs from its bits_per_samp_pack (Rfsoc4x2SampWord).")
    spec = composite_top_spec(comp, width=word_bw)
    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS,
                   solution_config=SOLUTION_CONFIG),
        encoding="utf-8")
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl + xsi/{spec.top_name}_ports.h "
          f"+ xsi/{WRAPPER}.v ({len(spec.tasks)} tasks, {len(spec.channels)} internal channels, "
          f"{len(spec.ports)} ports; elaborated top: {spec.elab_top})")
    return cpp


def _memory_class():
    """The memory class the design instantiates — read off the elaborated graph, not restated."""
    comp = elaborate(RfShotTx, dict(_ELAB))
    mems = {type(m) for m in comp.rtl_mods.values()}
    if len(mems) != 1:
        raise RuntimeError(f"expected exactly one RTL module class in {TOP}, got {mems}")
    return mems.pop()


def make_xsi_tb() -> RfShotPlayTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs."""
    return RfShotPlayTB(name="xsi_tb", sim=Simulation())


def generate_tb(out_dir: Path = HERE) -> None:
    """Generate the XSI harness + main from the TB graph, and write **both** scenario bundles.

    Both, because the second one is driven by a hand-written main beside the generated one
    (``rf_shot_tx_short.cpp``): the graph is identical and only the bundles differ, so a second
    testbench *graph* would be a second model of one design — which is the trap this arc has paid for
    more than once.
    """
    tb = make_xsi_tb()
    spec = tb_top_spec(tb)
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{TOP}_bfm_tb.cpp").write_text(render_tb_main(spec, int(tb.n_cycles)), encoding="utf-8")
    for name, frames in SCENARIOS:
        write_scenario(xsi, frames, name=name)
    print(f"generated TB xsi/{TOP}_tb_harness.h + xsi/{TOP}_bfm_tb.cpp "
          f"({tb.n_cycles} cycles) + vectors/{{{', '.join(n for n, _ in SCENARIOS)}}}")


def wrapper_text() -> str:
    """The wrapper Verilog for the gated configuration — for tests that want it without a build."""
    from waveflow.build.wrapper_gen import render_wrapper, wrapper_spec

    comp = elaborate(RfShotTx, dict(_ELAB), name=TOP)
    return render_wrapper(wrapper_spec(comp, composite_top_spec(comp, width=int(WORD.bitwidth))))


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run **both** scenarios in SimPy — the toolchain-free golden.

    Both, because they answer different questions and neither is a subset of the other: the gate
    scenario says a whole shot plays three times bit-exact, and the short one says a truncated
    transfer produces a verdict instead of a hang.
    """

    description = "Run the RfShotPlayTB pysim golden (the playout, the verdicts, the short load)."
    consumes = ["rf_shot_play_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/rf_shot_play_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        from examples.rf_shot_play.rf_shot_play import (
            NREPEAT,
            STARTUP_BLOCKS,
            check_played,
            check_responses,
            expected_plays,
            first_play_offset,
            played_samples,
            responses,
            run_pysim,
        )

        out: dict[str, object] = {}
        for name, frames in SCENARIOS:
            tb = run_pysim(frames=frames, in_bundle=f"vectors/{name}")
            check_responses(responses(tb), frames, where=f"pysim {name}: ")
            played = played_samples(tb)
            n_plays = expected_plays(frames)
            check_played(played, n_plays, where=f"pysim {name}: ")
            if n_plays:
                # Only the scenario that actually plays can make the converter claim: an edge fed
                # nothing underruns every block, which is correct and is asserted as such below.
                tb.dac_if.assert_clean(STARTUP_BLOCKS)
                tb.dut.assert_played(n_plays)
            elif int(tb.dac_if.underrun) != int(tb.n_blk):
                raise AssertionError(
                    f"pysim {name}: the DAC got {int(tb.n_blk) - int(tb.dac_if.underrun)} real "
                    f"block(s) from a load that was never playable. A shot that is not accepted "
                    f"must reach the converter not at all.")
            out[name] = {
                "responses": responses(tb), "n_plays": int(tb.dut.n_plays),
                "played_samples": int(played.size), "first_play_offset": first_play_offset(played),
                "underrun": int(tb.dac_if.underrun),
                "last_underrun_idx": int(tb.dac_if.last_underrun_idx),
                "blocks_delivered": int(tb.dac_if.blocks_delivered),
            }
        tb0 = run_pysim(frames=GATE_FRAMES)
        out["geometry"] = {
            "nword": int(NWORD), "depth": int(DEPTH), "bitwidth": int(WORD.bitwidth),
            "samp_per_word": slots_per_word(WORD), "shift": int(WORD.justify_shift()),
            "nrepeat": int(NREPEAT), "nsamp_shot": int(tb0.dut.nsamp_shot),
            "nsamp_held": int(tb0.dut.nsamp_held), "words_per_cycle": tb0.words_per_cycle,
        }
        p = config.root_dir / "results" / "rf_shot_play_pysim.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return {"pysim_results": p}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfShotTx to its ap_ctrl_none top + the memory + the wrapper."
    consumes = ["rf_shot_play_source"]
    produces: ClassVar[dict] = {"rf_shot_tx_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl"),
                                "dut_ports": Path(f"xsi/{TOP}_ports.h"),
                                "wrapper_v": Path(f"xsi/{WRAPPER}.v"),
                                "memory_v": Path("xsi/bram_t2p.v")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"rf_shot_tx_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl",
                "dut_ports": root / "xsi" / f"{TOP}_ports.h",
                "wrapper_v": root / "xsi" / f"{WRAPPER}.v",
                "memory_v": root / "xsi" / "bram_t2p.v"}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfShotPlayTB to the XSI harness + main + both scenario bundles."
    consumes = ["rf_shot_play_source", "dut_ports"]
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
    consumes = ["rf_shot_tx_cpp", "run_tcl"]
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


def build_rf_shot_play_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_shot_play_source", path=HERE / "rf_shot_play.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_shot_play_dag,
                description="Build the rf_shot_play design: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
