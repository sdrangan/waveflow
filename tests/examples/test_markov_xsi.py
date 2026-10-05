"""The Markov system at RTL (``plans/mm_credit_stream.md`` Stage 4), under XSI.

Four bus masters on AMD's crossbar -- the host, the generator's queue writer, the chain's credit writer
and the chain's memory writer -- two csynth'd kernels joined by a routed credit stream, and a BRAM as
the shared memory, all built by ``examples/markov/markov_xsi.py``.  The host is the pysim
``MarkovHost`` on the C++ endpoints: commands on ``qcmd`` (room interrupt), responses on ``qresp``
(data interrupt), then a read of each job's ``x``.

Gates: every job's ``x`` bit-exact against the golden; the host never reads a vacancy or occupancy;
and the run's cycle count, recorded.

Run: ``pytest tests/examples/test_markov_xsi.py -m xsi`` (needs Vivado, and
``python -m examples.markov.markov_build`` for the four csynth'd tops).
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from examples.markov.markov import CHAIN_BASE, CHAIN_LAYOUT, MEM_BASE, markov_golden
from examples.markov.markov_xsi import ROOT, TOPS, job_results, parse_kv, rtl_dir, run_xsi, scenario_jobs
from waveflow.build.trace_steps import rtl_staleness
from waveflow.toolchain.toolchain import find_vivado_path

WORK = Path(__file__).resolve().parents[2] / "tests" / "build" / "_xsi_work"

#: The recorded RTL cycle count of the scenario (4 jobs x 300 steps, 2 in flight).
#: History (2026-10-04, branch markov-timing, found with markov_xsi's timing probes):
#:   2356  both kernel bodies single-firing state machines;
#:   2246  rewritten as straight-line loops per job (long firings: the per-chunk drain is small);
#:   2015  a FIFO between the generator and its store-and-forward queue writer (the generator stalled
#:         for every burst -- 103 cycles a 64-draw chunk -- because nothing buffered it);
#:   1865  the chain's queue 64 -> 128 words: the credit window must cover the link's bandwidth-delay
#:         product, or credit throttled the generator at every job start and starved the chain;
#:   1870  the credit handling moved into the framework's credit::Producer / credit::Consumer
#:         (credit_stream_hls.h) -- equivalent logic, scheduled slightly differently (+5 cycles).
EXPECTED_CYCLES = 1870
#: pysim must stay within this of RTL (it is +3.3%, with the two per-chunk overheads it now charges).
PYSIM_TOLERANCE = 0.05


@pytest.fixture(scope="module")
def markov_run() -> str:
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (create_ip + xsim)")
    for t in TOPS:
        if not rtl_dir(t).is_dir():
            pytest.skip(f"XSI gate prerequisite missing: no csynth RTL for {t} -- run "
                        f"python -m examples.markov.markov_build")
        stale = rtl_staleness(ROOT, t)
        if stale is not None:
            pytest.skip(f"XSI gate prerequisite missing: {stale}")
    return run_xsi(WORK)


@pytest.mark.xsi
def test_markov_rtl_bit_exact(markov_run):
    done = parse_kv(markov_run, "DONE")
    assert done["done"] == 1, markov_run[-3000:]
    res = job_results(markov_run)
    jobs = scenario_jobs()
    assert sorted(res) == list(range(len(jobs)))
    for j, job in enumerate(jobs):
        g = markov_golden(job)
        assert np.array_equal(res[j]["x"], g), f"job {j}: RTL x differs from the golden"
        assert res[j]["ones"] == int(g.sum())


@pytest.mark.xsi
def test_markov_rtl_host_never_polls(markov_run):
    """Every host read is a response pop (qresp's lower half) or an x read (the memory): no vacancy,
    no occupancy -- both queue endpoints sleep on interrupts."""
    qresp = CHAIN_LAYOUT.at(CHAIN_BASE)["qresp"]
    reads = [int(m[1], 16) for m in re.finditer(r"OP R 0x([0-9a-f]+)", markov_run)]
    bad = [a for a in reads if not (a >= MEM_BASE or qresp.base <= a < qresp.base + qresp.window // 2)]
    assert reads and bad == [], [hex(a) for a in bad]
    assert parse_kv(markov_run, "DONE")["polls"] == 0


@pytest.mark.xsi
def test_markov_rtl_cycles(markov_run):
    assert parse_kv(markov_run, "DONE")["cycles"] == EXPECTED_CYCLES


@pytest.mark.xsi
def test_markov_pysim_tracks_rtl(markov_run):
    """The timing model is calibrated against this RTL: pysim's total within PYSIM_TOLERANCE."""
    from examples.markov.markov import MarkovSystem
    sysm = MarkovSystem(jobs=scenario_jobs(), link="mm")
    sysm.run()
    pysim = sysm.sim.env.now / sysm.clk.period
    rtl = parse_kv(markov_run, "DONE")["cycles"]
    assert abs(pysim - rtl) <= PYSIM_TOLERANCE * rtl, f"pysim {pysim:.0f} vs RTL {rtl}"
