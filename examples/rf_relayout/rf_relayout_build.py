"""rf_relayout_build.py — build the logic-side re-layout at 14-in-16: pysim -> codegen -> csynth.

``plans/rf_shot_buf.md`` § *The caveat, and it is a Stage A gate*.  The rungs:

    pysim       -> the loopback is byte-identical in SimPy, and the DENSE words match the golden
    codegen_dut -> the ap_ctrl_none top, its tcl, its port map, and the two generated array-utils
                   headers the task bodies serialize through
    codegen_tb  -> the XSI harness + main + scenario bundle
    csynth      -> Vitis HLS.  **This is the rung the plan asks for**: the achieved PipelineII of the
                   two re-layout bodies, at a geometry where the conversion is not the identity.

**No wrapper and no memory here** — the re-layout holds no state, so the thing xsim elaborates is the
kernel itself.  That is the difference from the shot family, whose designs all wrap a memory, and it
is why this example's ``.f`` is named for the top rather than for a wrapper.

**The two array-utils headers are generated into the same directory as the task bodies**, because the
bodies include them by plain name.  Their *class names* are fixed
(:func:`~waveflow.hw.rf_relayout.slot_elem_type` / ``dense_elem_type``) and their *widths* come from
the word type, which is what lets a framework body serialize through them without knowing either.
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
from waveflow.build.streamutils import (  # noqa: E402
    MemMgrStep,
    RfShotBufStep,
    StreamUtilsStep,
    XsiHarnessStep,
)
from waveflow.hw.arrayutils import ArrayUtilsStep  # noqa: E402
from waveflow.hw.rf_relayout import (  # noqa: E402
    RfRelayout,
    dense_elem_type,
    slot_elem_type,
    slots_per_word,
)
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_relayout.rf_relayout import (  # noqa: E402
    NWORD,
    WORD,
    RfRelayoutTB,
    write_scenario,
)

TOP = "rf_relayout"
INCLUDE_DIR = "include"

#: Hand-written ``hls::task`` bodies — framework, shipped by
#: :class:`~waveflow.build.streamutils.RfShotBufStep`.
FIXED_TASK_BODIES = ("rf_relayout_to_dense_task.h", "rf_relayout_to_slots_task.h")

#: The gated geometry, derived from the word rather than restated: 4 slots, 64-bit word, shift 2.
_ELAB = {"bitwidth": int(WORD.bitwidth), "n_slot": slots_per_word(WORD),
         "shift": int(WORD.justify_shift())}


def generate_dut(out_dir: Path = HERE) -> Path:
    """Generate the top, its tcl, the XSI port map, and the two array-utils headers."""
    word_bw = int(_ELAB["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(RfShotBufStep(output_dir=INCLUDE_DIR))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    # The serializers the two bodies call.  The SLOT element is the converter's container width and
    # the DENSE element the effective one; at 14-in-16 they differ, which is the whole reason this
    # example exists.  Both at the design's word width, because the re-layout stays inside one width.
    inner.add(ArrayUtilsStep(slot_elem_type(WORD, INCLUDE_DIR), [word_bw]))
    inner.add(ArrayUtilsStep(dense_elem_type(WORD, INCLUDE_DIR), [word_bw]))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    comp = elaborate(RfRelayout, dict(_ELAB), name=TOP)
    if comp.is_identity:
        raise RuntimeError(
            f"{TOP} elaborated with shift=0, which makes the re-layout the IDENTITY — the exact "
            f"condition plans/rf_shot_buf.md says must not be what gets measured. Use a word type "
            f"whose bits_per_samp differs from its bits_per_samp_pack (Rfsoc4x2SampWord).")
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
          f"({WORD.describe()}, shift {_ELAB['shift']})")
    return cpp


def make_xsi_tb() -> RfRelayoutTB:
    return RfRelayoutTB(name="xsi_tb", sim=Simulation())


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


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """The loopback **and** the intermediate, because the loopback alone cannot see the format."""

    description = "Run the RfRelayoutTB pysim golden (loopback identity + the dense golden)."
    consumes = ["rf_relayout_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/rf_relayout_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        import numpy as np

        from examples.rf_relayout.rf_relayout import (captured_words, check_outputs, dense_golden,
                                                      run_pysim, stim_words)

        tb = run_pysim()
        got = captured_words(tb)
        check_outputs(got, where="pysim: ")
        assert not tb.dut.is_identity, "the gated configuration degraded to the identity"
        dense = dense_golden()
        assert not np.array_equal(dense, stim_words()), (
            "the dense words equal the converter words — the re-layout did nothing, so this run "
            "measured a pair of wires")
        out = config.root_dir / "results" / "rf_relayout_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "word": WORD.describe(), "nword": int(NWORD), "shift": int(_ELAB["shift"]),
            "n_slot": int(_ELAB["n_slot"]), "words_out": int(got.size),
            "dense_first": f"0x{int(dense[0]):016x}"}, indent=2), encoding="utf-8")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfRelayout to its ap_ctrl_none top + the two array-utils headers."
    consumes = ["rf_relayout_source"]
    produces: ClassVar[dict] = {"rf_relayout_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl"),
                                "dut_ports": Path(f"xsi/{TOP}_ports.h")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"rf_relayout_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl",
                "dut_ports": root / "xsi" / f"{TOP}_ports.h"}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfRelayoutTB to the XSI harness + main + scenario bundle."
    consumes = ["rf_relayout_source", "dut_ports"]
    produces: ClassVar[dict] = {"tb_harness": Path(f"xsi/{TOP}_tb_harness.h"),
                                "tb_main": Path(f"xsi/{TOP}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb(config.root_dir)
        return {"tb_harness": config.root_dir / "xsi" / f"{TOP}_tb_harness.h",
                "tb_main": config.root_dir / "xsi" / f"{TOP}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis — **the measurement this example exists for**.

    The achieved ``PipelineII`` of ``rf_relayout_to_dense_task`` and ``rf_relayout_to_slots_task``,
    at 14-in-16.  Achieved, not target: Vitis reports both and they differ whenever it missed, and a
    number taken from the target would be the prediction this rung replaces.
    """

    description = "Run Vitis HLS C-synthesis of the generated top."
    consumes = ["rf_relayout_cpp", "run_tcl"]
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


def build_rf_relayout_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_relayout_source", path=HERE / "rf_relayout.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_relayout_dag,
                description="Build the rf_relayout design: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
