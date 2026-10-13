"""The pass-through's DUT gate through the **system** flow (``plans/system_model.md`` S3e): the kernel
alone as the DUT of an ``HwSystem``, between the generic driver's and sink's own models.

Two gates: the sink's recording is identical RTL against pysim, and the kernel flow's golden
(``test_xsi_bfm.py``'s ``rf_pass_through`` row) -- every word relayed bit-exact, the last at cycle
1066 -- holds on the RTL's recording.  The DAG runs with ``synth="check"``.

The number's history (it was the kernel flow's gate, ``test_xsi_bfm.py``, until
``plans/system_model.md`` S3f):

    rf_pass_through: the RF loopback's digital logic, cut ALONE between generic AXIS BFMs
    (plans/adc_model.md staging item 2).  8 bursts x 64 words, relayed bit-identically.

    1066, re-recorded 2026-08-14 when the DUT became two tasks + a one-block internal FIFO
    (ingress word relay -> block stage).  It was 1072, and **the six cycles are the whole
    throughput story, deliberately**: the change was not made to go faster.  The block stage still
    costs 136 cycles per firing (csynth: two 66-cycle pipelined loops, read-then-write over one
    block RAM), so per-block cost is ~133 either way and this TB -- whose driver pushes at full
    rate -- is bound by that.  What changed is WHERE the stall lands: the boundary port is now
    drained a word at a time by a task with latency 0, so a producer that cannot wait (an ADC)
    loses nothing.  That is invisible here, because a StreamDriver BFM waits; it is measured in
    tests/examples/test_rf_loopback_xsi.py, where ADC_DROPPED went 72 -> 0.

    The read/write serialization inside the block stage is intrinsic to block processing, not a
    defect: a stage that transforms a block cannot emit before it has received one.  Splitting it
    further would buy throughput here and change nothing about the converter.

    BACK TO 1066, 2026-08-18, when the RF fabric moved 300 -> 250 MHz (4.0 ns).  PREDICTED FROM
    THE REPORTS BEFORE THE RUN: at the looser target Vitis no longer needs the extra pipeline
    stage, VITIS_LOOP_258_1 latency returns 67 -> 66, the relay's interval 137 -> 136, and
    8 blocks x 1 cycle = 8.  1074 - 8 = 1066.  Measured 1066.  The two records below are the same
    schedule seen at two clock targets, which is why neither is "the" number.

    1074, recorded 2026-08-17 when the RF examples moved to the RFSoC 4x2 part
    (xczu48dr-ffvg1517-2-e) at 300 MHz, from xc7z020clg484-1 at 100 MHz.  +8 cycles, and the
    arithmetic closes exactly:

      * the relay's WRITE loop, VITIS_LOOP_258_1, deepened by one pipeline stage -- latency
        66 -> 67 -- to meet the 3.333 ns target.  Its II is unchanged at 65, and the READ loop
        (VITIS_LOOP_184_1) did not need the extra stage.  Depth-for-frequency, the ordinary trade.
      * so rf_samp_relay_task's interval went 136 -> 137: one more cycle per block firing.
      * the scenario is XSI_NBURST = 8 blocks, so 8 x 1 = 8.  1066 + 8 = 1074.

    Measured by A/B in one session: same generated C++ built for both targets, module latencies
    read from each project's own csynth.xml.  Fmax went 146.74 -> 547.05 MHz, which is the point of
    the move -- the RF model's arithmetic assumes a 300 MHz fabric and 146 MHz could not host it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.rf_loopback.rf_dut_system_build import HERE, TOP, build_dag, check_xsi, system
from waveflow.build.build import BuildConfig
from waveflow.toolchain.toolchain import find_vivado_path

WORK = Path(__file__).resolve().parents[2] / "tests" / "build" / "_xsi_work"


def test_rf_dut_system_pysim_relays_every_word():
    system().run()


@pytest.fixture(scope="module")
def rf_dut_system_run():
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (xsim)")
    res = build_dag(work_dir=WORK).run(BuildConfig(root_dir=HERE, params={"synth": "check"}),
                                       through="compare")
    bad = {n: r.message for n, r in res.items() if not r.success}
    if bad:
        pytest.fail(f"the rf_pass_through system DAG failed: {bad}")
    return WORK / TOP


@pytest.mark.xsi
def test_rf_dut_system_recording_matches_pysim(rf_dut_system_run):
    work = rf_dut_system_run
    assert json.loads((work / "compare.json").read_text())["trace_mismatches"] == []
    assert sorted(p.name for p in (work / "traces").iterdir()) == ["s_out"]


@pytest.mark.xsi
def test_rf_dut_system_rtl_matches_the_kernel_gate(rf_dut_system_run):
    """THE kernel gate, through the system flow: bit-exact relay, the last word at cycle 1066."""
    check_xsi(rf_dut_system_run)
