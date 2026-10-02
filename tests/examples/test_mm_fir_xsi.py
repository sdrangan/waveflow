"""mm_fir rung 3 (plans/mm_slave_adaptor.md): the synthesized kernel behind the adaptor, under XSI.

The system itself -- AMD's crossbar, the hand-written adaptor leaves and the csynth'd ``mm_fir``
kernel under one generated top, and the C++ host program -- is built by
``examples/mm_fir/mm_fir_xsi.py``; this file only runs it and checks it.  The host runs the same
protocol as the pysim ``FirHost``: commit a config, poll the status until the config is RECEIVED, wait
for room before each packet, drain the outputs between packets, and switch taps mid-stream.  The
output must equal the numpy golden bit for bit, and the status must show both configs received and
none late.

Run: ``pytest tests/examples/test_mm_fir_xsi.py -m xsi`` (needs Vivado, and
``python -m examples.mm_fir.mm_fir_build`` for the kernel's csynth).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.mm_fir.mm_fir import MmFirSystem, fir_golden
from examples.mm_fir.mm_fir_xsi import (
    NSAMP,
    PKT,
    PLAN,
    ROOT,
    RTL,
    output_words,
    parse_kv,
    run_xsi,
    scenario_x,
)
from waveflow.build.trace_steps import rtl_staleness
from waveflow.toolchain.toolchain import find_vivado_path

WORK = Path(__file__).resolve().parents[2] / "tests" / "build" / "_xsi_work"


@pytest.fixture(scope="module", params=["per_view", "one_front"])
def fir_run(request) -> tuple[str, str]:
    topology = request.param
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (create_ip + xsim)")
    if not RTL.is_dir():
        pytest.skip(f"XSI gate prerequisite missing: no csynth RTL at {RTL} -- run "
                    f"python -m examples.mm_fir.mm_fir_build")
    stale = rtl_staleness(ROOT, "mm_fir")
    if stale is not None:
        pytest.skip(f"XSI gate prerequisite missing: {stale}")
    return topology, run_xsi(topology, WORK)


@pytest.mark.xsi
def test_mm_fir_rtl_bit_exact(fir_run):
    _topology, fir_run = fir_run
    done = parse_kv(fir_run, "DONE")
    assert done["done"] == 1, fir_run[-3000:]
    st = parse_kv(fir_run, "STATUS")
    assert st == {"nsamp": NSAMP, "ncfg": 2, "late": 0}, st
    y = output_words(fir_run)
    assert np.array_equal(y, fir_golden(scenario_x(), PLAN)), "RTL output differs from the numpy golden"


@pytest.mark.xsi
def test_mm_fir_rtl_cycles(fir_run):
    topology, fir_run = fir_run
    done = parse_kv(fir_run, "DONE")
    sysm = MmFirSystem(x=list(scenario_x()), plan=PLAN, pkt=PKT, one_front=topology == "one_front")
    sysm.run()
    pysim_cycles = sysm.sim.env.now / sysm.clk.period
    print({"topology": topology, "rtl": done, "pysim_cycles": pysim_cycles})
    assert done["cycles"] == EXPECTED_CYCLES[topology], (
        f"{topology}: cycle count moved: {done} (pysim {pysim_cycles})")


#: Recorded 2026-10-02: host program start to the final status read, 200 samples, one tap switch.
#:
#: The kernel is pipelined at II=1 (csynth: latency 10, interval 1).  Its first version was not -- it
#: read a whole 5-word config and wrote a whole 2-word status inside one firing -- and ran at ~1 sample
#: per 10 cycles: 2096 cycles, 221 bus ops, 55 polls, because every drain found only a few outputs.
#: Moving at most one word per stream per firing fixed it: 857 cycles, 68 ops, 2 polls.
#:
#: pysim (crossbar latency_init = 4) predicts 709, 17% optimistic.  Per packet the RTL takes ~57 cycles
#: and pysim ~51, and the difference is the C++ host's own pacing: AxiMmMaster starts each op two
#: cycles after the previous one ends, and the protocol issues four ops per packet.  That is the
#: testbench, not the system; the adaptor alone tracks RTL within 2 cycles (tests/hw/test_mm_queue.py).
#:
#: one_front (Stage 4, recorded the same day): 823 -- 34 fewer over the same 68 ops.  Where the half
#: cycle per op comes from (a 1x2 instead of a 1x3 crossbar, or one front instead of three) has NOT
#: been isolated; both runs are bit-exact and pysim predicts 709 for each.
EXPECTED_CYCLES = {"per_view": 857, "one_front": 823}
