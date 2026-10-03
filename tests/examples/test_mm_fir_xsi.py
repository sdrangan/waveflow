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


#: Recorded 2026-10-03, with the two-process host on the C++ endpoints (xsi_mm_host.h) and a bus
#: master that may have one read and one write outstanding at once (mm_fir_xsi.OVERLAP_RW, i.e.
#: AxiMmMaster's overlap_rw): host program start to the final status read, 200 samples, one tap
#: switch.  per_view 567 (76 ops, 9 polls) / one_front 721 (74 ops, 7 polls).
#:
#: pysim says 423 / 742.  one_front: pysim 3% pessimistic.  per_view: pysim 25% optimistic -- the
#: remaining gap is not attributed yet.
#:
#: The same host with the one-at-a-time master (OVERLAP_RW = False) measured 811 / 776 (73 ops,
#: 6 polls); a pysim master forced to one transaction at a time gives 742 for both, so that is the
#: model the overlap removed.  one_front gains too (776 -> 721): its front still serves one
#: transaction at a time, but the master no longer waits for a write's response before presenting
#: the next read's address.
#:
#: History.  The single-process host (drain before every push, 4 ops per packet), one-at-a-time
#: master: 857 / 823, pysim 709 for both, on the same RTL.  Before that, a kernel that was not II=1 ran
#: ~1 sample per 10 cycles: 2096 cycles, 221 ops, 55 polls.  Where one_front differs from per_view
#: beyond the master (a 1x2 instead of a 1x3 crossbar, one front instead of three) is not isolated.
EXPECTED_CYCLES = {"per_view": 567, "one_front": 721}
