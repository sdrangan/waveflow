"""rf_loopback_xsi.py — the full RF loopback at RTL: source → ADC → DUT → DAC → sink.

``plans/adc_model.md`` staging item 2, closed. The same five-node graph the pysim golden runs, now
generated into an XSI harness: the digital logic is real Verilog, the converters are the two
``xsi_rfdc.h`` models, and the RF environment is file-backed peers across two behavioral edges.

**Same graph, two backends.** ``RfLoopbackTB`` is not re-declared here — it is imported. What this
module adds is the *procedure*: generate, write the scenario, and check the run from the bundles it
dumped.

**Two testbenches, one DUT.** ``rf_pass_through`` already carries the DUT-alone gate from
``rf_dut_build.py``. They share the RTL, the ``_ports.h`` and the workspace; only the harness, the
main and the vectors differ, which is why both are generated with an explicit namespace.

WHAT THE TWO BACKENDS COUNT — the divergence, recorded rather than reconciled
----------------------------------------------------------------------------
pysim accounts loss on the **edge**, in whole **blocks**: ``RFSampIF.underrun`` (the metronome fired
with an empty buffer, so a zero block went out) and ``.overrun`` (the receiver refused one).

XSI accounts it in three places and two granularities:

- ``RfdcAdcMaster.dropped`` — **words** the fabric was not ready for;
- ``RfdcDacSlave.underrun`` — **cycles** where a beat was due and none came;
- ``BlockChannel.dropped`` / ``.starved`` — **blocks** refused or read-when-empty on the edge.

Same phenomena, different objects and different units. Neither side is wrong and neither is being
redefined to line up — that mapping is the input to ``plans/behavioral_edges.md`` S4, which is the
next step, not this one.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


HERE = Path(__file__).resolve().parent

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep  # noqa: E402
from waveflow.build.composite_gen import (  # noqa: E402
    render_tb_harness,
    render_tb_main,
    tb_top_spec,
)
from waveflow.simulation.simulation import Simulation  # noqa: E402

from examples.rf_loopback.rf_loopback import RfLoopbackSim, RfLoopbackTB  # noqa: E402
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord  # noqa: E402

#: The DUT's top — shared with the DUT-alone gate; this testbench brings its own harness only.
TOP = "rf_pass_through"
#: Namespace / file stem for THIS testbench, so it cannot collide with the DUT-alone one.
TB_NS = "rf_loopback_tb"

#: The gate scenario. Eight RF blocks of 256 samples; the pysim golden uses the same numbers, and
#: :class:`RfLoopbackSim` is the single writer of the vectors for both backends.
XSI_NBLK = 8
XSI_BLKSIZE = 256

#: A generous ``h.run(N)``. The ADC emits 0.25 words/cycle, so one 64-word block takes 256 cycles and
#: eight take ~2048; the loop bound only has to clear completion, and the sink's own capture is what
#: reports the real number.
#:
#: **16000, raised from 6000 on 2026-08-17** when ``RfdcDacSlave`` began withholding ``TREADY``.  The
#: DUT is unchanged — its own completion gate in ``test_xsi_bfm.py`` (1066 cycles, generic AXIS BFMs,
#: no converter models) did not move at all.  What changed is the *drain*: this pass-through reads a
#: whole block before it writes one, so with the DAC now pacing the write it occupies the boundary for
#: roughly four grid periods per block instead of running ahead of the converter.  At 6000 cycles only
#: 6 of the 8 blocks had come out.  The budget is a testbench parameter, not a result — the result is
#: what the sink collected, and that is gated separately.
XSI_N_CYCLES = 16000


def make_xsi_tb(n_blk: int = XSI_NBLK, blksize: int = XSI_BLKSIZE) -> RfLoopbackTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs."""
    return RfLoopbackTB(name="xsi_tb", sim=Simulation(), n_blk=n_blk, blksize=blksize)


def make_sim(n_blk: int = XSI_NBLK, blksize: int = XSI_BLKSIZE) -> RfLoopbackSim:
    """The pysim procedure over the same scenario — the single source of the vectors."""
    return RfLoopbackSim(n_src_blk=n_blk, name="xsi_tb", blksize=blksize)


def write_scenario(xsi_dir) -> RfLoopbackSim:
    """Materialize ``<xsi>/vectors/rf_in`` — the blocks BOTH backends play.

    Delegates to :meth:`RfLoopbackSim.write_scenario`, which is the one writer, so the RTL run and
    the pysim golden cannot start from different bytes.
    """
    sim = make_sim()
    sim.write_scenario(Path(xsi_dir))
    return sim


def generate_tb(out_dir: Path = HERE, n_cycles: int = XSI_N_CYCLES) -> None:
    """Generate this testbench's harness + main, and write its scenario bundle.

    Refreshes the XSI framework library into the workspace first: this harness names models from
    ``xsi_rfdc.h`` / ``xsi_rf_block.h``, and the gates compile the **committed copies** rather than
    ``waveflow/build/xsi/`` — so a workspace missing a header fails at ``g++``, not at generate time.
    """
    from waveflow.build.streamutils import XsiHarnessStep

    lib = BuildDag()
    lib.add(XsiHarnessStep(output_dir="xsi"))
    results = lib.run(BuildConfig(root_dir=out_dir, params={}), force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"XSI library copy failed: {failed}")

    spec = tb_top_spec(make_xsi_tb())
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{TB_NS}_harness.h").write_text(render_tb_harness(spec, ns=TB_NS), encoding="utf-8")
    (xsi / f"{TB_NS}_bfm_tb.cpp").write_text(
        render_tb_main(spec, n_cycles, ns=TB_NS, harness_header=f"{TB_NS}_harness.h",
                       wdb=f"{TB_NS}.wdb"), encoding="utf-8")
    write_scenario(xsi)
    print(f"generated TB xsi/{TB_NS}_harness.h + xsi/{TB_NS}_bfm_tb.cpp "
          f"({XSI_NBLK} blocks x {XSI_BLKSIZE} samples)")


# ---------------------------------------------------------------------------
# The TILE at RTL — the same loopback with TWO converter channels
# ---------------------------------------------------------------------------
#
# ``plans/adc_model.md``, *Stage A — the tile*.  The one thing this adds over the graph above is the
# claim that could not be made at one channel: **one BFM model per direction spans BOTH AXIS ports
# plus the RF edge**, because the edge carries every channel in one block and n_ch models cannot each
# own it.  In the generated harness that is one `RfdcAdcMaster` taking `{ports::s_in_0,
# ports::s_in_1}` -- an `AxisPortList` -- rather than two objects.
#
# It is a SEPARATE TOP, and deliberately: a two-channel pass-through has four AXIS ports, so it is a
# different RTL module.  Giving it its own name is what keeps `rf_pass_through`'s project, ports
# header and every cycle count recorded against them untouched by this stage.

#: The two-channel DUT's top — :class:`RfSampPassThrough2Ch`'s ``cpp_kernel_name``.
TOP_2CH = "rf_pass_through_2ch"
#: Namespace / file stem for the two-channel testbench.
TB_NS_2CH = "rf_loopback_2ch_tb"
#: Channels. Two is the smallest count at which "row ch is port ch" is a checkable claim.
XSI_NCH = 2
#: Its own vectors, because the two testbenches share one ``xsi/`` workspace and would otherwise
#: overwrite each other's scenario -- whichever generated last would hand its blocks to both.
IN_BUNDLE_2CH = "vectors/rf_in_2ch"
OUT_BUNDLE_2CH = "vectors/rf_out_2ch"


def make_xsi_tb_2ch(n_blk: int = XSI_NBLK, blksize: int = XSI_BLKSIZE) -> RfLoopbackTB:
    """The two-channel graph the XSI testbench is generated from."""
    return RfLoopbackTB(name="xsi_tb_2ch", sim=Simulation(), n_ch=XSI_NCH, n_blk=n_blk,
                        blksize=blksize, in_bundle=IN_BUNDLE_2CH, out_bundle=OUT_BUNDLE_2CH)


def make_sim_2ch(n_blk: int = XSI_NBLK, blksize: int = XSI_BLKSIZE) -> RfLoopbackSim:
    """The pysim procedure over the two-channel scenario — the single source of its vectors."""
    return RfLoopbackSim(n_src_blk=n_blk, name="xsi_tb_2ch", n_ch=XSI_NCH, blksize=blksize,
                         in_bundle=IN_BUNDLE_2CH, out_bundle=OUT_BUNDLE_2CH)


def generate_dut_2ch(out_dir: Path = HERE) -> None:
    """Generate the two-channel DUT top, its TCL and its ports header.

    Delegates to :func:`examples.rf_loopback.rf_dut_build.generate_dut`, which takes the class and
    the top name for exactly this reason — same body, same TCL shape, a different number of lanes.
    """
    from examples.rf_loopback.rf_dut_build import generate_dut
    from examples.rf_loopback.rf_loopback import RfSampPassThrough2Ch

    generate_dut(out_dir, dut_cls=RfSampPassThrough2Ch, top=TOP_2CH)


def generate_tb_2ch(out_dir: Path = HERE, n_cycles: int = XSI_N_CYCLES) -> None:
    """Generate the two-channel harness + main, and write its scenario bundle.

    The vectors are ``(2, blksize)`` blocks whose two rows differ, which is what makes a swapped
    channel a failure rather than a symmetry.
    """
    from waveflow.build.streamutils import XsiHarnessStep

    lib = BuildDag()
    lib.add(XsiHarnessStep(output_dir="xsi"))
    results = lib.run(BuildConfig(root_dir=out_dir, params={}), force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"XSI library copy failed: {failed}")

    spec = tb_top_spec(make_xsi_tb_2ch())
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{TB_NS_2CH}_harness.h").write_text(render_tb_harness(spec, ns=TB_NS_2CH),
                                                encoding="utf-8")
    (xsi / f"{TB_NS_2CH}_bfm_tb.cpp").write_text(
        render_tb_main(spec, n_cycles, ns=TB_NS_2CH, harness_header=f"{TB_NS_2CH}_harness.h",
                       wdb=f"{TB_NS_2CH}.wdb"), encoding="utf-8")
    sim = make_sim_2ch()
    sim.write_scenario(xsi)
    print(f"generated 2-channel TB xsi/{TB_NS_2CH}_harness.h + xsi/{TB_NS_2CH}_bfm_tb.cpp "
          f"({XSI_NBLK} blocks x {XSI_NCH} x {XSI_BLKSIZE} samples)")


# ---------------------------------------------------------------------------
# INTERLEAVED I/Q at RTL — Stage D's gate
# ---------------------------------------------------------------------------
#
# ``plans/adc_model.md``, *Stage D*.  The same five-node graph, with the converter in ``iq_mode``:
# complex blocks on the RF side, `(re, im)` adjacent in the bundle, and 2 complex samples in each
# 64-bit beat.
#
# **The DUT is `rf_pass_through`, unchanged and not re-synthesized.**  That is the result, not a
# convenience: complex-ness is a property of the WORD, and a word is a bag of bits to the fabric.
# The RTL between the two converter ports relays 64-bit beats and has no opinion about what they
# carry — which is exactly the claim *Channels, ports, and where I/Q lives* makes when it says an
# I/Q design stays on the same bus by halving `samp_per_word`.  If this needed a different top,
# something would have leaked the sample geometry into the fabric.

#: Namespace / file stem for the I/Q testbench.  It shares ``rf_pass_through``'s RTL and ports
#: header with the one-channel real testbench; only the harness, the main and the vectors differ.
TB_NS_IQ = "rf_loopback_iq_tb"

#: The 4x2's I/Q geometry: 2 complex samples a beat, 14-in-16, a 64-bit word.
XSI_IQ_WORD = Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True)

#: **Half** the real testbench's rate, and the reason is the DUT rather than I/Q: this
#: pass-through reads a whole block before it writes one, so it occupies the boundary for twice its
#: utilisation, and at 256 MSa/s with ``samp_per_word = 2`` that exceeds a block period.  Same
#: scaling the width sweep in ``tests/examples/test_rf_loopback.py`` already applies, and it keeps
#: the geometry under test packing rather than a timing accident.
XSI_IQ_SAMP_RATE = 128e6

#: Its own vectors: three testbenches share one ``xsi/`` workspace and must not overwrite each
#: other's scenario.
IN_BUNDLE_IQ = "vectors/rf_in_iq"
OUT_BUNDLE_IQ = "vectors/rf_out_iq"


def _iq_kwargs(n_blk: int, blksize: int) -> dict:
    """The knobs both the graph and the procedure need, in one place so they cannot drift."""
    return dict(n_blk=n_blk, blksize=blksize, word=XSI_IQ_WORD, samp_rate=XSI_IQ_SAMP_RATE,
                in_bundle=IN_BUNDLE_IQ, out_bundle=OUT_BUNDLE_IQ)


def make_xsi_tb_iq(n_blk: int = XSI_NBLK, blksize: int = XSI_BLKSIZE) -> RfLoopbackTB:
    """The I/Q graph the XSI testbench is generated from."""
    return RfLoopbackTB(name="xsi_tb_iq", sim=Simulation(), **_iq_kwargs(n_blk, blksize))


def make_sim_iq(n_blk: int = XSI_NBLK, blksize: int = XSI_BLKSIZE) -> RfLoopbackSim:
    """The pysim procedure over the I/Q scenario — the single source of its vectors."""
    kw = _iq_kwargs(n_blk, blksize)
    kw.pop("n_blk")
    return RfLoopbackSim(n_src_blk=n_blk, name="xsi_tb_iq", **kw)


def generate_tb_iq(out_dir: Path = HERE, n_cycles: int = XSI_N_CYCLES) -> None:
    """Generate the I/Q harness + main, and write its complex scenario bundle.

    No DUT generation: this testbench drives ``rf_pass_through``, the RTL the real one-channel
    loopback already built.
    """
    from waveflow.build.streamutils import XsiHarnessStep

    lib = BuildDag()
    lib.add(XsiHarnessStep(output_dir="xsi"))
    results = lib.run(BuildConfig(root_dir=out_dir, params={}), force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"XSI library copy failed: {failed}")

    spec = tb_top_spec(make_xsi_tb_iq())
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{TB_NS_IQ}_harness.h").write_text(render_tb_harness(spec, ns=TB_NS_IQ),
                                               encoding="utf-8")
    (xsi / f"{TB_NS_IQ}_bfm_tb.cpp").write_text(
        render_tb_main(spec, n_cycles, ns=TB_NS_IQ, harness_header=f"{TB_NS_IQ}_harness.h",
                       wdb=f"{TB_NS_IQ}.wdb"), encoding="utf-8")
    make_sim_iq().write_scenario(xsi)
    print(f"generated I/Q TB xsi/{TB_NS_IQ}_harness.h + xsi/{TB_NS_IQ}_bfm_tb.cpp "
          f"({XSI_NBLK} blocks x {XSI_BLKSIZE} complex samples)")


# The golden lives in tests/examples/test_rf_loopback_xsi.py, not here.
#
# It was drafted here first, asserting the pysim shape: a leading `blk_latency` zero block, then the
# sent blocks bit-identical.  The RTL run refuted the first half -- XSI's DAC emits a block only when
# it has accumulated `blk_samples`, with no metronome forcing an emission on an empty buffer, so
# there is no startup zero-fill at all.  Writing the checker from the plan rather than from the run
# would have encoded that mistake as a gate.  The surviving claims are asserted where they were
# measured.


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    description = "Run the RfLoopbackTB pysim golden over the XSI scenario."
    consumes = ["rf_loopback_source"]
    produces: ClassVar[dict] = {"loopback_pysim": Path("results/rf_loopback_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        sim = make_sim()
        sim.run()
        sim.check()
        out = config.root_dir / "results" / "rf_loopback_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "blocks": XSI_NBLK, "blksize": XSI_BLKSIZE,
            "adc": sim.tb.adc_if.counters(), "dac": sim.tb.dac_if.counters(),
            "blk_latency": int(sim.tb.dut.blk_latency),
        }, indent=2), encoding="utf-8")
        return {"loopback_pysim": out}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower RfLoopbackTB to its XSI harness + main + scenario bundle."
    consumes = ["rf_loopback_source"]
    produces: ClassVar[dict] = {"loopback_harness": Path(f"xsi/{TB_NS}_harness.h"),
                                "loopback_main": Path(f"xsi/{TB_NS}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb(config.root_dir)
        return {"loopback_harness": config.root_dir / "xsi" / f"{TB_NS}_harness.h",
                "loopback_main": config.root_dir / "xsi" / f"{TB_NS}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class PySim2ChStep(BuildStep):
    description = "Run the TWO-CHANNEL RfLoopbackTB pysim golden — Stage A's byte-identical gate."
    consumes = ["rf_loopback_source"]
    produces: ClassVar[dict] = {"loopback_pysim_2ch": Path("results/rf_loopback_2ch_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        sim = make_sim_2ch()
        sim.run()
        sim.check()
        out = config.root_dir / "results" / "rf_loopback_2ch_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "blocks": XSI_NBLK, "blksize": XSI_BLKSIZE, "n_ch": XSI_NCH,
            "adc": sim.tb.adc_if.counters(), "dac": sim.tb.dac_if.counters(),
            "blk_latency": int(sim.tb.dut.blk_latency),
        }, indent=2), encoding="utf-8")
        return {"loopback_pysim_2ch": out}


@dataclass(kw_only=True)
class CodegenDut2ChStep(BuildStep):
    description = "Lower RfSampPassThrough2Ch to its own ap_ctrl_none top (+ tcl, ports)."
    consumes = ["rf_loopback_source"]
    produces: ClassVar[dict] = {"dut_2ch_cpp": Path(f"gen/{TOP_2CH}.cpp"),
                                "dut_2ch_tcl": Path(f"{TOP_2CH}.tcl"),
                                "dut_2ch_ports": Path(f"xsi/{TOP_2CH}_ports.h")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut_2ch(config.root_dir)
        return {"dut_2ch_cpp": config.root_dir / "gen" / f"{TOP_2CH}.cpp",
                "dut_2ch_tcl": config.root_dir / f"{TOP_2CH}.tcl",
                "dut_2ch_ports": config.root_dir / "xsi" / f"{TOP_2CH}_ports.h"}


@dataclass(kw_only=True)
class CodegenTb2ChStep(BuildStep):
    description = "Lower the two-channel RfLoopbackTB to its XSI harness + main + vectors."
    consumes = ["rf_loopback_source", "dut_2ch_ports"]
    produces: ClassVar[dict] = {"loopback_2ch_harness": Path(f"xsi/{TB_NS_2CH}_harness.h"),
                                "loopback_2ch_main": Path(f"xsi/{TB_NS_2CH}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb_2ch(config.root_dir)
        return {"loopback_2ch_harness": config.root_dir / "xsi" / f"{TB_NS_2CH}_harness.h",
                "loopback_2ch_main": config.root_dir / "xsi" / f"{TB_NS_2CH}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class CSynth2ChStep(BuildStep):
    """Vitis HLS C-synthesis of the two-channel top — the RTL its ``-m xsi`` gate drives.

    A step of its own rather than a parameter of the one-channel one: the two are different RTL
    modules with different projects, and a shared step would mean synthesizing one on top of the
    other's output.
    """

    description = "Run Vitis HLS C-synthesis of the two-channel pass-through top."
    consumes = ["dut_2ch_cpp", "dut_2ch_tcl"]
    produces: ClassVar[dict] = {"report_dir_2ch": Path(f"{TOP_2CH}_proj/solution1")}
    params: ClassVar[dict] = {"live_output": False}

    def run(self, config: BuildConfig, live_output, **_) -> dict:
        from waveflow.build.composite_gen import render_rtl_f
        from waveflow.toolchain import toolchain

        result = toolchain.run_vitis_hls(Path(f"{TOP_2CH}.tcl"), work_dir=config.root_dir,
                                         capture_output=not live_output)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        xsi = config.root_dir / "xsi"
        xsi.mkdir(parents=True, exist_ok=True)
        (xsi / f"rtl_{TOP_2CH}.f").write_text(render_rtl_f(TOP_2CH, config.root_dir),
                                              encoding="utf-8")
        return {"report_dir_2ch": config.root_dir / f"{TOP_2CH}_proj" / "solution1"}


@dataclass(kw_only=True)
class PySimIQStep(BuildStep):
    description = "Run the INTERLEAVED-I/Q RfLoopbackTB pysim golden — Stage D's byte-identical gate."
    consumes = ["rf_loopback_source"]
    produces: ClassVar[dict] = {"loopback_pysim_iq": Path("results/rf_loopback_iq_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        sim = make_sim_iq()
        sim.run()
        sim.check()
        out = config.root_dir / "results" / "rf_loopback_iq_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "blocks": XSI_NBLK, "blksize": XSI_BLKSIZE, "word": XSI_IQ_WORD.describe(),
            "samp_rate": XSI_IQ_SAMP_RATE,
            "adc": sim.tb.adc_if.counters(), "dac": sim.tb.dac_if.counters(),
        }, indent=2), encoding="utf-8")
        return {"loopback_pysim_iq": out}


@dataclass(kw_only=True)
class CodegenTbIQStep(BuildStep):
    """Lower the I/Q testbench.  **No DUT step**: it drives ``rf_pass_through``, the RTL the real
    one-channel loopback already built, because a word is a bag of bits to the fabric."""

    description = "Lower the interleaved-I/Q RfLoopbackTB to its XSI harness + main + vectors."
    consumes = ["rf_loopback_source"]
    produces: ClassVar[dict] = {"loopback_iq_harness": Path(f"xsi/{TB_NS_IQ}_harness.h"),
                                "loopback_iq_main": Path(f"xsi/{TB_NS_IQ}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb_iq(config.root_dir)
        return {"loopback_iq_harness": config.root_dir / "xsi" / f"{TB_NS_IQ}_harness.h",
                "loopback_iq_main": config.root_dir / "xsi" / f"{TB_NS_IQ}_bfm_tb.cpp"}


def build_rf_loopback_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_loopback_source", path=HERE / "rf_loopback.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    # The tile: the same graph at two channels, through a top of its own.
    dag.add(PySim2ChStep(name="pysim_2ch"))
    dag.add(CodegenDut2ChStep(name="codegen_dut_2ch"))
    dag.add(CodegenTb2ChStep(name="codegen_tb_2ch"))
    dag.add(CSynth2ChStep(name="csynth_2ch"))
    # Interleaved I/Q: the same graph and the same RTL, with a complex word.
    dag.add(PySimIQStep(name="pysim_iq"))
    dag.add(CodegenTbIQStep(name="codegen_tb_iq"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_loopback_dag,
                description="Build the full RF loopback XSI testbench (the DUT's RTL comes from "
                            "rf_dut_build.py).",
                default_through="codegen_tb",
                root_dir=HERE)
