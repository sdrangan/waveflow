"""The Markov system at RTL (``plans/mm_credit_stream.md`` Stage 4), under XSI.

Four bus masters on AMD's crossbar -- the host, the generator's queue writer, the chain's credit writer
and the chain's memory writer -- two csynth'd kernels joined by a routed credit stream, and a BRAM as
the shared memory, all run by the example's build DAG (``examples/markov/markov_build.py``: codegen,
then the framework's csynth / scenario / pysim / system_xsi / compare).  The host is the pysim
``MarkovHost`` on the C++ endpoints: commands on ``qcmd`` (room interrupt), responses on ``qresp``
(data interrupt), then a read of each job's ``x``.

Gates: every job's ``x`` bit-exact against the golden; the host never reads a vacancy or occupancy;
and the run's cycle count, recorded.

Run: ``pytest tests/examples/test_markov_xsi.py -m xsi`` (needs Vivado, and
``python -m examples.markov.markov_build --through csynth`` for the four csynth'd tops: the gate runs
the DAG with ``synth="check"``, so a missing or stale top FAILS it and is never synthesized here).

The DAG leaves its run in the workspace (``report.json``, ``pysim.json``, ``compare.json``, the traces);
the decoders that turn it into per-job results are here, with the tests that read them.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from examples.markov.markov import CHAIN_BASE, CHAIN_LAYOUT, DW, MEM_BASE, U8, MkvResp, markov_golden
from examples.markov.markov_build import HERE, WORKSPACE, build_dag, scenario_jobs
from waveflow.build.build import BuildConfig
from waveflow.build.system_xsi import load_run
from waveflow.hw.arrayutils import read_array
from waveflow.toolchain.toolchain import find_vivado_path
from waveflow.utils.burst_io import read_burst_bundle

WORK = Path(__file__).resolve().parents[2] / "tests" / "build" / "_xsi_work"

#: The recorded RTL cycle count of the scenario (4 jobs x 300 steps, 2 in flight).
#: History (2026-10-04, branch markov-timing, found with the system top's timing probes):
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


def trace_report(out: str, traces) -> str:
    """One ``JOB <tx> ones=<n> t=<cycle> X <words>`` line per job: the response and the ``x`` words
    from the traces -- the k-th region read follows the k-th response -- and the completion cycle
    from the host's ``JOBT`` line."""
    traces = Path(traces)
    t = {int(m[1]): int(m[2]) for m in re.finditer(r"^JOBT (\d+) t=(\d+)", out, re.M)}
    resp = [MkvResp().deserialize(b, word_bw=DW)
            for b in read_burst_bundle(traces / "qresp")]
    xs = read_burst_bundle(traces / "mem_reader")
    lines = []
    for r, x in zip(resp, xs):
        tx = r.tx_id
        lines.append(f"JOB {tx} ones={r.ones} t={t.get(tx, -1)} X"
                     + "".join(f" {int(w):x}" for w in np.asarray(x, dtype=np.uint64)))
    return "\n".join(lines) + "\n"


def parse_kv(out: str, tag: str) -> dict[str, int]:
    line = next(ln for ln in out.splitlines() if ln.startswith(tag + " "))
    return {k: int(v) for k, v in (kv.split("=") for kv in line.split()[1:] if "=" in kv)}


def probe_runs(out: str) -> dict[str, list[tuple[int, int]]]:
    """``{probe: [(start_cycle, length), ...]}`` from a probes run's PROBE lines
    (``markov_build.build_dag(probes=True)``)."""
    res = {}
    for ln in out.splitlines():
        if ln.startswith("PROBE "):
            parts = ln.split()
            res[parts[1]] = [tuple(int(v) for v in r.split("+")) for r in parts[2:]]
    return res


def job_results(out: str) -> dict[int, dict]:
    """Per job: ``ones``, completion cycle ``t``, and ``x`` decoded from the words the host read."""
    res = {}
    jobs = scenario_jobs()
    for ln in out.splitlines():
        if not ln.startswith("JOB "):
            continue
        head, xs = ln.split(" X")
        parts = head.split()
        j = int(parts[1])
        kv = dict(p.split("=") for p in parts[2:])
        words = [int(h, 16) for h in xs.split()]
        x = read_array(words, U8, word_bw=DW, shape=jobs[j]["n"]).val
        res[j] = {"ones": int(kv["ones"]), "t": int(kv["t"]), "x": x}
    return res


@pytest.fixture(scope="module")
def markov_run():
    """The example's DAG, through ``compare``, with ``synth="check"``.  Its ``codegen`` regenerates
    ``include/`` and ``gen/`` first (untracked build output, ``plans/source_layout.md``), so csynth's
    stamp check compares the RTL against THIS checkout's framework headers and schemas; a stale or
    missing top fails the gate, naming it -- it is never synthesized here."""
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (create_ip + xsim)")
    res = build_dag(work_dir=WORK).run(BuildConfig(root_dir=HERE, params={"synth": "check"}),
                                       through="compare")
    bad = {n: r.message for n, r in res.items() if not r.success and n != "compare"}
    if bad:
        pytest.fail(f"the markov DAG failed: {bad}")
    run = load_run(WORK / WORKSPACE)  # an XsiRun: .output, .cycles, .pysim_cycles, .trace_mismatches
    run.output += trace_report(run.output, run.traces)
    return run


@pytest.mark.xsi
def test_markov_rtl_bit_exact(markov_run):
    done = parse_kv(markov_run.output, "DONE")
    assert done["done"] == 1, markov_run.output[-3000:]
    res = job_results(markov_run.output)
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
    reads = [int(m[1], 16) for m in re.finditer(r"OP R 0x([0-9a-f]+)", markov_run.output)]
    bad = [a for a in reads if not (a >= MEM_BASE or qresp.base <= a < qresp.base + qresp.window // 2)]
    assert reads and bad == [], [hex(a) for a in bad]
    assert parse_kv(markov_run.output, "DONE")["polls"] == 0


@pytest.mark.xsi
def test_markov_rtl_cycles(markov_run):
    assert parse_kv(markov_run.output, "DONE")["cycles"] == EXPECTED_CYCLES


@pytest.mark.xsi
def test_markov_pysim_tracks_rtl(markov_run):
    """The timing model is calibrated against this RTL: pysim's total within PYSIM_TOLERANCE.  The
    pysim run is the same system, run by the DAG's pysim step from the same scenario."""
    pysim = markov_run.pysim_cycles
    rtl = parse_kv(markov_run.output, "DONE")["cycles"]
    assert abs(pysim - rtl) <= PYSIM_TOLERANCE * rtl, f"pysim {pysim:.0f} vs RTL {rtl}"


@pytest.mark.xsi
def test_markov_host_traces_match_pysim(markov_run):
    """The host conformance gate (plans/xsi_system_top.md): MarkovHost and MarkovHostModel run the
    SAME scenario bundle, and each host endpoint's trace -- the commands sent, the responses taken,
    the x regions read back -- is byte-identical between the pysim run and the RTL run
    (the DAG's ``pysim`` and ``compare`` steps)."""
    assert sorted(p.name for p in markov_run.traces.iterdir()) == ["mem_reader", "qcmd", "qresp"]
    assert markov_run.trace_mismatches == [], markov_run.trace_mismatches
