"""rf_shot_rx_build.py — build the continuous-capture receiver: pysim -> codegen -> csynth.

``plans/t2p_lock_chan.md`` S2.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> the scenario in SimPy, clean **and** stalled: windows alternate between the two
                   halves, the ramp comes back contiguous, and the stalled run loses samples and
                   says so on the wire
    codegen_dut -> the ap_ctrl_none top (three tasks, two ``mode=bram`` ports, a plain input and a
                   **framed** window port), its tcl, its port map, the memory beside it, the WRAPPER
                   that joins them, the ``$dumpvars`` second top, and the hazard manifest
    codegen_tb  -> the XSI harness + main + the RF scenario bundle
    csynth      -> Vitis HLS; re-emits ``rtl_<wrapper>.f`` from the RTL on disk

The RTL rung is the ``-m xsi`` gate in ``tests/examples/test_rf_shot_rx_xsi.py``.

What a simulator elaborates is the **wrapper** (``rf_shot_rx_top``), not the kernel: the memory is
inside it, which is why the testbench sees only AXI-Stream and the BFM library needs no memory model.

**The reset trap does not bite this design, and the setting is here anyway.**
:class:`~waveflow.hw.rf_shot_rx.PingPongCapture` holds statics and is the owner — but an RX owner
*consumes*, so its first act is a blocking stream read and it stalls at reset like any requester.
``config_rtl -reset state`` is in :data:`SOLUTION_CONFIG` regardless: the statics are still state a
reset should clear, and a build that differed from the TX one only in this would be a difference
nobody could explain later.
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
    RfPingPongStep,
    RfShotBufStep,
    StreamUtilsStep,
    XsiHarnessStep,
)
from waveflow.build.trace_steps import AddVcdTopStep  # noqa: E402
from waveflow.build.wrapper_gen import bram_hazard_manifest  # noqa: E402
from waveflow.hw.arrayutils import ArrayUtilsStep  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.hw.locked_mem import LOCK_SCHEMA_CLASSES  # noqa: E402
from waveflow.hw.rf_shot_rx import CAPTURE_SCHEMA_CLASSES, RfShotRx  # noqa: E402
from waveflow.hw.rf_relayout import dense_elem_type, slot_elem_type, slots_per_word  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_shot_rx.rf_shot_rx import (  # noqa: E402
    BLKSIZE,
    DEPTH,
    SPW,
    WORD,
    RfShotRxAbs,
    RfShotRxTB,
    write_scenario,
)

#: The generated kernel's name, and the wrapper's.  The wrapper is what a simulator elaborates.
TOP = "rf_shot_rx"
WRAPPER = f"{TOP}_top"
#: The **second build of the same design** — ``plans/rf_shot_absolute.md`` S2.  One C++ body with an
#: ``ABS`` template argument Vitis folds, so this is a second *elaboration*, not a second source; it
#: needs its own name only because a Vitis project, an xsim snapshot and a generated ports header are
#: all keyed on the kernel's name and the two have to sit in one directory to be compared.
TOP_ABS = RfShotRxAbs.cpp_kernel_name
WRAPPER_ABS = f"{TOP_ABS}_top"
INCLUDE_DIR = "include"

#: Where the hazard manifest lands — the nets a read-during-write collision is visible on, resolved
#: from the wrapper's own wire names rather than matched by substring.
HAZARD_JSON = f"xsi/{TOP}_hazard.json"

#: RTL that must land in ``xsi/`` beside the ``.f`` naming it, in elaboration reading order.
RTL_FILES = ("bram_t2p.v", f"{WRAPPER}.v")
#: The same, for the absolute-index build.  The memory is shared — one ``bram_t2p.v`` serves both,
#: because it is the same memory and neither variant changes it.
RTL_FILES_ABS = ("bram_t2p.v", f"{WRAPPER_ABS}.v")

#: Solution-level tcl.  See the module docstring — kept for parity with the TX build rather than
#: because this design depends on it.
SOLUTION_CONFIG = ("config_rtl -reset state",)

#: Blocks the reader stalls for in the **fault-injection** pysim run.  Tuned, not guessed: at 5 the
#: loss starts and at 4 there is none, so 6 is comfortably past the edge while still leaving enough
#: windows for the gap to fall *between* two of them.  See ``PySimStep``.
STALL_BLOCKS = 6

#: The gated geometry, stated rather than defaulted: the 4x2's 64-bit word at four samples a beat, a
#: 256-word memory in two regions of 128, and blocks of 16 words.
_ELAB = {"bitwidth": int(WORD.bitwidth), "samp_per_word": slots_per_word(WORD),
         "depth": int(DEPTH), "shift": int(WORD.justify_shift()),
         "blk_words": int(BLKSIZE) // SPW}

#: The absolute-index build's parameters — **the same five numbers**, and one more.  Spelled out
#: rather than derived from :data:`_ELAB` by mutation so a reader can see that the geometry is
#: identical: what separates the two builds is the mode and nothing else.
_ELAB_ABS = dict(_ELAB, absolute_index=1)

#: Blocks the fault-injection run pushes under ``absolute_index``.  **60, not 40, and measured**: the
#: absolute build skips a busy region ENTIRELY where the default retries every block, so at 40 the
#: horizon closes before a window announced *after* the loss has been drained — and a stalled run
#: with no post-loss window cannot show the thing S2 exists to show.  At 60 there are two.
ABS_STALL_BLOCKS = 60


def generate_dut(out_dir: Path = HERE, *, comp_class=RfShotRx, top: str = TOP,
                 elab: dict | None = None, with_vcd: bool = True) -> Path:
    """Generate the schema headers, the lock header, the task bodies, the array utils, the top, its
    tcl, the port map, the memory, the wrapper, the ``$dumpvars`` top and the hazard manifest."""
    elab = dict(_ELAB if elab is None else elab)
    wrapper = f"{top}_top"
    hazard_json = f"xsi/{top}_hazard.json"
    word_bw = int(elab["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    # The dumper step consumes the design source (it is derived from the class), so the inner DAG
    # needs the same source node the outer one has.
    inner.add(SourceStep(artifact="rf_shot_rx_source", path=HERE / "rf_shot_rx.py"))
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    # `render_top` includes memmgr.hpp unconditionally, so a generated top needs it beside the
    # sources even when the design has no m_axi port at all.
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    # The re-layout body is Stage A's and shared; the two ping-pong bodies are this design's.
    inner.add(RfShotBufStep(output_dir=INCLUDE_DIR))
    inner.add(RfPingPongStep(output_dir=INCLUDE_DIR))
    inner.add(MemLockStep(output_dir=INCLUDE_DIR))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    # The window port is an `axi4s_word` stream, so the body calls write_axi4_stream -- which
    # DataSchemaStep emits by default.  The LOCK schemas need only the plain read_stream /
    # write_stream pair: the lock channels are INTERNAL edges, where ap_axis is refused (HLS 214-208).
    for cls in [*LOCK_SCHEMA_CLASSES, *CAPTURE_SCHEMA_CLASSES]:
        inner.add(DataSchemaStep(cls, word_bw_supported=[word_bw], include_dir=INCLUDE_DIR))
    # The serializers the re-layout body calls.  The SLOT element is the converter's container width
    # and the DENSE element the effective one; at 14-in-16 they differ, which is what makes the first
    # stage a conversion rather than a pair of wires.
    inner.add(ArrayUtilsStep(slot_elem_type(WORD, INCLUDE_DIR), [word_bw]))
    inner.add(ArrayUtilsStep(dense_elem_type(WORD, INCLUDE_DIR), [word_bw]))
    inner.add(GenRtlStep(name="place_memory", comp_class=_memory_class(), output_dir="xsi"))
    inner.add(GenWrapperStep(name="wrapper", comp_class=comp_class, elab_params=dict(elab),
                             width=word_bw, output_dir="xsi"))
    # The $dumpvars second top.  It names the WRAPPER, because that is what xsim elaborates here and
    # a $dumpvars naming a scope outside this elaboration is a hard error.
    if with_vcd:
        inner.add(AddVcdTopStep(name="vcd_dumper", comp_class=comp_class,
                                source_artifact="rf_shot_rx_source", output_dir="xsi", top=wrapper))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    comp = elaborate(comp_class, dict(elab), name=top)
    if comp.is_identity:
        raise RuntimeError(
            f"{top} elaborated with shift=0, which makes the first stage the IDENTITY — a build "
            f"that measures a pair of wires. Use a word type whose bits_per_samp differs from its "
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
    (out_dir / hazard_json).write_text(json.dumps(bram_hazard_manifest(comp, spec), indent=2),
                                       encoding="utf-8")
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl + xsi/{spec.top_name}_ports.h "
          f"+ xsi/{wrapper}.v + {hazard_json} ({len(spec.tasks)} tasks, "
          f"{len(spec.channels)} internal channels, {len(spec.ports)} ports)")
    return cpp


def hazard_manifest() -> dict:
    """The manifest for the gated configuration — for tests that want it without reading the file."""
    comp = elaborate(RfShotRx, dict(_ELAB), name=TOP)
    return bram_hazard_manifest(comp, composite_top_spec(comp, width=int(WORD.bitwidth)))


def _memory_class():
    """The memory class the design instantiates — read off the elaborated graph, not restated."""
    comp = elaborate(RfShotRx, dict(_ELAB))
    mems = {type(m) for m in comp.rtl_mods.values()}
    if len(mems) != 1:
        raise RuntimeError(f"expected exactly one RTL module class in {TOP}, got {mems}")
    return mems.pop()


def make_xsi_tb(dut_cls=RfShotRx) -> RfShotRxTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs.

    ``dut_cls`` selects the **build**, not the graph: ONE testbench serves both variants, because a
    second graph would be a second model of one design.
    """
    return RfShotRxTB(name="xsi_tb", sim=Simulation(), dut_cls=dut_cls)


def generate_tb(out_dir: Path = HERE, dut_cls=RfShotRx) -> None:
    """Generate the XSI harness + main from the TB graph, and write the RF scenario bundle."""
    tb = make_xsi_tb(dut_cls)
    spec = tb_top_spec(tb)
    top = spec.top_name
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{top}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{top}_bfm_tb.cpp").write_text(render_tb_main(spec, int(tb.n_cycles)), encoding="utf-8")
    write_scenario(xsi, n_blk=int(tb.n_blk))
    print(f"generated TB xsi/{top}_tb_harness.h + xsi/{top}_bfm_tb.cpp "
          f"({tb.n_cycles} cycles) + vectors/rf_in")


def wrapper_text() -> str:
    """The wrapper Verilog for the gated configuration — for tests that want it without a build."""
    from waveflow.build.wrapper_gen import render_wrapper, wrapper_spec

    comp = elaborate(RfShotRx, dict(_ELAB), name=TOP)
    return render_wrapper(wrapper_spec(comp, composite_top_spec(comp, width=int(WORD.bitwidth))))


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the scenario in SimPy, **clean and stalled** — the toolchain-free golden.

    Both, because neither is a subset of the other: the clean run says the design keeps up, and the
    stalled one says the gate can tell when it does not.  A clean pass on its own would be
    indistinguishable from a run that was never pushed.
    """

    description = "Run the RfShotRxTB pysim golden (the swap, the contiguity, the loss)."
    consumes = ["rf_shot_rx_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/rf_shot_rx_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        from examples.rf_shot_rx.rf_shot_rx import (
            RfShotRxAbs,
            check_addresses_are_the_phase,
            check_windows,
            expected_bases,
            frames_from_sink,
            run_pysim,
            windows_as_codes,
        )

        out: dict[str, object] = {}
        for name, stall in (("clean", 0), ("stalled", STALL_BLOCKS)):
            tb = run_pysim(stall_blocks=stall)
            frames = frames_from_sink(tb)
            flat = check_windows(frames, where=f"pysim {name}: ", expect_loss=bool(stall))
            wins = windows_as_codes(frames)
            if not stall:
                tb.dut.assert_ran(min_windows=2)
                tb.dut.assert_no_loss()
                tb.dut.assert_published_loss(frames, where="pysim clean: ")
            elif not tb.dut.n_dropped:
                raise AssertionError(
                    f"pysim {name}: the stalled reader lost nothing at stall_blocks="
                    f"{STALL_BLOCKS}; the fault injection is not reaching the design.")
            if tb.dut.window.bases != expected_bases(len(wins)):
                raise AssertionError(
                    f"pysim {name}: windows came from {tb.dut.window.bases}, not the alternating "
                    f"{expected_bases(len(wins))}")
            out[name] = {
                "windows": len(wins),
                "bases": list(tb.dut.window.bases),
                "headers": [(int(h.status), int(h.base_addr), int(h.n_dropped)) for h, _c in wins],
                "samples": int(flat.size),
                "first_code": int(flat[0]), "last_code": int(flat[-1]),
                "n_dropped": int(tb.dut.n_dropped),
                "blocks_in": int(tb.dut.capture.n_blocks),
                "words_written": int(tb.dut.capture.n_written),
                "adc_overrun": int(tb.adc_if.overrun) if hasattr(tb.adc_if, "overrun") else None,
            }
        # THE SECOND BUILD -- `plans/rf_shot_absolute.md` S2.  Toolchain-free, and the rung that
        # should fail first if the mode regresses; the DEFAULT build on the same stalled scenario is
        # its negative control, and both are filed so a reader without Vivado has the pair.
        for name, stall, n_blk, cls in (
                ("clean_abs", 0, None, RfShotRxAbs),
                ("stalled_abs", STALL_BLOCKS, ABS_STALL_BLOCKS, RfShotRxAbs),
                ("stalled_ctl", STALL_BLOCKS, ABS_STALL_BLOCKS, None)):
            kw = {} if n_blk is None else {"n_blk": int(n_blk)}
            if cls is not None:
                kw["dut_cls"] = cls
            tb = run_pysim(stall_blocks=stall, **kw)
            frames = frames_from_sink(tb)
            wins = windows_as_codes(frames)
            absolute = cls is not None
            failed = None
            if absolute:
                tb.dut.assert_windows_absolute(frames, where=f"pysim {name}: ")
                check_addresses_are_the_phase(frames, where=f"pysim {name}: ")
                if stall and not tb.dut.n_dropped:
                    raise AssertionError(
                        f"pysim {name}: the stalled reader lost nothing, so this run does not show "
                        f"what a drop does to an address.")
            else:
                # THE NEGATIVE CONTROL, run here so the toolchain-free rung carries it too: the same
                # assertion on the same scenario at absolute_index=0 must FAIL.
                try:
                    tb.dut.assert_windows_absolute(frames, where=f"pysim {name}: ")
                except AssertionError as e:
                    failed = str(e)
                if failed is None:
                    raise AssertionError(
                        f"pysim {name}: the DEFAULT build satisfied the absolute claim on a run "
                        f"that dropped {int(tb.dut.n_dropped)} word(s). Then absolute_index changes "
                        f"nothing and every gate above is asserting a property it already had.")
            out[name] = {
                "windows": len(wins),
                "bases": list(tb.dut.window.bases),
                "headers": [(int(h.status), int(h.base_addr), int(h.n_dropped)) for h, _c in wins],
                "n_dropped": int(tb.dut.n_dropped),
                "blocks_in": int(tb.dut.capture.n_blocks),
                "words_written": int(tb.dut.capture.n_written),
                "absolute_control_failed_with": failed,
            }
        out["geometry"] = {"depth": int(DEPTH), "blk_words": int(BLKSIZE) // SPW,
                           "bitwidth": int(WORD.bitwidth), "shift": int(WORD.justify_shift()),
                           "stall_blocks": STALL_BLOCKS, "abs_stall_blocks": ABS_STALL_BLOCKS}
        p = config.root_dir / "results" / "rf_shot_rx_pysim.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return {"pysim_results": p}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfShotRx to its ap_ctrl_none top + the memory + the wrapper."
    consumes = ["rf_shot_rx_source"]
    produces: ClassVar[dict] = {"rf_shot_rx_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
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
        return {"rf_shot_rx_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl",
                "dut_ports": root / "xsi" / f"{TOP}_ports.h",
                "wrapper_v": root / "xsi" / f"{WRAPPER}.v",
                "memory_v": root / "xsi" / "bram_t2p.v",
                "vcd_dumper": root / "xsi" / f"vcd_dumper_{WRAPPER}.v",
                "hazard_manifest": root / HAZARD_JSON}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfShotRxTB to the XSI harness + main + the RF scenario bundle."
    consumes = ["rf_shot_rx_source", "dut_ports"]
    produces: ClassVar[dict] = {"tb_harness": Path(f"xsi/{TOP}_tb_harness.h"),
                                "tb_main": Path(f"xsi/{TOP}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb(config.root_dir)
        return {"tb_harness": config.root_dir / "xsi" / f"{TOP}_tb_harness.h",
                "tb_main": config.root_dir / "xsi" / f"{TOP}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class CodegenDutAbsStep(BuildStep):
    """The SAME design at ``absolute_index = 1`` — a second elaboration, not a second source.

    Everything shared between the two builds (the task bodies, the schema headers, the lock, the
    memory) is re-emitted identically; what is new is the top, its tcl, its ports header and its
    wrapper, all named for :data:`TOP_ABS`.  The two therefore sit in one directory and one ``xsi/``
    without either one's artifacts standing in for the other's.
    """

    description = "Lower RfShotRxAbs (absolute_index=1) to its own top + wrapper."
    consumes = ["rf_shot_rx_source"]
    produces: ClassVar[dict] = {"rf_shot_rx_abs_cpp": Path(f"{GEN_DIR}/{TOP_ABS}.cpp"),
                                "run_tcl_abs": Path(f"{TOP_ABS}.tcl"),
                                "dut_ports_abs": Path(f"xsi/{TOP_ABS}_ports.h"),
                                "wrapper_v_abs": Path(f"xsi/{WRAPPER_ABS}.v"),
                                "vcd_dumper_abs": Path(f"xsi/vcd_dumper_{WRAPPER_ABS}.v"),
                                "hazard_manifest_abs": Path(f"xsi/{TOP_ABS}_hazard.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir, comp_class=RfShotRxAbs, top=TOP_ABS, elab=_ELAB_ABS)
        root = config.root_dir
        return {"rf_shot_rx_abs_cpp": root / GEN_DIR / f"{TOP_ABS}.cpp",
                "run_tcl_abs": root / f"{TOP_ABS}.tcl",
                "dut_ports_abs": root / "xsi" / f"{TOP_ABS}_ports.h",
                "wrapper_v_abs": root / "xsi" / f"{WRAPPER_ABS}.v",
                "vcd_dumper_abs": root / "xsi" / f"vcd_dumper_{WRAPPER_ABS}.v",
                "hazard_manifest_abs": root / "xsi" / f"{TOP_ABS}_hazard.json"}


@dataclass(kw_only=True)
class CodegenTbAbsStep(BuildStep):
    description = "Lower the SAME RfShotRxTB graph, cut at RfShotRxAbs, to its harness + main."
    consumes = ["rf_shot_rx_source", "dut_ports_abs"]
    produces: ClassVar[dict] = {"tb_harness_abs": Path(f"xsi/{TOP_ABS}_tb_harness.h"),
                                "tb_main_abs": Path(f"xsi/{TOP_ABS}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb(config.root_dir, RfShotRxAbs)
        return {"tb_harness_abs": config.root_dir / "xsi" / f"{TOP_ABS}_tb_harness.h",
                "tb_main_abs": config.root_dir / "xsi" / f"{TOP_ABS}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of the kernel — and the ``.f`` for the **wrapper**.

    Re-emitted from the RTL that is actually on disk, because a stale file list plus a cached
    ``xsimk.dll`` is how an XSI run goes green while proving nothing.
    """

    description = "Run Vitis HLS C-synthesis of the generated top."
    consumes = ["rf_shot_rx_cpp", "run_tcl"]
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
class CSynthAbsStep(BuildStep):
    """Vitis HLS C-synthesis of the absolute-index build — **both settings reach RTL**.

    A second ``csynth``, and the reason it is not optional: ``ABS`` is a template argument, so the
    two settings are two pieces of RTL.  A gate that only ever elaborated one of them would be
    asserting the mode's behaviour against a simulator — and on RX there is a second thing only an
    RTL run can say, which is that two regions still keep the writer and the reader apart.
    """

    description = "Run Vitis HLS C-synthesis of the absolute-index top."
    consumes = ["rf_shot_rx_abs_cpp", "run_tcl_abs"]
    produces: ClassVar[dict] = {"report_dir_abs": Path(f"{TOP_ABS}_proj/solution1")}
    params: ClassVar[dict] = {"live_output": False}

    def run(self, config: BuildConfig, live_output, **_) -> dict:
        result = toolchain.run_vitis_hls(config.root_dir / f"{TOP_ABS}.tcl",
                                         work_dir=config.root_dir,
                                         capture_output=not live_output)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        xsi = config.root_dir / "xsi"
        xsi.mkdir(parents=True, exist_ok=True)
        (xsi / f"rtl_{WRAPPER_ABS}.f").write_text(
            render_rtl_f(TOP_ABS, config.root_dir, extra=RTL_FILES_ABS), encoding="utf-8")
        return {"report_dir_abs": config.root_dir / f"{TOP_ABS}_proj" / "solution1"}


def build_rf_shot_rx_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_shot_rx_source", path=HERE / "rf_shot_rx.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    # THE SECOND BUILD.  Same source, same geometry, `absolute_index = 1` -- and its own top, so the
    # two snapshots can be elaborated against each other in one `xsi/`.
    dag.add(CodegenDutAbsStep(name="codegen_dut_abs"))
    dag.add(CodegenTbAbsStep(name="codegen_tb_abs"))
    dag.add(CSynthAbsStep(name="csynth_abs"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_shot_rx_dag,
                description="Build the rf_shot_rx design: pysim -> codegen -> csynth, for BOTH "
                            "settings of absolute_index.",
                default_through="csynth_abs",
                root_dir=HERE)
