"""rf_shot_tx_build.py — build the shot transmitter: pysim -> codegen -> csynth.

PR #181.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> both scenarios in SimPy: three passes then quiet, and a waveform switched mid-play
    codegen_dut -> the ap_ctrl_none top (three tasks, two ``mode=bram`` ports, a framed command port
                   and a framed response port), its headers and its tcl
    csynth      -> Vitis HLS; stamps the sources the RTL was built from

and the same two rungs again for the absolute-index build (``codegen_dut_abs``, ``csynth_abs``).

The RTL rung is the **system flow** (``rf_shot_tx_system_build.py``), one system per build and frame
stream; the ``-m xsi`` gates are ``tests/examples/test_rf_shot_tx_xsi.py`` and ``..._abs_xsi.py``.
The system top instantiates the kernel as its **wrapper** (the kernel and its ``bram_t2p``).

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
    render_tcl,
    render_top,
)
from waveflow.build.elaborate import elaborate  # noqa: E402
from waveflow.build.rtl_digest import write_stamp  # noqa: E402
from waveflow.build.streamutils import (  # noqa: E402
    MemLockStep,
    MemMgrStep,
    RfShotBufStep,
    RfShotTxStep,
    StreamUtilsStep,
)
from waveflow.hw.arrayutils import ArrayUtilsStep  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.hw.locked_mem import LOCK_SCHEMA_CLASSES  # noqa: E402
from waveflow.hw.rf_relayout import dense_elem_type, slot_elem_type, slots_per_word  # noqa: E402
from waveflow.hw.rf_shot_tx import shot_tx_schemas  # noqa: E402
from waveflow.hw.rf_shot_tx import (  # noqa: E402
    SHOT_PLAY_SCHEMA_CLASSES,
    RfShotTx,
)
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_shot_tx.rf_shot_tx_figures import (  # noqa: E402
    PlayoutFigureStep,
    SyncDocsFiguresStep,
)

from examples.rf_shot_tx.rf_shot_tx import (  # noqa: E402
    BLKSIZE,
    DEPTH,
    SCENARIOS,
    SPW,
    WORD,
    RfShotTxAbs,
)

#: The generated kernel's name.
TOP = "rf_shot_tx"
#: The **second build of the same design** — PR #199.  One C++ body with an
#: ``ABS`` template argument Vitis folds, so this is a second *elaboration*, not a second source; it
#: needs its own name only because a Vitis project and a system top are keyed on the kernel's
#: name and the two have to sit in one directory to be compared.
TOP_ABS = RfShotTxAbs.cpp_kernel_name
INCLUDE_DIR = "include"

#: Solution-level tcl.  See the module docstring.
SOLUTION_CONFIG = ("config_rtl -reset state",)

#: The gated geometry, stated rather than defaulted.  **Four numbers where there were six**: since
#: PR #194 the shot IS the buffer, so ``depth`` is the length as well as the
#: size and there is no ``nword`` and no ``base``.  What the removed placement used to exercise —
#: ``base + offset`` — does not exist to be wrong; the wrap that replaced it is gated on the read
#: port at RTL.
_ELAB = {"bitwidth": int(WORD.bitwidth), "samp_per_word": slots_per_word(WORD),
         "depth": int(DEPTH),
         "shift": int(WORD.justify_shift()), "blk_words": int(BLKSIZE) // SPW}

#: The absolute-index build's parameters — **the same four numbers**, and one more.  Spelled out
#: rather than derived from :data:`_ELAB` by mutation so a reader can see that the geometry is
#: identical: what separates the two builds is the mode and nothing else.
_ELAB_ABS = dict(_ELAB, absolute_index=1)


def generate_dut(out_dir: Path = HERE, *, comp_class=RfShotTx, top: str = TOP,
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
    # The re-layout body is Stage A's and shared; the two merged bodies are this design's.
    inner.add(RfShotBufStep(output_dir=INCLUDE_DIR))
    inner.add(RfShotTxStep(output_dir=INCLUDE_DIR))
    inner.add(MemLockStep(output_dir=INCLUDE_DIR))
    # THREE schema lists.  The header and the verdict are still rf_shot_tx's at Stage A -- see the
    # ownership decision in PR #181 -- the lock's are the lock's, and the play command
    # is the merged design's own.
# The header/response pair for THIS geometry, not the module default: plans/rf_shot_wire_format.md
    # Part A derives nsamp_loaded's width from depth x samp_per_word, so the emitted C++ has to be the
    # design's own pair or the twin would parse a different wire.
    hdr_cls, resp_cls = shot_tx_schemas(int(elab["depth"]), int(elab["samp_per_word"]))
    for cls in [hdr_cls, resp_cls, *LOCK_SCHEMA_CLASSES, *SHOT_PLAY_SCHEMA_CLASSES]:
        inner.add(DataSchemaStep(cls, word_bw_supported=[word_bw], include_dir=INCLUDE_DIR))
    # The serializers the re-layout body calls.  The SLOT element is the converter's container width
    # and the DENSE element the effective one; at 14-in-16 they differ, which is what makes the last
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
            f"{top} elaborated with shift=0, which makes the last stage the IDENTITY — a build that "
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
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl ({len(spec.tasks)} tasks, "
          f"{len(spec.channels)} internal channels, {len(spec.ports)} ports)")
    return cpp


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run **both** scenarios in SimPy — the toolchain-free golden."""

    description = "Run the RfShotTxTB pysim golden (finite, infinite, and every verdict)."
    consumes = ["rf_shot_tx_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/rf_shot_tx_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        from examples.rf_shot_tx.rf_shot_tx import (
            c_lead,
            check_finite_playout,
            check_loop_playout,
            check_phase,
            check_responses,
            played_samples,
            responses,
            run_pysim,
            segments,
            transients,
        )

        from examples.rf_shot_tx.rf_shot_tx import (
            RfShotTxAbs,
            check_address_is_the_phase,
            check_starts_on_a_boundary,
            play_log,
        )

        out: dict[str, object] = {}
        for name, frames in SCENARIOS:
            tb = run_pysim(frames=frames, in_bundle=f"vectors/{name}")
            check_responses(responses(tb), frames, where=f"pysim {name}: ")
            played = played_samples(tb)
            (check_finite_playout if name == "cmd" else check_loop_playout)(
                played, where=f"pysim {name}: ")
            # LT gate 1 (`plans/lt_transient.md` S2), run here as well as in the XSI gate: it needs
            # no toolchain, and it is the check that makes relaxing the cross-backend comparison
            # safe -- so the toolchain-free rung should be able to fail on it.
            check_phase(played, where=f"pysim {name}: ")
            startup, handovers = transients(played)
            out[name] = {
                "responses": responses(tb),
                "segments": [(bool(f), int(s.size)) for f, s in segments(played)],
                "played_samples": int(played.size),
                "startup_transient": startup,
                "handover_transients": handovers,
                "c_lead": int(c_lead(tb)),
                "n_plays": int(tb.dut.play.n_plays),
                "n_done": int(tb.dut.play.n_done),
                "grants": int(tb.dut.lock.n_grants),
                "underrun": int(tb.dac_if.underrun),
                "blocks_delivered": int(tb.dac_if.blocks_delivered),
            }
            # THE SECOND BUILD, on the SAME scenario -- PR #199.  Toolchain-free,
            # and the rung that should fail first if the mode regresses: the two gates the parameter
            # exists for are asserted here, and the default run above is their negative control.
            tb_abs = run_pysim(frames=frames, in_bundle=f"vectors/{name}", dut_cls=RfShotTxAbs)
            check_responses(responses(tb_abs), frames, where=f"pysim abs {name}: ")
            played_abs = played_samples(tb_abs)
            check_phase(played_abs, where=f"pysim abs {name}: ")
            starts = [r.start for r in play_log(played_abs)]
            if starts:
                check_starts_on_a_boundary(played_abs, where=f"pysim abs {name}: ")
                check_address_is_the_phase(played_abs, where=f"pysim abs {name}: ")
            out[f"{name}_abs"] = {
                "responses": responses(tb_abs),
                "segments": [(bool(f), int(s.size)) for f, s in segments(played_abs)],
                "play_starts": starts,
                "startup_transient": transients(played_abs)[0],
                "n_plays": int(tb_abs.dut.play.n_plays),
                "n_done": int(tb_abs.dut.play.n_done),
                "underrun": int(tb_abs.dac_if.underrun),
            }
        p = config.root_dir / "results" / "rf_shot_tx_pysim.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return {"pysim_results": p}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfShotTx to its ap_ctrl_none top, its headers and its tcl."
    consumes = ["rf_shot_tx_source"]
    produces: ClassVar[dict] = {"rf_shot_tx_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"rf_shot_tx_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl"}


@dataclass(kw_only=True)
class CodegenDutAbsStep(BuildStep):
    """The SAME design at ``absolute_index = 1`` — a second elaboration, not a second source.

    Everything shared between the two builds (the task bodies, the schema headers, the lock) is
    re-emitted identically; what is new is the top and its tcl, named for :data:`TOP_ABS`. The two
    therefore sit in one directory without either one's artifacts standing in for the other's —
    which is the whole reason the variant needs a name at all.
    """

    description = "Lower RfShotTxAbs (absolute_index=1) to its own top and tcl."
    consumes = ["rf_shot_tx_source"]
    produces: ClassVar[dict] = {"rf_shot_tx_abs_cpp": Path(f"{GEN_DIR}/{TOP_ABS}.cpp"),
                                "run_tcl_abs": Path(f"{TOP_ABS}.tcl")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir, comp_class=RfShotTxAbs, top=TOP_ABS, elab=_ELAB_ABS)
        root = config.root_dir
        return {"rf_shot_tx_abs_cpp": root / GEN_DIR / f"{TOP_ABS}.cpp",
                "run_tcl_abs": root / f"{TOP_ABS}.tcl"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of the kernel — and the source stamp the system flow checks.

    The stamp (:func:`~waveflow.build.rtl_digest.write_stamp`) records the sources this RTL was built
    from, so the system run's ``synth="check"`` accepts it, and refuses it once a source changes.
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
        write_stamp(config.root_dir, TOP)
        return {"report_dir": config.root_dir / f"{TOP}_proj" / "solution1"}


@dataclass(kw_only=True)
class CSynthAbsStep(BuildStep):
    """Vitis HLS C-synthesis of the absolute-index build — **both settings reach RTL**.

    A second ``csynth``, and the reason it is not optional: ``ABS`` is a template argument, so the
    two settings are two pieces of RTL. A gate that only ever elaborated one of them would be
    asserting the mode's behaviour against a simulator alone.
    """

    description = "Run Vitis HLS C-synthesis of the absolute-index top."
    consumes = ["rf_shot_tx_abs_cpp", "run_tcl_abs"]
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


def build_rf_shot_tx_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_shot_tx_source", path=HERE / "rf_shot_tx.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CSynthStep(name="csynth"))
    # THE SECOND BUILD.  Same source, same geometry, `absolute_index = 1` -- and its own top, so the
    # two csynth projects sit side by side (the system flow runs each as systems of its own).
    dag.add(CodegenDutAbsStep(name="codegen_dut_abs"))
    dag.add(CSynthAbsStep(name="csynth_abs"))
    # The committed-figure rungs, on demand.  They render from PYSIM rather than a VCD, so they need
    # no toolchain -- see rf_shot_tx_figures.py for why that is the point rather than a shortcut.
    dag.add(PlayoutFigureStep(name="playout_figure"))
    dag.add(SyncDocsFiguresStep(name="sync_docs_figures"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_shot_tx_dag,
                description="Build the rf_shot_tx design: pysim -> codegen -> csynth, for BOTH "
                            "settings of absolute_index.",
                default_through="csynth_abs",
                root_dir=HERE)
