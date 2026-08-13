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

#: The DUT's top — shared with the DUT-alone gate; this testbench brings its own harness only.
TOP = "rf_pass_through"
#: Namespace / file stem for THIS testbench, so it cannot collide with the DUT-alone one.
TB_NS = "rf_loopback_tb"

#: The gate scenario. Eight RF blocks of 256 samples; the pysim golden uses the same numbers, and
#: :class:`RfLoopbackSim` is the single writer of the vectors for both backends.
XSI_NBLK = 8
XSI_BLKSIZE = 256

#: A generous ``h.run(N)``. The ADC emits ~0.213 words/cycle, so one 64-word block takes ~300 cycles
#: and eight take ~2400; the loop bound only has to clear completion, and the sink's own capture is
#: what reports the real number.
XSI_N_CYCLES = 6000


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


def build_rf_loopback_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_loopback_source", path=HERE / "rf_loopback.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_loopback_dag,
                description="Build the full RF loopback XSI testbench (the DUT's RTL comes from "
                            "rf_dut_build.py).",
                default_through="codegen_tb",
                root_dir=HERE)
