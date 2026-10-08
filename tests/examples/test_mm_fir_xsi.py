"""mm_fir rung 3 (plans/mm_slave_adaptor.md): the synthesized kernel behind the adaptor, under XSI.

The system itself -- AMD's crossbar, the hand-written adaptor leaves and the csynth'd ``mm_fir``
kernel under one generated top, and the C++ host program -- is built by
``examples/mm_fir/mm_fir_xsi.py``; this file only runs it and checks it.  The host is the pysim
``FirHost`` on the C++ endpoints of ``xsi_mm_host.h`` (plans/mm_adaptor_host_endpoints.md Stage 2): a
writer that commits each config and sends each packet behind a FirCmdHdr whose cfg_id names the config
it needs (plans/mm_fir_cfg_seq.md); and a reader that takes one output packet per input packet and its
response.  The output must equal the numpy golden bit for bit, the status must show both configs taken,
and every response must echo its packet's tx_id and intended config.

Run: ``pytest tests/examples/test_mm_fir_xsi.py -m xsi`` (needs Vivado, and
``python -m examples.mm_fir.mm_fir_build`` for the kernel's csynth).
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from examples.mm_fir.mm_fir import QIN, QOUT, QRESP, REGS, fir_golden, host_schedule
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
def fir_run(request):
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
    _topology, run = fir_run
    fir_run = run.output
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
def test_mm_fir_rtl_host_never_polls(fir_run):
    """The host waits on the views' interrupts (plans/mm_irq.md): among every bus operation the
    testbench host issued, no read of queue in's vacancy or of a queue out's occupancy, and the status
    read exactly once."""
    _topology, run = fir_run
    out = run.output
    reads = [int(m[1], 16) for m in re.finditer(r"OP R 0x([0-9a-f]+)", out)]
    counts = [a for a in reads if QIN <= a < QIN + 0x1000 or QOUT + 0x800 <= a < QOUT + 0x1000
              or QRESP + 0x800 <= a < QRESP + 0x1000]
    assert reads and counts == [], [hex(a) for a in counts]
    assert reads.count(REGS + 0xC00) == 1
    assert parse_kv(out, "DONE")["polls"] == 0


@pytest.mark.xsi
def test_mm_fir_host_traces_match_pysim(fir_run):
    """The host conformance gate (plans/xsi_system_top.md): FirHost and its C++ realization,
    FirHostModel, run the SAME scenario bundle, and every host endpoint's trace -- each config
    committed, each packet sent, the words each read took, the status read -- is byte-identical
    between the pysim run and the RTL run.  Per endpoint, not globally: the interleaving across
    endpoints is timing, and pysim is loosely timed.  ``run_system_xsi`` runs the pysim side and
    compares (``run.trace_mismatches``)."""
    topology, run = fir_run
    assert run.pysim_traces is not None and sorted(p.name for p in run.traces.iterdir()) == \
        ["cfg", "qin", "qout", "qresp", "status"]
    assert run.trace_mismatches == [], f"{topology}: {run.trace_mismatches}"


@pytest.mark.xsi
def test_mm_fir_rtl_cycles(fir_run):
    topology, run = fir_run
    done = parse_kv(run.output, "DONE")
    pysim_cycles = run.pysim_cycles            # the same system, run in pysim by run_system_xsi
    print({"topology": topology, "rtl": done, "pysim_cycles": pysim_cycles})
    assert done["cycles"] == EXPECTED_CYCLES[topology], (
        f"{topology}: cycle count moved: {done} (pysim {pysim_cycles})")
    # The timing model is calibrated against this RTL (MmFir's hdr/tail/restart cycles, measured with
    # mm_fir_xsi's probes): pysim within PYSIM_TOLERANCE of it.
    assert abs(pysim_cycles - done["cycles"]) <= PYSIM_TOLERANCE * done["cycles"], (
        f"{topology}: pysim {pysim_cycles:.0f} vs RTL {done['cycles']}")


#: Recorded 2026-10-03, with NO polling (plans/mm_irq.md): the host waits on the queue views'
#: interrupts -- queue in's for room, queue out's and the response FIFO's for data -- and reads the final
#: status once (the kernel publishes it before each response).  Header + cfg_id protocol, samples
#: packed four to a word, bus master overlapping one read and one write.  per_view 520 / one_front 529,
#: 67 bus ops, 0 polls.  pysim 498 / 545 (-4.2% / +3.0%).
#:
#: History (same RTL scenario):
#:   * the same protocol with the kernel body a single-firing STATE MACHINE (one word per stream per
#:     firing, no pipeline drain between packets): 520 / 529.  The loop-style body (one packet per
#:     firing, a pipelined sample loop) costs a pipeline fill + drain and a few cycles per packet --
#:     13 packets of 16 samples -- for code that reads like run_iter (2026-10-04, the user's choice);
#:   * the same protocol with the endpoints POLLING the counts: 768 / 783 (124 / 125 ops), pysim
#:     734 / 792 after three model fixes (crossbar travel 2 of 4 cycles; one read + one write per
#:     master; 2 cycles of host pacing) -- 536 / 874 before them;
#:   * header pattern with ONE sample per 64-bit word (hand-packed): 937 / 922 (143 / 124 ops), pysim
#:     575 / 1023 -- the serializer's packing moves a quarter of the sample words;
#:   * apply_at protocol, two-process host, overlap master: 567 / 721 (76 / 74 ops), pysim 423 / 742;
#:   * the same with the one-at-a-time master: 811 / 776 (73 ops), pysim 742 / 742 when its master is
#:     forced to one transaction too;
#:   * the single-process host (drain before every push): 857 / 823, pysim 709;
#:   * a kernel that was not II=1 (a whole config / status per firing): 2096 cycles, 221 ops.
#: The header pattern costs ~6 bus ops per packet against ~4 -- the header's own vacancy poll and write,
#: and the response FIFO's poll and pop -- more than dropping the status wait saved.
EXPECTED_CYCLES = {"per_view": 618, "one_front": 611}
#: pysim 635 / 635 -- +2.8% / +3.9% -- once it charges the loop body's measured per-packet costs.
PYSIM_TOLERANCE = 0.05
