"""markov_xsi.py — the Markov system at RTL: four bus masters, one crossbar, two kernels, under XSI.

Stage 4 of ``plans/mm_credit_stream.md``.  Everything is real RTL under one Verilog top:

* AMD's ``axi_crossbar``, generated from the pysim system's own crossbar (``AxiXbarConfig.from_crossbar``)
  -- 4 SI: the host (the testbench's ``AxiMmMaster``), the generator's queue writer, the chain's credit
  writer, the chain's memory writer;  3 MI: the generator's adaptor (``qcmd`` queue in, ``u_crd``
  credit in), the chain's adaptor (``qu`` queue in, ``qresp`` queue out), and the shared memory -- a
  BRAM window behind its own front, which echoes AXI IDs as a four-master crossbar needs;
* the four csynth'd tops: ``markov_gen``, ``markov_chain`` (its core + the in-band memory writer),
  the queue writer and the credit writer (``waveflow/build/mm_writer_gen.py``).  Each writer's
  ``target`` -- its peer view's bus word index -- is a constant the top drives, so neither kernel's RTL
  depends on placement.

Nothing about that system is restated here: :func:`run_xsi` hands the pysim system object to
``waveflow.build.system_xsi.run_system_xsi``, which walks it to the top (the two kernels and the memory
are the cut), generates the harness around the host's C++ twin -- ``MarkovHostModel`` in
``markov_host.h``: a producer thread sending commands while at most ``max_in_flight`` jobs are out, a
consumer thread taking each response and reading that job's ``x`` back, on generated endpoints
(``plans/host_runtime.md``) -- runs it from the scenario the Python host writes, and checks the host
against pysim.  Nothing polls.

The C++ host reports its bus timing (``DONE``, ``JOBT``, ``OP``); the data -- the responses and each
job's ``x`` -- comes back as **traces**, decoded here (:func:`trace_report`) into the ``JOB`` lines.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from examples.markov.markov import DW, QDEPTH, U8, MarkovSystem, MkvResp, default_jobs
from waveflow.build.axi_xbar import AxiXbarConfig
from waveflow.build.mm_writer_gen import writer_top_name
from waveflow.build.system_top import SystemTopSpec, beat, last, system_top_spec
from waveflow.build.system_xsi import XsiRun, run_system_xsi
from waveflow.hw.arrayutils import read_array
from waveflow.utils.burst_io import read_burst_bundle

ROOT = Path(__file__).resolve().parent
#: The four csynth'd tops.  The writers' names are derived as markov_build derives them (the queue
#: writer's longest packet is the queue depth), never restated.
QWRITER = writer_top_name("queue", DW, QDEPTH)
CWRITER = writer_top_name("credit", DW)
TOPS = ("markov_gen", "markov_chain", QWRITER, CWRITER)


def rtl_dir(top: str) -> Path:
    return ROOT / f"{top}_proj" / "solution1" / "syn" / "verilog"


XBAR_NAME = "xbar_markov_4x3"
#: The testbench scenario: four jobs of 300 steps (two in flight at a time).
NJOBS, NSTEPS = 4, 300

def timing_probes(sysm: MarkovSystem) -> dict:
    """Timing probes (plans: markov-timing): one-bit handshakes the top exposes as outputs when built
    with ``probes=True``, and the testbench samples every cycle.  Off for the gate.  Each names the
    pysim object it watches -- a kernel's port, or a bus master and an AXI channel -- and the system
    top resolves the net."""
    gen, chain, link = sysm.gen, sysm.chain, sysm.u_link
    return {
        "cmd": beat(gen.s_cmd),                          # host's command reaches the generator
        "ufwd": beat(gen.m_u.fwd_ep),                    # generator -> its queue writer, a word
        "ufwd_last": last(gen.m_u.fwd_ep),               # ... the last word of a chunk
        "wr1_aw": beat(link.fwd_writer.m_mem, "AW"),     # queue writer: a burst issued
        "wr1_b": beat(link.fwd_writer.m_mem, "B"),       # ... and acknowledged
        "u": beat(chain.s_u.fwd_ep),                     # chain takes a word from its queue in
        "crd": beat(chain.s_u.crd_ep),                   # chain offers credit
        "wr2_aw": beat(link.crd_writer.m_mem, "AW"),     # credit writer: a credit write
        "ucrd": beat(gen.m_u.crd_ep),                    # generator takes a credit value
        "wr3_aw": beat(chain.m_mem, "AW"),               # chain's memory writer: a burst
        "wr3_b": beat(chain.m_mem, "B"),
        "resp": beat(chain.m_resp),                      # a response word into qresp
    }


def scenario_jobs() -> list[dict]:
    return default_jobs(NJOBS, NSTEPS)


def system() -> MarkovSystem:
    return MarkovSystem(jobs=scenario_jobs(), link="mm")


def system_spec(sysm: MarkovSystem | None = None) -> SystemTopSpec:
    """The RTL top, walked from the pysim system with the two kernels and the shared memory as the cut:
    their adaptors and the credit link's two writers come with them; the host is outside."""
    sysm = sysm or system()
    return system_top_spec(sysm.xbar, [sysm.gen, sysm.chain, sysm.mem], top="markov_top",
                           xbar_name=XBAR_NAME)


def xbar_config() -> AxiXbarConfig:
    """The RTL crossbar, generated from the pysim crossbar: the same slaves at the same ranges."""
    return system_spec().xbar


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
    """``{probe: [(start_cycle, length), ...]}`` from a ``probes=True`` run's PROBE lines."""
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


def run_xsi(work_dir, timeout: int = 3600, probes: bool = False) -> XsiRun:
    """The system at RTL (``waveflow.build.system_xsi.run_system_xsi``): its top, its host's C++ twin
    and the harness generated from the pysim system, run under XSI, and the host checked against pysim
    on the same scenario (``run.trace_mismatches``).  The returned output carries the host's report
    plus :func:`trace_report`'s ``JOB`` lines.  Needs Vivado and the four csynth'd tops
    (``python -m examples.markov.markov_build``)."""
    sysm = system()
    run = run_system_xsi(sysm, work_dir, top="markov_top", xbar_name=XBAR_NAME, workspace="markov",
                         probes=timing_probes(sysm) if probes else None, timeout=timeout)
    run.output += trace_report(run.output, run.traces)
    return run


if __name__ == "__main__":
    import sys
    run = run_xsi(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "xsi_work")
    print(run.output[-4000:])
