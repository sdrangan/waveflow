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

Nothing about that system is restated here (``plans/xsi_system_top.md``):

* the **top** is walked from the pysim system (:func:`system_spec`, ``waveflow.build.system_top``) with
  the two kernels and the memory as the cut;
* the **host** is :class:`~examples.markov.markov.MarkovHost`'s own C++ realization, ``markov_host.h``
  beside it, named by its ``bfm_model()`` -- a writer that sends each command on ``qcmd`` (room
  interrupt) while at most ``MAX_IN_FLIGHT`` jobs are out, and a reader that takes each response on
  ``qresp`` (data interrupt) and then reads that job's ``x`` from the memory.  Nothing polls.  It runs
  the **scenario bundle** the Python host writes;
* the **harness** is generated (``system_tb_spec`` / ``render_system_tb``).

The C++ host reports its bus timing (``DONE``, ``JOBT``, ``OP``); the data -- the responses and each
job's ``x`` -- comes back as **traces**, decoded here (:func:`trace_report`) into the ``JOB`` lines.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import numpy as np

from examples.markov.markov import DW, QDEPTH, U8, MarkovSystem, MkvResp, default_jobs
from waveflow.build.axi_xbar import AxiXbarConfig, generate_axi_xbar
from waveflow.build.mm_adaptor_gen import leaf_sources
from waveflow.build.mm_writer_gen import writer_top_name
from waveflow.build.system_top import (
    SystemTopSpec,
    render_system_tb,
    render_system_top,
    system_tb_spec,
    system_top_spec,
)
from waveflow.build.xsi_workspace import XsiWorkspace
from waveflow.hw.arrayutils import read_array
from waveflow.hw.mm_device import bus_address_headers
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

#: Timing probes (plans: markov-timing): one-bit handshakes the top exposes as outputs when built with
#: ``probes=True``, and the testbench samples every cycle.  Off for the gate.  The nets are the pysim
#: system's channels, by name: a view's ``<kernel>_k_<view>``, the credit link's ``u_fwd`` (``_q`` past
#: its FIFO) and ``u_crd``; the SI slots are the crossbar's master order.
PROBES = {
    "cmd": "gen_k_qcmd_TVALID && gen_k_qcmd_TREADY",        # host's command reaches the generator
    "ufwd": "u_fwd_TVALID && u_fwd_TREADY",                 # generator -> its queue writer, a word
    "ufwd_last": "u_fwd_TVALID && u_fwd_TREADY && u_fwd_TLAST",
    "wr1_aw": "si1_axi_AWVALID && si1_axi_AWREADY",          # queue writer: a burst issued
    "wr1_b": "si1_axi_BVALID && si1_axi_BREADY",             # ... and acknowledged
    "u": "chain_k_qu_TVALID && chain_k_qu_TREADY",           # chain takes a word from its queue in
    "crd": "u_crd_TVALID && u_crd_TREADY",                   # chain offers credit
    "wr2_aw": "si2_axi_AWVALID && si2_axi_AWREADY",          # credit writer: a credit write
    "ucrd": "gen_k_u_crd_TVALID && gen_k_u_crd_TREADY",      # generator takes a credit value
    "wr3_aw": "si3_axi_AWVALID && si3_axi_AWREADY",          # chain's memory writer: a burst
    "wr3_b": "si3_axi_BVALID && si3_axi_BREADY",
    "resp": "chain_k_qresp_TVALID && chain_k_qresp_TREADY",  # a response word into qresp
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


def address_headers() -> dict[str, str]:
    """The headers the C++ host includes, found by walking the pysim crossbar: the two kernel types'
    layouts and this system's bases (the memory included)."""
    return bus_address_headers(system().xbar, system="markov")


def workspace(work_dir, probes: bool = False) -> Path:
    return Path(work_dir).resolve() / ("markov_probes" if probes else "markov")


def scenario_path(work_dir, probes: bool = False) -> Path:
    """The scenario bundle both hosts run -- written into the workspace by :func:`run_xsi`."""
    return workspace(work_dir, probes) / "scenario"


def trace_dir(work_dir, probes: bool = False) -> Path:
    """Where the C++ host dumps its endpoints' traces (one bundle per endpoint)."""
    return workspace(work_dir, probes) / "traces"


def trace_report(out: str, traces) -> str:
    """One ``JOB <tx> ones=<n> t=<cycle> X <words>`` line per job: the response and the ``x`` words
    from the traces -- the k-th region read follows the k-th response -- and the completion cycle
    from the host's ``JOBT`` line."""
    traces = Path(traces)
    t = {int(m[1]): int(m[2]) for m in re.finditer(r"^JOBT (\d+) t=(\d+)", out, re.M)}
    resp = [MkvResp().deserialize(np.asarray(b, dtype=np.uint64), word_bw=DW)
            for b in read_burst_bundle(traces / "qresp")]
    xs = read_burst_bundle(traces / "mem")
    lines = []
    for r, x in zip(resp, xs):
        tx = int(r.tx_id)
        lines.append(f"JOB {tx} ones={int(r.ones)} t={t.get(tx, -1)} X"
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
        words = np.array([int(h, 16) for h in xs.split()], dtype=np.uint64)
        x = np.asarray(read_array(words, U8, word_bw=DW, shape=jobs[j]["n"]).val, dtype=np.uint8)
        res[j] = {"ones": int(kv["ones"]), "t": int(kv["t"]), "x": x}
    return res


def run_xsi(work_dir, timeout: int = 3600, probes: bool = False) -> str:
    """Generate the crossbar, the top and the testbench, and run XSI.  Returns the host's report
    followed by :func:`trace_report`.  Needs Vivado and the four csynth'd tops
    (``python -m examples.markov.markov_build``)."""
    for t in TOPS:
        if not rtl_dir(t).is_dir():
            raise FileNotFoundError(f"no csynth RTL for {t}: run python -m examples.markov.markov_build")
    work_dir = Path(work_dir).resolve()        # Vivado runs in the IP directory: no relative paths
    sysm = system()
    spec = system_spec(sysm)
    ip = generate_axi_xbar(spec.xbar, work_dir / "ip")
    ws = XsiWorkspace(workspace(work_dir, probes), top=spec.top)
    host = sysm.host
    host.scenario = scenario_path(work_dir, probes).as_posix()
    host.trace_dir = trace_dir(work_dir, probes).as_posix()
    host.write_scenario(host.scenario)
    shutil.rmtree(host.trace_dir, ignore_errors=True)        # a stale trace would describe another run
    tb = system_tb_spec(spec, sysm.xbar, [host], probes=list(PROBES) if probes else ())
    main, tb_files = render_system_tb(spec, tb)
    rtl = [f for t in spec.modules for f in sorted(rtl_dir(t).glob("*.v"))]
    ws.prepare(rtl_files=ip.sim_files + leaf_sources() + rtl + [f"{spec.top}.v"],
               include_dirs=ip.include_dirs, tb_name="markov_tb", tb_cpp=main,
               extra_files={f"{spec.top}.v": render_system_top(spec, PROBES if probes else None),
                            **tb_files, **address_headers()})
    out = ws.run(timeout=timeout)
    return out + trace_report(out, host.trace_dir)


if __name__ == "__main__":
    import sys
    out = run_xsi(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "xsi_work")
    print(out[-4000:])
