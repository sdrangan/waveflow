"""rf_repeat_play_build.py — build the streaming transmitter: pysim -> gen-include -> csynth.

Two rungs, in the order a failure is cheapest to diagnose::

    pysim        the three Stage 1 assertions in SimPy (tests/examples/test_rf_repeat_play.py)
    gen_include  the four schema headers + the two hand-written hls::task bodies
    csynth       Vitis HLS over BOTH bodies, under hls::task, at the RF arc's part and clock

**It generates the real composite top.**  ``RfTxStream``'s one internal edge is an
:class:`~waveflow.hw.reverse_stream.AckedStreamIF`, which lowers as **two ordinary stream edges**
rather than a new edge kind — see :meth:`~waveflow.hw.interface.Interface.physical_interfaces`. The
top, its tcl and its ports header are derived from the elaborated graph, so nothing here restates the
design.

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
    RfTxStreamStep,
    StreamUtilsStep,
    XsiHarnessStep,
)
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.hw.rf_tx_stream import RfRepeatPlay  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.rf_repeat_play.rf_repeat_play import (  # noqa: E402
    BLK_SAMP,
    RfCircPlayTB,
    MAX_IN_FLIGHT,
    NSAMP,
    PERIOD,
    SAMP_BW,
    TX_STREAM_SCHEMA_CLASSES,
    write_wave_scenario,
)
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.hw.rf_tx_stream import LEAD, TAG_BW  # noqa: E402

#: The generated kernel's name — read off ``RfTxStream.cpp_kernel_name``, never restated.
TOP = "rf_repeat_play"
INCLUDE_DIR = "include"
GEN = GEN_DIR

#: The gated geometry, stated rather than defaulted: ONE sample per word, 16-bit words on the
#: command / sample / response ports.  ``samp_per_word > 1`` is the throughput lever and is exercised
#: in pysim only.
SYNTH_SAMP_PER_WORD = 1
SYNTH_W = SAMP_BW * SYNTH_SAMP_PER_WORD

#: Word widths the generated schema headers must support.  ``SYNTH_W`` for the command / response
#: ports; ``TAG_BW`` because the two internal channels carry a ``TaggedSamp`` and a ``TxStatus``
#: **packed into one word each** by the generated ``pack_to_uint`` — which is exactly what the pysim
#: twin puts on those wires, so the two backends carry identical bits.
WORD_BW_SUPPORTED = [SYNTH_W, TAG_BW]

#: Solution-level tcl this design needs.  **The reset trap is closed here and only here**: an
#: ``hls::task`` that writes before it reads advances its state during reset, and both task bodies
#: carry ``#pragma HLS reset`` on every state register that Vitis 2025.1 ignored -- csynth reported
#: "Register '<x>' is power-on initialization" for all of them.  This setting takes them to zero, and
#: costs nothing measurable (the payload loop still schedules at II=1, the player still at II=1).
SOLUTION_CONFIG = ("config_rtl -reset state",)


def elab_params(samp_per_word: int = SYNTH_SAMP_PER_WORD) -> dict:
    """The elaboration parameters.  ``bitwidth`` is derived so the two cannot disagree."""
    return {"bitwidth": SAMP_BW * int(samp_per_word), "samp_per_word": int(samp_per_word),
            "nsamp": NSAMP, "period": PERIOD, "blk_samp": BLK_SAMP,
            "max_in_flight": MAX_IN_FLIGHT, "lead": LEAD}


def generate(out_dir: Path = HERE, samp_per_word: int = SYNTH_SAMP_PER_WORD) -> None:
    """Emit the schema headers, both task bodies, and the generated composite top + tcl."""
    elab = elab_params(samp_per_word)
    word_bw = int(elab["bitwidth"])
    config = BuildConfig(root_dir=out_dir, params={})
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    # `render_top` includes memmgr.hpp unconditionally, so a generated top needs it beside the
    # sources even when the design has no m_axi port at all.
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    inner.add(RfTxStreamStep(output_dir=INCLUDE_DIR))
    for cls in TX_STREAM_SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=sorted({word_bw, TAG_BW}),
                                 include_dir=INCLUDE_DIR))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    comp = elaborate(RfRepeatPlay, dict(elab), name=TOP)
    spec = composite_top_spec(comp, width=word_bw)
    gen = out_dir / GEN
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS,
                   solution_config=SOLUTION_CONFIG),
        encoding="utf-8")
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    print(f"generated {GEN}/{spec.top_name}.cpp + {spec.top_name}.tcl + xsi/{spec.top_name}_ports.h "
          f"({len(spec.tasks)} tasks, {len(spec.channels)} internal channels, "
          f"{len(spec.ports)} ports)")


def make_xsi_tb() -> RfCircPlayTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs.

    **One graph, two backends.**  The testbench pushes a waveform once and reads the converter; every
    command in the run is generated by the scheduler inside the DUT, so there is no stimulus here
    that pysim and RTL could disagree about.
    """
    return RfCircPlayTB(name="xsi_tb", sim=Simulation())


def generate_tb(out_dir: Path = HERE) -> None:
    """Generate the XSI harness + main from the TB graph, and write the one-shot waveform bundle."""
    tb = make_xsi_tb()
    spec = tb_top_spec(tb)
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{TOP}_bfm_tb.cpp").write_text(render_tb_main(spec, int(tb.n_cycles)), encoding="utf-8")
    write_wave_scenario(xsi, nsamp=int(tb.nsamp))
    print(f"generated TB xsi/{TOP}_tb_harness.h + xsi/{TOP}_bfm_tb.cpp "
          f"(one burst of {tb.nsamp} words, {tb.n_cycles} cycles)")


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the three Stage 1 assertions in SimPy — the toolchain-free checkpoint."""

    description = "Run the rf_repeat_play pysim gate (schedule, TX_TOO_LATE, underrun recovery)."
    consumes = ["rf_repeat_play_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/rf_repeat_play_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        from examples.rf_repeat_play.rf_repeat_play import responses, run_pysim

        tb = run_pysim()
        c = dict(tb.dut.counters)
        c["underrun"] = int(tb.dac_if.underrun)
        c["last_underrun_idx"] = int(tb.dac_if.last_underrun_idx)
        c["base"] = int(tb.host.base)
        c["responses"] = responses(tb)
        out = config.root_dir / "results" / "rf_repeat_play_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(c, indent=2), encoding="utf-8")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class GenIncludeStep(BuildStep):
    description = "Emit the schema headers, both task bodies, the generated top and its tcl."
    consumes = ["rf_repeat_play_source"]
    produces: ClassVar[dict] = {"top_cpp": Path(f"{GEN}/{TOP}.cpp"), "run_tcl": Path(f"{TOP}.tcl"),
                                "dut_ports": Path(f"xsi/{TOP}_ports.h")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate(config.root_dir)
        return {"top_cpp": config.root_dir / GEN / f"{TOP}.cpp",
                "run_tcl": config.root_dir / f"{TOP}.tcl",
                "dut_ports": config.root_dir / "xsi" / f"{TOP}_ports.h"}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfCircPlayTB to the XSI harness + main + the one-shot waveform bundle."
    consumes = ["rf_repeat_play_source", "dut_ports"]
    produces: ClassVar[dict] = {"tb_harness": Path(f"xsi/{TOP}_tb_harness.h"),
                                "tb_main": Path(f"xsi/{TOP}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb(config.root_dir)
        return {"tb_harness": config.root_dir / "xsi" / f"{TOP}_tb_harness.h",
                "tb_main": config.root_dir / "xsi" / f"{TOP}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of both task bodies."""

    description = "Run Vitis HLS C-synthesis over the two rf_tx_stream task bodies."
    consumes = ["top_cpp", "run_tcl"]
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
        # Re-emit the file list FROM THE RTL ON DISK.  A committed or stale `.f` names files that may
        # no longer exist or may no longer be this design, and xsim would elaborate whatever it finds
        # -- an XSI run that PASSES against the wrong RTL.
        xsi = config.root_dir / "xsi"
        xsi.mkdir(parents=True, exist_ok=True)
        (xsi / f"rtl_{TOP}.f").write_text(render_rtl_f(TOP, config.root_dir), encoding="utf-8")
        return {"report_dir": config.root_dir / f"{TOP}_proj" / "solution1"}


def build_rf_repeat_play_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_repeat_play_source", path=HERE / "rf_repeat_play.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(GenIncludeStep(name="gen_include"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_repeat_play_dag,
                description="Build the streaming transmitter: pysim -> gen-include -> csynth.",
                default_through="csynth",
                root_dir=HERE)
