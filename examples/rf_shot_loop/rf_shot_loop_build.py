"""rf_shot_loop_build.py — build the looping transmitter: pysim -> codegen -> csynth.

``plans/t2p_lock_chan.md`` S1.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> the scenario in SimPy: waveform A plays, the handover gaps, waveform B plays,
                   every header is answered, and the converter is never starved
    codegen_dut -> the ap_ctrl_none top (three tasks, two ``mode=bram`` ports, a **framed** command
                   port and a framed response port), its tcl, its port map, the memory placed beside
                   it, the WRAPPER that joins them, the ``$dumpvars`` second top, and the hazard
                   manifest naming the nets a read-during-write collision is visible on
    codegen_tb  -> the XSI harness + main + the scenario bundle
    csynth      -> Vitis HLS; re-emits ``rtl_<wrapper>.f`` from the RTL on disk

The RTL rung — the same scenario through real Verilog, bit-exact against the pysim golden, plus the
VCD scan for a read-during-write collision — is the ``-m xsi`` gate in
``tests/examples/test_rf_shot_loop_xsi.py``.

What a simulator elaborates is the **wrapper** (``rf_shot_tx_loop_top``), not the kernel: the memory
is inside it, which is why the testbench sees only AXI-Stream and the BFM library needs no memory
model.

**The reset trap is closed here and only here.**  :class:`~waveflow.hw.rf_shot_loop.ShotLoopPlay`
holds two ``static``\\ s **and writes before it reads** — writing without being asked is what *the
side that cannot stop* means — which is exactly ``reference-hls-task-reset-trap``.  The
``#pragma HLS reset`` in the body is not enough on its own under Vitis 2025.1; ``config_rtl -reset
state`` is what closed it in ``rf_repeat_play`` and it is in :data:`SOLUTION_CONFIG` for that reason.
"""
from __future__ import annotations

import json
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
    MemLockStep,
    MemMgrStep,
    RfShotBufStep,
    StreamUtilsStep,
    XsiHarnessStep,
)
from waveflow.build.trace_steps import AddVcdTopStep  # noqa: E402
from waveflow.build.wrapper_gen import bram_hazard_manifest  # noqa: E402
from waveflow.hw.arrayutils import ArrayUtilsStep  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.hw.locked_mem import LOCK_SCHEMA_CLASSES  # noqa: E402
from waveflow.hw.rf_relayout import dense_elem_type, slot_elem_type, slots_per_word  # noqa: E402
from waveflow.hw.rf_shot_loop import RfShotTxLoop  # noqa: E402
from waveflow.hw.rf_shot_tx import SHOT_TX_SCHEMA_CLASSES  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_shot_loop.rf_shot_loop import (  # noqa: E402
    BASE,
    DEPTH,
    GATE_FRAMES,
    NWORD,
    WORD,
    RfShotLoopDirtyTB,
    RfShotLoopTB,
    RfShotTxLoopDirty,
    write_scenario,
)

#: The generated kernel's name, and the wrapper's.  The wrapper is what a simulator elaborates.
TOP = "rf_shot_tx_loop"
WRAPPER = f"{TOP}_top"
INCLUDE_DIR = "include"

#: Where the hazard manifest lands — the nets a read-during-write collision is visible on, resolved
#: from the wrapper's own wire names rather than matched by substring.  Consumed by
#: :func:`waveflow.utils.bram_trace.find_read_during_write` after a traced run.
HAZARD_JSON = f"xsi/{TOP}_hazard.json"

#: RTL that must land in ``xsi/`` beside the ``.f`` naming it, in elaboration reading order: the
#: memory, then the wrapper that instantiates it.
RTL_FILES = ("bram_t2p.v", f"{WRAPPER}.v")

#: The POSITIVE CONTROL: the same composite with the player's ``playing = 0`` removed.  See
#: :class:`~examples.rf_shot_loop.rf_shot_loop.RfShotTxLoopDirty` -- the read-during-write scan on
#: the clean run is evidence only because the same scan on the same manifest finds hazards here.
#: Built as a second Vitis project in the same tree, so both tops share one ``xsi/`` directory.
DIRTY_TOP = "rf_shot_tx_loop_dirty"
DIRTY_WRAPPER = f"{DIRTY_TOP}_top"
DIRTY_RTL_FILES = ("bram_t2p.v", f"{DIRTY_WRAPPER}.v")
DIRTY_HAZARD_JSON = f"xsi/{DIRTY_TOP}_hazard.json"

#: Solution-level tcl.  See the module docstring — the player is on the wrong side of the reset trap
#: by construction, and this is the setting that closed it.
SOLUTION_CONFIG = ("config_rtl -reset state",)

#: The gated geometry, stated rather than defaulted.  ``base = depth - nword`` puts the region at the
#: **top of the memory**, which is the point: ``base + offset`` is the shape of the byte-versus-word
#: bug, and a build that only ever loaded at zero would be measuring nothing.
_ELAB = {"bitwidth": int(WORD.bitwidth), "samp_per_word": slots_per_word(WORD),
         "depth": int(DEPTH), "nword": int(NWORD), "base": int(BASE),
         "shift": int(WORD.justify_shift()),
         "blk_words": 64 // slots_per_word(WORD)}


def generate_dut(out_dir: Path = HERE) -> Path:
    """Generate the schema headers, the lock header, the array utils, the top, its tcl, the port map,
    the memory, the wrapper, the ``$dumpvars`` top and the hazard manifest."""
    word_bw = int(_ELAB["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    # The dumper step consumes the design source (it is derived from the class), so the inner DAG
    # needs the same source node the outer one has.
    inner.add(SourceStep(artifact="rf_shot_loop_source", path=HERE / "rf_shot_loop.py"))
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    # `render_top` includes memmgr.hpp unconditionally, so a generated top needs it beside the
    # sources even when the design has no m_axi port at all.
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(RfShotBufStep(output_dir=INCLUDE_DIR))
    inner.add(MemLockStep(output_dir=INCLUDE_DIR))
    # The POSITIVE CONTROL's body is copied HERE rather than in generate_dirty, and the ordering is
    # load-bearing: `rtl_staleness` hashes the sources a design was synthesized against, and
    # include/ is one directory for both tops -- so a file that appeared AFTER the clean csynth makes
    # every clean gate skip, which is the one outcome worse than a failure.
    inner.add(SourceStep(artifact="dirty_task_source", path=HERE / "src" /
                         "shot_loop_play_dirty_task.h"))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    # The command and response ports are `axi4s_word` streams, so the loader calls
    # read_axi4_stream / write_axi4_stream -- which DataSchemaStep emits by default.  The two LOCK
    # schemas need only the plain read_stream / write_stream pair: the lock channels are INTERNAL
    # edges, where ap_axis is refused outright (HLS 214-208).
    for cls in [*SHOT_TX_SCHEMA_CLASSES, *LOCK_SCHEMA_CLASSES]:
        inner.add(DataSchemaStep(cls, word_bw_supported=[word_bw], include_dir=INCLUDE_DIR))
    # The serializers the re-layout body calls.  The SLOT element is the converter's container width
    # and the DENSE element the effective one; at 14-in-16 they differ, which is what makes the last
    # stage a conversion rather than a pair of wires.
    inner.add(ArrayUtilsStep(slot_elem_type(WORD, INCLUDE_DIR), [word_bw]))
    inner.add(ArrayUtilsStep(dense_elem_type(WORD, INCLUDE_DIR), [word_bw]))
    inner.add(GenRtlStep(name="place_memory", comp_class=_memory_class(), output_dir="xsi"))
    inner.add(GenWrapperStep(name="wrapper", comp_class=RfShotTxLoop, elab_params=dict(_ELAB),
                             width=word_bw, output_dir="xsi"))
    # The $dumpvars second top.  It names the WRAPPER, because that is what xsim elaborates here and
    # a $dumpvars naming a scope outside this elaboration is a hard error.
    inner.add(AddVcdTopStep(name="vcd_dumper", comp_class=RfShotTxLoop,
                            source_artifact="rf_shot_loop_source", output_dir="xsi", top=WRAPPER))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    inc = out_dir / INCLUDE_DIR
    inc.mkdir(parents=True, exist_ok=True)
    (inc / "shot_loop_play_dirty_task.h").write_text(
        (out_dir / "src" / "shot_loop_play_dirty_task.h").read_text(encoding="utf-8"),
        encoding="utf-8")

    comp = elaborate(RfShotTxLoop, dict(_ELAB), name=TOP)
    if comp.is_identity:
        raise RuntimeError(
            f"{TOP} elaborated with shift=0, which makes the last stage the IDENTITY — a build that "
            f"measures a pair of wires. Use a word type whose bits_per_samp differs from its "
            f"bits_per_samp_pack (Rfsoc4x2SampWord).")
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
    (out_dir / HAZARD_JSON).write_text(json.dumps(bram_hazard_manifest(comp, spec), indent=2),
                                       encoding="utf-8")
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl + xsi/{spec.top_name}_ports.h "
          f"+ xsi/{WRAPPER}.v + {HAZARD_JSON} ({len(spec.tasks)} tasks, "
          f"{len(spec.channels)} internal channels, {len(spec.ports)} ports)")
    return cpp


def generate_dirty(out_dir: Path = HERE) -> Path:
    """Generate the positive control's top, tcl, wrapper, dumper and hazard manifest.

    It reuses the clean build's ``include/`` wholesale — the two designs differ in **one line of one
    task body**, which is the point — and adds only the broken body from ``src/`` and the four
    artifacts that carry its own name.
    """
    word_bw = int(_ELAB["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    inner.add(SourceStep(artifact="rf_shot_loop_source", path=HERE / "rf_shot_loop.py"))
    inner.add(GenWrapperStep(name="dirty_wrapper", comp_class=RfShotTxLoopDirty,
                             elab_params=dict(_ELAB), width=word_bw, output_dir="xsi"))
    inner.add(AddVcdTopStep(name="dirty_vcd_dumper", comp_class=RfShotTxLoopDirty,
                            source_artifact="rf_shot_loop_source", output_dir="xsi",
                            top=DIRTY_WRAPPER))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"dirty gen failed: {failed}")

    comp = elaborate(RfShotTxLoopDirty, dict(_ELAB), name=DIRTY_TOP)
    spec = composite_top_spec(comp, width=word_bw)
    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{DIRTY_TOP}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{DIRTY_TOP}.tcl").write_text(
        render_tcl(DIRTY_TOP, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS,
                   solution_config=SOLUTION_CONFIG),
        encoding="utf-8")
    xsi = out_dir / "xsi"
    (xsi / f"{DIRTY_TOP}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    (out_dir / DIRTY_HAZARD_JSON).write_text(
        json.dumps(bram_hazard_manifest(comp, spec), indent=2), encoding="utf-8")
    print(f"generated CONTROL {cpp.name} + {DIRTY_TOP}.tcl + xsi/{DIRTY_WRAPPER}.v "
          f"+ {DIRTY_HAZARD_JSON}")
    return cpp


def hazard_manifest() -> dict:
    """The manifest for the gated configuration — for tests that want it without reading the file."""
    comp = elaborate(RfShotTxLoop, dict(_ELAB), name=TOP)
    return bram_hazard_manifest(comp, composite_top_spec(comp, width=int(WORD.bitwidth)))


def _memory_class():
    """The memory class the design instantiates — read off the elaborated graph, not restated."""
    comp = elaborate(RfShotTxLoop, dict(_ELAB))
    mems = {type(m) for m in comp.rtl_mods.values()}
    if len(mems) != 1:
        raise RuntimeError(f"expected exactly one RTL module class in {TOP}, got {mems}")
    return mems.pop()


def make_xsi_tb() -> RfShotLoopTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs."""
    return RfShotLoopTB(name="xsi_tb", sim=Simulation())


def generate_tb(out_dir: Path = HERE) -> None:
    """Generate **both** XSI harnesses + mains from the TB graphs, and write the scenario bundle.

    Two harnesses, because a harness hardcodes ``DESIGN_DLL`` — the elaborated snapshot it loads.
    The positive control running against the shipped design's snapshot would report the shipped
    design's numbers and find no hazard, which is the single most convincing way for this gate to
    lie.  One scenario, though: the two designs differ in one line of one task body and in nothing a
    host can see.
    """
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    # BOTH testbenches are named `xsi_tb`, and that is deliberate: a harness member takes the TB
    # graph's own name, so two different names would make the two mains textually different for no
    # reason and the control's would drift from the one it is supposed to mirror.  The namespaces
    # are already distinct -- they come from the DUT's cpp_kernel_name.
    for name, tb in ((TOP, make_xsi_tb()),
                     (DIRTY_TOP, RfShotLoopDirtyTB(name="xsi_tb", sim=Simulation()))):
        spec = tb_top_spec(tb)
        (xsi / f"{name}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
        (xsi / f"{name}_bfm_tb.cpp").write_text(render_tb_main(spec, int(tb.n_cycles)),
                                                encoding="utf-8")
    write_scenario(xsi, GATE_FRAMES, name="cmd")
    print(f"generated TB xsi/{{{TOP},{DIRTY_TOP}}}_tb_harness.h + _bfm_tb.cpp + vectors/cmd")


def wrapper_text() -> str:
    """The wrapper Verilog for the gated configuration — for tests that want it without a build."""
    from waveflow.build.wrapper_gen import render_wrapper, wrapper_spec

    comp = elaborate(RfShotTxLoop, dict(_ELAB), name=TOP)
    return render_wrapper(wrapper_spec(comp, composite_top_spec(comp, width=int(WORD.bitwidth))))


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the scenario in SimPy — the toolchain-free golden.

    What it records is what the RTL rung is compared against **and** what a reader of this design
    wants: where the handover fell, how long it lasted, and that the converter was never starved
    through it.
    """

    description = "Run the RfShotLoopTB pysim golden (the switch, the verdicts, the DAC)."
    consumes = ["rf_shot_loop_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/rf_shot_loop_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        from examples.rf_shot_loop.rf_shot_loop import (
            BLKSIZE,
            STARTUP_BLOCKS,
            check_responses,
            check_switched,
            expected_loads,
            played_samples,
            responses,
            run_pysim,
            segments,
        )

        tb = run_pysim()
        check_responses(responses(tb), where="pysim: ")
        played = played_samples(tb)
        check_switched(played, where="pysim: ")
        tb.dut.assert_handover(expected_loads())
        segs = [(bool(f), int(s.size)) for f, s in segments(played)]
        if segs[0][1] != STARTUP_BLOCKS * BLKSIZE:
            raise AssertionError(
                f"pysim: the startup gap is {segs[0][1]} samples, not "
                f"{STARTUP_BLOCKS * BLKSIZE}. The playout is bit-exact either way, so only this "
                f"says the pipeline's latency moved.")
        out = {
            "responses": responses(tb),
            "segments": segs,
            "played_samples": int(played.size),
            "underrun": int(tb.dac_if.underrun),
            "blocks_delivered": int(tb.dac_if.blocks_delivered),
            "n_chunks": int(tb.dut.play.n_chunks),
            "n_filler": int(tb.dut.play.n_filler),
            "n_resumed": int(tb.dut.play.n_resumed),
            "grant_wait_cycles": [round(x * float(tb.axis_freq), 2)
                                  for x in tb.dut.load.lock.grant_waits],
            "geometry": {"nword": int(NWORD), "depth": int(DEPTH), "base": int(BASE),
                         "bitwidth": int(WORD.bitwidth), "shift": int(WORD.justify_shift()),
                         "words_per_cycle": tb.words_per_cycle},
        }
        p = config.root_dir / "results" / "rf_shot_loop_pysim.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return {"pysim_results": p}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfShotTxLoop to its ap_ctrl_none top + the memory + the wrapper."
    consumes = ["rf_shot_loop_source"]
    produces: ClassVar[dict] = {"rf_shot_tx_loop_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl"),
                                "dut_ports": Path(f"xsi/{TOP}_ports.h"),
                                "wrapper_v": Path(f"xsi/{WRAPPER}.v"),
                                "memory_v": Path("xsi/bram_t2p.v"),
                                "vcd_dumper": Path(f"xsi/vcd_dumper_{WRAPPER}.v"),
                                "hazard_manifest": Path(HAZARD_JSON)}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"rf_shot_tx_loop_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl",
                "dut_ports": root / "xsi" / f"{TOP}_ports.h",
                "wrapper_v": root / "xsi" / f"{WRAPPER}.v",
                "memory_v": root / "xsi" / "bram_t2p.v",
                "vcd_dumper": root / "xsi" / f"vcd_dumper_{WRAPPER}.v",
                "hazard_manifest": root / HAZARD_JSON}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfShotLoopTB to the XSI harness + main + the scenario bundle."
    consumes = ["rf_shot_loop_source", "dut_ports"]
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
    consumes = ["rf_shot_tx_loop_cpp", "run_tcl"]
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


@dataclass(kw_only=True)
class CodegenDirtyStep(BuildStep):
    description = "Lower the POSITIVE CONTROL (RfShotTxLoopDirty) to its own top + wrapper."
    # `dut_ports` is a dependency for ORDER, not for content: the control's body lands in the shared
    # include/ during codegen_dut, and a build that reached here first would leave the clean design
    # synthesized against a directory that changed underneath it -- which `rtl_staleness` reports as
    # "not this checkout's RTL" and every clean gate then SKIPS.
    consumes = ["rf_shot_loop_source", "dut_ports"]
    produces: ClassVar[dict] = {"dirty_cpp": Path(f"{GEN_DIR}/{DIRTY_TOP}.cpp"),
                                "dirty_tcl": Path(f"{DIRTY_TOP}.tcl"),
                                "dirty_wrapper_v": Path(f"xsi/{DIRTY_WRAPPER}.v"),
                                "dirty_hazard": Path(DIRTY_HAZARD_JSON)}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dirty(config.root_dir)
        root = config.root_dir
        return {"dirty_cpp": root / GEN_DIR / f"{DIRTY_TOP}.cpp",
                "dirty_tcl": root / f"{DIRTY_TOP}.tcl",
                "dirty_wrapper_v": root / "xsi" / f"{DIRTY_WRAPPER}.v",
                "dirty_hazard": root / DIRTY_HAZARD_JSON}


@dataclass(kw_only=True)
class CSynthDirtyStep(BuildStep):
    """C-synthesis of the positive control.  A second project, in the same tree.

    Its cost is the price of the clean run's scan meaning anything: without a run known to collide,
    "no hazards found" and "bound to the wrong nets" are the same output.
    """

    description = "Run Vitis HLS C-synthesis of the positive control."
    # `report_dir` is the clean csynth's, and it is here for ORDER: both designs are synthesized
    # against one include/, so the clean one has to be finished before this project touches it.
    consumes = ["dirty_cpp", "dirty_tcl", "report_dir"]
    produces: ClassVar[dict] = {"dirty_report_dir": Path(f"{DIRTY_TOP}_proj/solution1")}
    params: ClassVar[dict] = {"live_output": False}

    def run(self, config: BuildConfig, live_output, **_) -> dict:
        result = toolchain.run_vitis_hls(config.root_dir / f"{DIRTY_TOP}.tcl",
                                         work_dir=config.root_dir,
                                         capture_output=not live_output)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        xsi = config.root_dir / "xsi"
        (xsi / f"rtl_{DIRTY_WRAPPER}.f").write_text(
            render_rtl_f(DIRTY_TOP, config.root_dir, extra=DIRTY_RTL_FILES), encoding="utf-8")
        return {"dirty_report_dir": config.root_dir / f"{DIRTY_TOP}_proj" / "solution1"}


def build_rf_shot_loop_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_shot_loop_source", path=HERE / "rf_shot_loop.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    dag.add(CodegenDirtyStep(name="codegen_dirty"))
    dag.add(CSynthDirtyStep(name="csynth_dirty"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_shot_loop_dag,
                description=("Build the rf_shot_loop design: pysim -> codegen -> csynth, then the "
                             "positive control the RTL hazard scan needs."),
                default_through="csynth_dirty",
                root_dir=HERE)
