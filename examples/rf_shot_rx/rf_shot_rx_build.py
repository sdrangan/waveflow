"""rf_shot_rx_build.py — build the continuous-capture receiver: pysim -> codegen -> csynth.

PR #179.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> the scenario in SimPy, clean **and** stalled: windows alternate between the two
                   halves, the ramp comes back contiguous, and the stalled run loses samples and
                   says so on the wire
    codegen_dut -> the ap_ctrl_none top (three tasks, two ``mode=bram`` ports, a plain input and a
                   **framed** window port), its headers and its tcl
    csynth      -> Vitis HLS; stamps the sources the RTL was built from

and the same two rungs again for the absolute-index build (``codegen_dut_abs``, ``csynth_abs``).

The RTL rung is the **system flow** (``rf_shot_rx_system_build.py``), one system per build; the
``-m xsi`` gates are ``tests/examples/test_rf_shot_rx_xsi.py`` and ``..._abs_xsi.py``.  The system
top instantiates the kernel as its **wrapper** (the kernel and its ``bram_t2p``), so the testbench
sees only AXI-Stream and the BFM library needs no memory model.

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
    render_tcl,
    render_top,
)
from waveflow.build.elaborate import elaborate  # noqa: E402
from waveflow.build.rtl_digest import write_stamp  # noqa: E402
from waveflow.build.streamutils import (  # noqa: E402
    MemLockStep,
    MemMgrStep,
    RfPingPongStep,
    RfShotBufStep,
    StreamUtilsStep,
)
from waveflow.hw.arrayutils import ArrayUtilsStep  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.hw.locked_mem import LOCK_SCHEMA_CLASSES  # noqa: E402
from waveflow.hw.rf_shot_rx import CAPTURE_SCHEMA_CLASSES, RfShotRx  # noqa: E402
from waveflow.hw.rf_relayout import dense_elem_type, slot_elem_type, slots_per_word  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_shot_rx.rf_shot_rx import (  # noqa: E402
    BLKSIZE,
    DEPTH,
    SPW,
    WORD,
    RfShotRxAbs,
)

#: The generated kernel's name.
TOP = "rf_shot_rx"
#: The **second build of the same design** — PR #201.  One C++ body with an
#: ``ABS`` template argument Vitis folds, so this is a second *elaboration*, not a second source; it
#: needs its own name only because a Vitis project and a system top are keyed on the kernel's
#: name and the two have to sit in one directory to be compared.
TOP_ABS = RfShotRxAbs.cpp_kernel_name
INCLUDE_DIR = "include"

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
                 elab: dict | None = None) -> Path:
    """Generate the schema headers, the lock header, the task bodies, the array utils, the top and
    its tcl -- what csynth compiles.  The wrapper joining the kernel to its memory is the system
    flow's (rendered into the system top)."""
    elab = dict(_ELAB if elab is None else elab)
    word_bw = int(elab["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    # `render_top` includes memmgr.hpp unconditionally, so a generated top needs it beside the
    # sources even when the design has no m_axi port at all.
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    # The re-layout body is Stage A's and shared; the two ping-pong bodies are this design's.
    inner.add(RfShotBufStep(output_dir=INCLUDE_DIR))
    inner.add(RfPingPongStep(output_dir=INCLUDE_DIR))
    inner.add(MemLockStep(output_dir=INCLUDE_DIR))
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
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl ({len(spec.tasks)} tasks, "
          f"{len(spec.channels)} internal channels, {len(spec.ports)} ports)")
    return cpp


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
        # THE SECOND BUILD -- PR #201.  Toolchain-free, and the rung that
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
    description = "Lower RfShotRx to its ap_ctrl_none top, its headers and its tcl."
    consumes = ["rf_shot_rx_source"]
    produces: ClassVar[dict] = {"rf_shot_rx_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"rf_shot_rx_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl"}


@dataclass(kw_only=True)
class CodegenDutAbsStep(BuildStep):
    """The SAME design at ``absolute_index = 1`` — a second elaboration, not a second source.

    Everything shared between the two builds (the task bodies, the schema headers, the lock) is
    re-emitted identically; what is new is the top and its tcl, named for :data:`TOP_ABS`.  The two
    therefore sit in one directory without either one's artifacts standing in for the other's.
    """

    description = "Lower RfShotRxAbs (absolute_index=1) to its own top and tcl."
    consumes = ["rf_shot_rx_source"]
    produces: ClassVar[dict] = {"rf_shot_rx_abs_cpp": Path(f"{GEN_DIR}/{TOP_ABS}.cpp"),
                                "run_tcl_abs": Path(f"{TOP_ABS}.tcl")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir, comp_class=RfShotRxAbs, top=TOP_ABS, elab=_ELAB_ABS)
        root = config.root_dir
        return {"rf_shot_rx_abs_cpp": root / GEN_DIR / f"{TOP_ABS}.cpp",
                "run_tcl_abs": root / f"{TOP_ABS}.tcl"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of the kernel — and the source stamp the system flow checks.

    The stamp (:func:`~waveflow.build.rtl_digest.write_stamp`) records the sources this RTL was built
    from, so the system run's ``synth="check"`` accepts it, and refuses it once a source changes.
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
        write_stamp(config.root_dir, TOP)
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
        write_stamp(config.root_dir, TOP_ABS)
        return {"report_dir_abs": config.root_dir / f"{TOP_ABS}_proj" / "solution1"}


def build_rf_shot_rx_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_shot_rx_source", path=HERE / "rf_shot_rx.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CSynthStep(name="csynth"))
    # THE SECOND BUILD.  Same source, same geometry, `absolute_index = 1` -- and its own top, so the
    # two csynth projects sit side by side (the system flow runs each as a system of its own).
    dag.add(CodegenDutAbsStep(name="codegen_dut_abs"))
    dag.add(CSynthAbsStep(name="csynth_abs"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_shot_rx_dag,
                description="Build the rf_shot_rx design: pysim -> codegen -> csynth, for BOTH "
                            "settings of absolute_index.",
                default_through="csynth_abs",
                root_dir=HERE)
