"""rf_shot_unified_build.py — build the unified transmitter: pysim -> codegen -> csynth.

``plans/rf_shot_unify.md`` Stage A.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> both scenarios in SimPy: three passes then quiet, and a waveform switched mid-play
    codegen_dut -> the ap_ctrl_none top (three tasks, two ``mode=bram`` ports, a framed command port
                   and a framed response port), its tcl, its port map, the memory beside it, the
                   WRAPPER that joins them, and the ``$dumpvars`` second top
    codegen_tb  -> the XSI harness + main + both scenario bundles
    csynth      -> Vitis HLS; re-emits ``rtl_<wrapper>.f`` from the RTL on disk

The RTL rung is the ``-m xsi`` gate in ``tests/examples/test_rf_shot_unified_xsi.py``.

What a simulator elaborates is the **wrapper** (``rf_shot_tx_unified_top``), not the kernel.

**The reset trap is closed here and only here.**  The player holds four ``static``\\ s and **writes
before it reads** — writing without being asked is what *the side that cannot stop* means — so
``config_rtl -reset state`` is in :data:`SOLUTION_CONFIG`, which is what actually closed it under
Vitis 2025.1 in ``rf_repeat_play`` when the pragma alone did not.
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
    RfShotTxUnifiedStep,
    StreamUtilsStep,
    XsiHarnessStep,
)
from waveflow.build.trace_steps import AddVcdTopStep  # noqa: E402
from waveflow.build.wrapper_gen import bram_hazard_manifest  # noqa: E402
from waveflow.hw.arrayutils import ArrayUtilsStep  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.hw.locked_mem import LOCK_SCHEMA_CLASSES  # noqa: E402
from waveflow.hw.rf_relayout import dense_elem_type, slot_elem_type, slots_per_word  # noqa: E402
from waveflow.hw.rf_shot_tx import SHOT_TX_SCHEMA_CLASSES  # noqa: E402
from waveflow.hw.rf_shot_tx import (  # noqa: E402
    UNIFIED_TX_SCHEMA_CLASSES,
    RfShotTx,
)
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_shot_unified.rf_shot_unified import (  # noqa: E402
    BASE,
    BLKSIZE,
    DEPTH,
    NWORD,
    SCENARIOS,
    SPW,
    WORD,
    RfShotUnifiedTB,
    write_scenario,
)

#: The generated kernel's name, and the wrapper's.
TOP = "rf_shot_tx_unified"
WRAPPER = f"{TOP}_top"
INCLUDE_DIR = "include"

#: Where the hazard manifest lands — the nets a read-during-write collision is visible on.
HAZARD_JSON = f"xsi/{TOP}_hazard.json"

#: RTL that must land in ``xsi/`` beside the ``.f`` naming it, in elaboration reading order.
RTL_FILES = ("bram_t2p.v", f"{WRAPPER}.v")

#: Solution-level tcl.  See the module docstring.
SOLUTION_CONFIG = ("config_rtl -reset state",)

#: The gated geometry, stated rather than defaulted.  ``base = depth - nword`` puts the region at the
#: **top of the memory**, which is the point: ``base + offset`` is the shape of the byte-versus-word
#: bug, and a build that only ever loaded at zero would be measuring nothing.
_ELAB = {"bitwidth": int(WORD.bitwidth), "samp_per_word": slots_per_word(WORD),
         "depth": int(DEPTH), "nword": int(NWORD), "base": int(BASE),
         "shift": int(WORD.justify_shift()), "blk_words": int(BLKSIZE) // SPW}


def generate_dut(out_dir: Path = HERE) -> Path:
    """Generate the schema headers, the lock header, the task bodies, the array utils, the top, its
    tcl, the port map, the memory, the wrapper, the ``$dumpvars`` top and the hazard manifest."""
    word_bw = int(_ELAB["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    # The dumper step consumes the design source (it is derived from the class), so the inner DAG
    # needs the same source node the outer one has.
    inner.add(SourceStep(artifact="rf_shot_unified_source", path=HERE / "rf_shot_unified.py"))
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    # `render_top` includes memmgr.hpp unconditionally, so a generated top needs it beside the
    # sources even when the design has no m_axi port at all.
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    # The re-layout body is Stage A's and shared; the two merged bodies are this design's.
    inner.add(RfShotBufStep(output_dir=INCLUDE_DIR))
    inner.add(RfShotTxUnifiedStep(output_dir=INCLUDE_DIR))
    inner.add(MemLockStep(output_dir=INCLUDE_DIR))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    # THREE schema lists.  The header and the verdict are still rf_shot_tx's at Stage A -- see the
    # ownership decision in plans/rf_shot_unify.md -- the lock's are the lock's, and the play command
    # is the merged design's own.
    for cls in [*SHOT_TX_SCHEMA_CLASSES, *LOCK_SCHEMA_CLASSES, *UNIFIED_TX_SCHEMA_CLASSES]:
        inner.add(DataSchemaStep(cls, word_bw_supported=[word_bw], include_dir=INCLUDE_DIR))
    # The serializers the re-layout body calls.  The SLOT element is the converter's container width
    # and the DENSE element the effective one; at 14-in-16 they differ, which is what makes the last
    # stage a conversion rather than a pair of wires.
    inner.add(ArrayUtilsStep(slot_elem_type(WORD, INCLUDE_DIR), [word_bw]))
    inner.add(ArrayUtilsStep(dense_elem_type(WORD, INCLUDE_DIR), [word_bw]))
    inner.add(GenRtlStep(name="place_memory", comp_class=_memory_class(), output_dir="xsi"))
    inner.add(GenWrapperStep(name="wrapper", comp_class=RfShotTx, elab_params=dict(_ELAB),
                             width=word_bw, output_dir="xsi"))
    inner.add(AddVcdTopStep(name="vcd_dumper", comp_class=RfShotTx,
                            source_artifact="rf_shot_unified_source", output_dir="xsi",
                            top=WRAPPER))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    comp = elaborate(RfShotTx, dict(_ELAB), name=TOP)
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


def _memory_class():
    """The memory class the design instantiates — read off the elaborated graph, not restated."""
    comp = elaborate(RfShotTx, dict(_ELAB))
    mems = {type(m) for m in comp.rtl_mods.values()}
    if len(mems) != 1:
        raise RuntimeError(f"expected exactly one RTL module class in {TOP}, got {mems}")
    return mems.pop()


def make_xsi_tb() -> RfShotUnifiedTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs."""
    return RfShotUnifiedTB(name="xsi_tb", sim=Simulation())


def generate_tb(out_dir: Path = HERE) -> None:
    """Generate the XSI harness + main from the TB graph, and write **both** scenario bundles.

    Both, because the second is driven by a hand-written main beside the generated one
    (``rf_shot_tx_unified_loop.cpp``): the graph is identical and only the bundle names differ, so a
    second testbench *graph* would be a second model of one design.
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


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run **both** scenarios in SimPy — the toolchain-free golden."""

    description = "Run the RfShotUnifiedTB pysim golden (finite, infinite, and every verdict)."
    consumes = ["rf_shot_unified_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/rf_shot_unified_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        from examples.rf_shot_unified.rf_shot_unified import (
            check_finite_playout,
            check_loop_playout,
            check_responses,
            played_samples,
            responses,
            run_pysim,
            segments,
        )

        out: dict[str, object] = {}
        for name, frames in SCENARIOS:
            tb = run_pysim(frames=frames, in_bundle=f"vectors/{name}")
            check_responses(responses(tb), frames, where=f"pysim {name}: ")
            played = played_samples(tb)
            (check_finite_playout if name == "cmd" else check_loop_playout)(
                played, where=f"pysim {name}: ")
            out[name] = {
                "responses": responses(tb),
                "segments": [(bool(f), int(s.size)) for f, s in segments(played)],
                "played_samples": int(played.size),
                "n_plays": int(tb.dut.play.n_plays),
                "n_done": int(tb.dut.play.n_done),
                "grants": int(tb.dut.lock.n_grants),
                "underrun": int(tb.dac_if.underrun),
                "blocks_delivered": int(tb.dac_if.blocks_delivered),
            }
        p = config.root_dir / "results" / "rf_shot_unified_pysim.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return {"pysim_results": p}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfShotTx to its ap_ctrl_none top + the memory + the wrapper."
    consumes = ["rf_shot_unified_source"]
    produces: ClassVar[dict] = {"rf_shot_tx_unified_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
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
        return {"rf_shot_tx_unified_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl",
                "dut_ports": root / "xsi" / f"{TOP}_ports.h",
                "wrapper_v": root / "xsi" / f"{WRAPPER}.v",
                "memory_v": root / "xsi" / "bram_t2p.v",
                "vcd_dumper": root / "xsi" / f"vcd_dumper_{WRAPPER}.v",
                "hazard_manifest": root / HAZARD_JSON}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfShotUnifiedTB to the XSI harness + main + both scenario bundles."
    consumes = ["rf_shot_unified_source", "dut_ports"]
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
    consumes = ["rf_shot_tx_unified_cpp", "run_tcl"]
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


def build_rf_shot_unified_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_shot_unified_source", path=HERE / "rf_shot_unified.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_shot_unified_dag,
                description="Build the rf_shot_unified design: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
