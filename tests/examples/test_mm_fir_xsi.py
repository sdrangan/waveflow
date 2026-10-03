"""mm_fir rung 3 (plans/mm_slave_adaptor.md): the synthesized kernel behind the adaptor, under XSI.

The system itself -- AMD's crossbar, the hand-written adaptor leaves and the csynth'd ``mm_fir``
kernel under one generated top, and the C++ host program -- is built by
``examples/mm_fir/mm_fir_xsi.py``; this file only runs it and checks it.  The host is the pysim
``FirHost`` on the C++ endpoints of ``xsi_mm_host.h`` (plans/mm_adaptor_host_endpoints.md Stage 2): a
writer that commits each config and sends each packet behind a FirCmdHdr whose cfg_seq names the config
it needs (plans/mm_fir_cfg_seq.md); and a reader that takes one output packet per input packet and its
response.  The output must equal the numpy golden bit for bit, the status must show both configs taken,
and every response must echo its packet's tx_id and intended config.

Run: ``pytest tests/examples/test_mm_fir_xsi.py -m xsi`` (needs Vivado, and
``python -m examples.mm_fir.mm_fir_build`` for the kernel's csynth).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.mm_fir.mm_fir import MmFirSystem, fir_golden, host_schedule
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
    assert st == {"nsamp": NSAMP, "ncfg": 2}, st
    # Every packet answered, each echoing its tx_id and the config it was meant to use.
    npkt = sum(1 for it in host_schedule(NSAMP, PLAN, PKT) if it[0] == "pkt")
    assert parse_kv(fir_run, "RESP") == {"n": npkt, "mismatches": 0}
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


#: Recorded 2026-10-03, after mm_fir moved to the in-band header pattern (plans/mm_fir_cfg_seq.md):
#: every packet is a FirCmdHdr write and a sample write to queue in, and a FirRespHdr read from the
#: response FIFO; the host never polls the status for "received".  Bus master overlaps one read and one
#: write (mm_fir_xsi.OVERLAP_RW).  per_view 937 (143 ops, 12 polls) / one_front 922 (124 ops, 3 polls).
#:
#: pysim says 575 / 1023 -- and so gets the ORDER of the two topologies wrong (RTL: nearly equal; pysim:
#: per_view far faster).  Not attributed; it is the same open question as the earlier 25% gap.
#:
#: History (same host program shape, same RTL kernel unless noted):
#:   * apply_at protocol, two-process host, overlap master: 567 / 721 (76 / 74 ops), pysim 423 / 742;
#:   * the same with the one-at-a-time master: 811 / 776 (73 ops), pysim 742 / 742 when its master is
#:     forced to one transaction too;
#:   * the single-process host (drain before every push): 857 / 823, pysim 709;
#:   * a kernel that was not II=1 (a whole config / status per firing): 2096 cycles, 221 ops.
#: The header pattern costs ~6 bus ops per packet against ~4 -- the header's own vacancy poll and write,
#: and the response FIFO's poll and pop -- more than dropping the status wait saved.
EXPECTED_CYCLES = {"per_view": 937, "one_front": 922}
