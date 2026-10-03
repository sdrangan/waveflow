"""mm_fir rung 3 (plans/mm_slave_adaptor.md): the synthesized kernel behind the adaptor, under XSI.

The system itself -- AMD's crossbar, the hand-written adaptor leaves and the csynth'd ``mm_fir``
kernel under one generated top, and the C++ host program -- is built by
``examples/mm_fir/mm_fir_xsi.py``; this file only runs it and checks it.  The host is the pysim
``FirHost`` on the C++ endpoints of ``xsi_mm_host.h`` (plans/mm_adaptor_host_endpoints.md Stage 2): a
writer that commits each config and waits until the status shows it RECEIVED, then sends the sample
packets; and a reader that takes one output packet per input packet.  The output must equal the numpy
golden bit for bit, and the status must show both configs received and none late.

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


#: Recorded 2026-10-03, with the two-process host on the C++ endpoints (xsi_mm_host.h): host program
#: start to the final status read, 200 samples, one tap switch.  per_view 811 / one_front 776, 73 bus
#: ops, 6 polls.  Measured on RTL csynth'd from this checkout; the previous host on the SAME RTL still
#: measured 857 / 823 (68 ops, 2 polls), so the move is the host program and nothing else.
#:
#: pysim says 423 / 742.  The per_view gap is the BUS MASTER MODEL, not the adaptor: AxiMmMaster keeps
#: one transaction outstanding, so the writer's and the reader's operations take turns even when they
#: go to different slaves, while a pysim MMIFMaster lets a read and a write run at once.  Probe: wrap
#: the pysim master in a capacity-1 resource and per_view drops to 742 -- equal to one_front, where the
#: single front serializes them anyway.  Under the same master model pysim is 4-9% optimistic
#: (742 vs 776 / 811).  Which master model is right is open (plans/mm_adaptor_host_endpoints.md).
#:
#: History.  2026-10-02, the single-process host (drain before every push, 4 ops per packet): 857 /
#: 823, pysim 709 for both.  Before that, a kernel that was not II=1 (it moved a whole 5-word config
#: and a whole 2-word status in one firing) ran ~1 sample per 10 cycles: 2096 cycles, 221 ops, 55 polls.
#: Where one_front's advantage over per_view comes from (a 1x2 instead of a 1x3 crossbar, or one front
#: instead of three) has NOT been isolated.
EXPECTED_CYCLES = {"per_view": 811, "one_front": 776}
