"""rf_samp_buf_rx_build.py — build the RX capture buffer: pysim -> codegen -> csynth.

``plans/adc_model.md`` staging item 3 (RX).  The rungs, in the order a failure is cheapest to
diagnose:

    pysim       -> the four command cases in SimPy, against a predicted golden
    codegen_dut -> the ap_ctrl_none top (two tasks, two `mode=bram` ports), its tcl, its port map,
                   the memory placed beside it, and the WRAPPER that joins them
    codegen_tb  -> the XSI harness + main + scenario bundles
    csynth      -> Vitis HLS (needs the toolchain); re-emits rtl_<wrapper>.f from the RTL on disk

The RTL rung — the same four cases through real Verilog, bit-exact against the pysim golden — is the
``-m xsi`` gate in ``tests/examples/test_rf_samp_buf_rx_xsi.py``.

As with ``bram_toy``, what a simulator elaborates is the **wrapper** (``rf_samp_buf_rx_top``), not
the kernel: the memory is inside it, which is why the testbench sees only AXI-Stream.
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
    RfSampBufStep,
    StreamUtilsStep,
    XsiHarnessStep,
)  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_samp_buf_rx.rf_samp_buf_rx import (  # noqa: E402
    BUF_DEPTH,
    HORIZON_MARGIN,
    SCHEMA_CLASSES,
    WORD_BW,
    XSI_BLKSIZE,
    XSI_NBLK,
    RfSampBufRx,
    RfSampBufRxTB,
    write_scenario,
)

#: The generated kernel's name, and the wrapper's.  The wrapper is what a simulator elaborates.
TOP = "rf_samp_buf_rx"
WRAPPER = f"{TOP}_top"
INCLUDE_DIR = "include"

#: Hand-written ``hls::task`` bodies.  They are **framework** now — shipped from ``waveflow/build/``
#: by :class:`~waveflow.build.streamutils.RfSampBufStep`, exactly as the ``mem_*`` bodies are — so
#: this example no longer carries a ``src/`` directory of its own.
FIXED_TASK_BODIES = ("rf_samp_buf_ingress_task.h", "rf_samp_buf_capture_task.h")

#: RTL that must land in ``xsi/`` beside the ``.f`` naming it, in elaboration reading order.
RTL_FILES = ("bram_t2p.v", f"{WRAPPER}.v")

#: The gated geometry, stated rather than defaulted: ONE sample per word.  The recorded XSI cycle
#: count is for this configuration, so a change here is a change to what the gate measures.
#: `samp_per_word > 1` is the throughput lever and is swept in pysim; see
#: `tests/examples/test_rf_samp_buf_rx.py`.
XSI_SAMP_PER_WORD = 1




def elab_params(samp_per_word: int = XSI_SAMP_PER_WORD) -> dict:
    """The elaboration parameters for a given word geometry.

    ``bitwidth`` is derived rather than declared: a word is ``samp_per_word`` samples of
    :data:`WORD_BW` bits, so the two cannot disagree.
    """
    return {"bitwidth": WORD_BW * int(samp_per_word), "samp_per_word": int(samp_per_word),
            "depth": BUF_DEPTH, "horizon_margin": HORIZON_MARGIN}


def generate_dut(out_dir: Path = HERE, samp_per_word: int = XSI_SAMP_PER_WORD) -> Path:
    """Generate the top, its tcl, the XSI port map, the schema headers, the memory and the wrapper.

    *samp_per_word* is a parameter so a synthesis test can build the wider geometry into its own
    directory without disturbing the gated one — the recorded XSI cycle count belongs to
    ``samp_per_word == 1`` and nothing here may quietly move it.  The generated top keeps its name
    (it comes from ``RfSampBufRx.cpp_kernel_name``); what separates the two builds is *out_dir*.
    """
    elab = elab_params(samp_per_word)
    word_bw = int(elab["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(RfSampBufStep(output_dir=INCLUDE_DIR))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    # RxCmd / RxResp at the design's AXIS word width.  The capture body reads and writes them with
    # the generated read_stream<W> / write_stream<W> -- never a hand-rolled pack.  The fields stay
    # IDX_BW wide whatever W is; a wider word just packs the command into fewer beats.
    for cls in SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=[word_bw], include_dir=INCLUDE_DIR))
    inner.add(GenRtlStep(name="place_memory", comp_class=_memory_class(samp_per_word),
                         output_dir="xsi"))
    inner.add(GenWrapperStep(name="wrapper", comp_class=RfSampBufRx, elab_params=dict(elab),
                             width=word_bw, output_dir="xsi"))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    comp = elaborate(RfSampBufRx, dict(elab), name=TOP)
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


def _memory_class(samp_per_word: int = XSI_SAMP_PER_WORD):
    """The memory class the design instantiates — read off the elaborated graph, not restated."""
    comp = elaborate(RfSampBufRx, elab_params(samp_per_word))
    mems = {type(m) for m in comp.rtl_mods.values()}
    if len(mems) != 1:
        raise RuntimeError(f"expected exactly one RTL module class in {TOP}, got {mems}")
    return mems.pop()


def make_xsi_tb() -> RfSampBufRxTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs."""
    return RfSampBufRxTB(name="xsi_tb", sim=Simulation())


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
          f"({XSI_NBLK} blocks x {XSI_BLKSIZE} samples + 4 commands)")


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the four cases in SimPy against the predicted golden — the toolchain-free checkpoint."""

    description = "Run the RfSampBufRxTB pysim golden (four command cases + the counters)."
    consumes = ["rf_samp_buf_rx_source"]
    produces = {"pysim_results": Path("results/rf_samp_buf_rx_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        import numpy as np

        from examples.rf_samp_buf_rx.rf_samp_buf_rx import (captured_words, expected_capture,
                                                            responses, run_pysim)

        tb = run_pysim()
        got, resp = captured_words(tb), responses(tb)
        want_words, want_resp = expected_capture()
        assert np.array_equal(got, want_words), "pysim capture is not the predicted window"
        assert resp == want_resp, f"pysim responses {resp} != predicted {want_resp}"
        assert tb.adc_axis.dropped == 0, "the ingress stalled the converter"
        out = config.root_dir / "results" / "rf_samp_buf_rx_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"captured": int(got.size), "responses": resp,
                                   "too_old": int(tb.dut.n_too_old),
                                   "waited": int(tb.dut.n_waited),
                                   "adc_dropped": int(tb.adc_axis.dropped)}, indent=2),
                       encoding="utf-8")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfSampBufRx to its ap_ctrl_none top + the memory + the wrapper."
    consumes = ["rf_samp_buf_rx_source"]
    produces: ClassVar[dict] = {"rf_samp_buf_rx_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl"),
                                "dut_ports": Path(f"xsi/{TOP}_ports.h"),
                                "wrapper_v": Path(f"xsi/{WRAPPER}.v"),
                                "memory_v": Path("xsi/bram_t2p.v")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"rf_samp_buf_rx_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl",
                "dut_ports": root / "xsi" / f"{TOP}_ports.h",
                "wrapper_v": root / "xsi" / f"{WRAPPER}.v",
                "memory_v": root / "xsi" / "bram_t2p.v"}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfSampBufRxTB to the XSI harness + main + scenario bundles."
    consumes = ["rf_samp_buf_rx_source", "dut_ports"]
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
    consumes = ["rf_samp_buf_rx_cpp", "run_tcl"]
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


def build_rf_samp_buf_rx_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_samp_buf_rx_source", path=HERE / "rf_samp_buf_rx.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_samp_buf_rx_dag,
                description="Build the RX capture buffer: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
