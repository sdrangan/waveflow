"""rf_samp_buf_tx_build.py — build the TX playout buffer: pysim -> codegen -> csynth.

``plans/adc_model.md`` staging item 3 (TX half).  The rungs, in the order a failure is cheapest to
diagnose:

    pysim       -> the playout cases in SimPy, against a predicted golden
    codegen_dut -> the ap_ctrl_none top (two tasks, two `mode=bram` ports), its tcl and the schema
                   headers
    csynth      -> Vitis HLS (needs the toolchain)

The RTL rung — the same four cases through real Verilog, bit-exact against the pysim golden — is the
**system** flow, ``rf_samp_buf_tx_system_build.py``; its gate is the ``-m xsi`` test
``tests/examples/test_rf_samp_buf_tx_xsi.py``.

As with ``bram_access``, what a simulator elaborates is the **wrapper** (``rf_samp_buf_tx_top``), not
the kernel: the memory is inside it, which is why the testbench sees only AXI-Stream.  The system flow
renders that wrapper into its system top; nothing here writes it.
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
    render_tcl,
    render_top,
)
from waveflow.build.elaborate import elaborate  # noqa: E402
from waveflow.build.streamutils import (  # noqa: E402
    MemMgrStep,
    RfSampBufStep,
    StreamUtilsStep,
)  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_samp_buf_tx.rf_samp_buf_tx import (  # noqa: E402
    HORIZON_MARGIN,
    SAMP_BW,
    TX_BUF_DEPTH,
    TX_SCHEMA_CLASSES,
    RfSampBufTx,
)

#: The generated kernel's name.
TOP = "rf_samp_buf_tx"
INCLUDE_DIR = "include"

#: Hand-written ``hls::task`` bodies.  They are **framework** now — shipped from ``waveflow/build/``
#: by :class:`~waveflow.build.streamutils.RfSampBufStep`, exactly as the ``mem_*`` bodies are — so
#: this example no longer carries a ``src/`` directory of its own.
FIXED_TASK_BODIES = ("rf_samp_buf_loader_task.h", "rf_samp_buf_player_task.h")

#: The gated geometry, stated rather than defaulted: ONE sample per word.  The recorded XSI cycle
#: count is for this configuration, so a change here is a change to what the gate measures.
#: `samp_per_word > 1` is the throughput lever and is swept in pysim; see
#: `tests/examples/test_rf_samp_buf_tx.py`.
XSI_SAMP_PER_WORD = 1




def elab_params(samp_per_word: int = XSI_SAMP_PER_WORD) -> dict:
    """The elaboration parameters for a given word geometry.

    ``bitwidth`` is derived rather than declared: a word is ``samp_per_word`` samples of
    :data:`SAMP_BW` bits, so the two cannot disagree.
    """
    return {"bitwidth": SAMP_BW * int(samp_per_word), "samp_per_word": int(samp_per_word),
            "depth": TX_BUF_DEPTH, "horizon_margin": HORIZON_MARGIN}


def generate_dut(out_dir: Path = HERE, samp_per_word: int = XSI_SAMP_PER_WORD) -> Path:
    """Generate the top, its tcl and the schema headers.

    *samp_per_word* is a parameter so a synthesis test can build the wider geometry into its own
    directory without disturbing the gated one — the recorded XSI cycle count belongs to
    ``samp_per_word == 1`` and nothing here may quietly move it.  The generated top keeps its name
    (it comes from ``RfSampBufTx.cpp_kernel_name``); what separates the two builds is *out_dir*.
    """
    elab = elab_params(samp_per_word)
    word_bw = int(elab["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})

    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(RfSampBufStep(output_dir=INCLUDE_DIR))
    # RxCmd / RxResp at the design's AXIS word width.  The capture body reads and writes them with
    # the generated read_stream<W> / write_stream<W> -- never a hand-rolled pack.  The fields stay
    # IDX_BW wide whatever W is; a wider word just packs the command into fewer beats.
    for cls in TX_SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=[word_bw], include_dir=INCLUDE_DIR))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    comp = elaborate(RfSampBufTx, dict(elab), name=TOP)
    spec = composite_top_spec(comp, width=word_bw)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS),
        encoding="utf-8")
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl (elaborated top: {spec.elab_top})")
    return cpp


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the four cases in SimPy against the predicted golden — the toolchain-free checkpoint."""

    description = "Run the RfSampBufTxTB pysim golden (the playout window + the counters)."
    consumes = ["rf_samp_buf_tx_source"]
    produces = {"pysim_results": Path("results/rf_samp_buf_tx_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        import numpy as np

        from examples.rf_samp_buf_tx.rf_samp_buf_tx import (PRIMED_AT, expected_responses,
                                                            played_samples, ramp_samples,
                                                            responses, run_pysim)

        tb = run_pysim()
        played, resp = played_samples(tb), responses(tb)
        want = ramp_samples(PRIMED_AT + 1088)
        hi = min(PRIMED_AT + 1024, played.size)
        assert np.array_equal(played[PRIMED_AT:hi], want[PRIMED_AT:hi]), (
            "the played window is not the loaded ramp")
        assert resp == expected_responses(1), f"pysim responses {resp} != predicted"
        assert not np.any(played[:PRIMED_AT]), "something played before the buffer was primed"
        out = config.root_dir / "results" / "rf_samp_buf_tx_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"played": int(tb.dut.n_played),
                                   "underrun": int(tb.dut.n_underrun),
                                   "responses": resp,
                                   "too_late": int(tb.dut.n_too_late),
                                   "waited": int(tb.dut.n_waited),
                                   "misaligned": int(tb.dut.n_misaligned)}, indent=2),
                       encoding="utf-8")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower RfSampBufTx to its ap_ctrl_none top + tcl + schema headers."
    consumes = ["rf_samp_buf_tx_source"]
    produces: ClassVar[dict] = {"rf_samp_buf_tx_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"rf_samp_buf_tx_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of the kernel."""

    description = "Run Vitis HLS C-synthesis of the generated top."
    consumes = ["rf_samp_buf_tx_cpp", "run_tcl"]
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
        # The source stamp: this RTL was just built from the sources on disk, which is what lets the
        # system flow's ``synth="check"`` accept it (``rtl_digest`` / ``trace_steps.rtl_staleness``).
        from waveflow.build.composite_gen import check_maxi_lowered
        from waveflow.build.rtl_digest import write_stamp

        check_maxi_lowered(config.root_dir, TOP)
        write_stamp(config.root_dir, TOP)
        return {"report_dir": config.root_dir / f"{TOP}_proj" / "solution1"}


def build_rf_samp_buf_tx_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_samp_buf_tx_source", path=HERE / "rf_samp_buf_tx.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CSynthStep(name="csynth"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_samp_buf_tx_dag,
                description="Build the RX capture buffer: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
