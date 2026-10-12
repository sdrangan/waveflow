"""mm_fir rung 3 (plans/mm_slave_adaptor.md): the synthesized kernel behind the adaptor, under XSI.

The system itself -- AMD's crossbar, the hand-written adaptor leaves and the csynth'd ``mm_fir``
kernel under one generated top, and the C++ host program -- is run by the example's build DAG
(``examples/mm_fir/mm_fir_build.py``: codegen, then the framework's csynth and, per topology, system_rtl
/ scenario / pysim / system_xsi / compare); this file runs that DAG and checks what it left.  The host is the
pysim ``FirHost`` on the C++ endpoints of ``xsi_mm_host.h`` (plans/mm_adaptor_host_endpoints.md Stage
2): a writer that commits each config and sends each packet behind a FirCmdHdr whose cfg_id names the
config it needs (plans/mm_fir_cfg_seq.md); and a reader that takes one output packet per input packet
and its response.  The output must equal the numpy golden bit for bit, the status must show both
configs taken, and every response must echo its packet's tx_id and intended config.

The C++ host reports its bus timing (the ``DONE`` and ``OP`` lines); the data -- every message that
crossed each host endpoint -- comes back as **traces** (``<workspace>/traces/<endpoint>``), which
:func:`trace_report` decodes here into the ``STATUS`` / ``RESP`` / ``Y`` lines.

Run: ``pytest tests/examples/test_mm_fir_xsi.py -m xsi`` (needs Vivado, and
``python -m examples.mm_fir.mm_fir_build --through csynth`` for the kernel's csynth: the gate runs the
DAG with ``synth="check"``, so a missing or stale top FAILS it and is never synthesized here).
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from examples.mm_fir.mm_fir import PKT as _ITEM_PKT
from examples.mm_fir.mm_fir import (
    QIN,
    QOUT,
    QRESP,
    REGS,
    FirRespHdr,
    FirStatus,
    fir_golden,
    host_schedule,
)
from examples.mm_fir.mm_fir_build import HERE, NSAMP, PKT, PLAN, build_dag, scenario_x
from waveflow.build.build import BuildConfig
from waveflow.build.system_xsi import load_run
from waveflow.toolchain.toolchain import find_vivado_path
from waveflow.utils.burst_io import read_burst_bundle

WORK = Path(__file__).resolve().parents[2] / "tests" / "build" / "_xsi_work"


def trace_report(traces) -> str:
    """The data the host collected, decoded from its traces: ``STATUS`` (the final status),
    ``RESP`` (responses, and how many did not echo what the scenario expected), ``Y`` (the outputs)."""
    traces = Path(traces)
    expected = [(int(b[2]), int(b[3])) for b in read_burst_bundle(Path(traces).parent / "scenario")
                if int(b[0]) == _ITEM_PKT]
    resp = [FirRespHdr().deserialize(b, word_bw=64)
            for b in read_burst_bundle(traces / "qresp")]
    bad = sum(1 for r, (tx, want) in zip(resp, expected)
              if (r.tx_id, r.cfg_id) != (tx, want)) + abs(len(resp) - len(expected))
    lines = [f"RESP n={len(resp)} mismatches={bad}"]
    status = read_burst_bundle(traces / "status")
    if status:
        st = FirStatus().deserialize(status[-1], word_bw=64)
        lines.append(f"STATUS nsamp={st.nsamp} ncfg={st.ncfg}")
    y = [int(w) for b in read_burst_bundle(traces / "qout") for w in np.asarray(b, dtype=np.uint64)]
    lines.append("Y" + "".join(f" {w:x}" for w in y))
    return "\n".join(lines) + "\n"


def probe_runs(out: str) -> dict[str, list[tuple[int, int]]]:
    """``{probe: [(start_cycle, length), ...]}`` from a probes run's PROBE lines
    (``mm_fir_build.build_dag(probes=True)``)."""
    res = {}
    for ln in out.splitlines():
        if ln.startswith("PROBE "):
            parts = ln.split()
            res[parts[1]] = [tuple(int(v) for v in r.split("+")) for r in parts[2:]]
    return res


def parse_kv(out: str, tag: str) -> dict[str, int]:
    line = next(ln for ln in out.splitlines() if ln.startswith(tag + " "))
    return {k: int(v) for k, v in (kv.split("=") for kv in line.split()[1:])}


def output_words(out: str) -> np.ndarray:
    """The outputs the host drained (the ``Y`` line), as signed int64."""
    y_line = next(ln for ln in out.splitlines() if ln.startswith("Y"))
    return np.array([np.int64(np.uint64(int(h, 16))) for h in y_line.split()[1:]], dtype=np.int64)


@pytest.fixture(scope="module", params=["per_view", "one_front"])
def fir_run(request):
    """The example's DAG through ``<topology>_compare``, with ``synth="check"``: its ``codegen``
    regenerates the headers first, so the stamp check compares the RTL against THIS checkout's
    generators; a stale or missing top fails the gate -- it is never synthesized here."""
    topology = request.param
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (create_ip + xsim)")
    res = build_dag(work_dir=WORK).run(BuildConfig(root_dir=HERE, params={"synth": "check"}),
                                       through=f"{topology}_compare")
    bad = {n: r.message for n, r in res.items() if not r.success and n != f"{topology}_compare"}
    if bad:
        pytest.fail(f"the mm_fir DAG failed: {bad}")
    run = load_run(WORK / f"mm_fir_{topology}")
    run.output += trace_report(run.traces)
    return topology, run


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
    endpoints is timing, and pysim is loosely timed.  The DAG's ``pysim`` and ``compare`` steps run the
    pysim side and compare (``run.trace_mismatches``)."""
    topology, run = fir_run
    assert run.pysim_traces is not None and sorted(p.name for p in run.traces.iterdir()) == \
        ["cfg", "qin", "qout", "qresp", "status"]
    assert run.trace_mismatches == [], f"{topology}: {run.trace_mismatches}"


@pytest.mark.xsi
def test_mm_fir_rtl_cycles(fir_run):
    topology, run = fir_run
    done = parse_kv(run.output, "DONE")
    pysim_cycles = run.pysim_cycles            # the same system, run in pysim by the DAG
    print({"topology": topology, "rtl": done, "pysim_cycles": pysim_cycles})
    assert done["cycles"] == EXPECTED_CYCLES[topology], (
        f"{topology}: cycle count moved: {done} (pysim {pysim_cycles})")
    # The timing model is calibrated against this RTL (MmFir's hdr/tail/restart cycles, measured with
    # the system top's probes): pysim within PYSIM_TOLERANCE of it.
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
