"""mm_fir_xsi.py — the mm_fir system at RTL: crossbar + adaptor + kernel, driven by the C++ host.

Rung 3 of ``plans/mm_slave_adaptor.md``'s witness.  Everything here is real RTL -- AMD's crossbar
(:mod:`waveflow.build.axi_xbar`), the hand-written adaptor leaves, and the csynth'd ``mm_fir`` kernel --
under one Verilog top, simulated through XSI.  Nothing about that system is restated here:
:func:`run_xsi` hands the pysim system object to ``waveflow.build.system_xsi.run_system_xsi``, which
walks it to the top (the kernel is the cut), generates the harness around the host's C++ twin --
``FirHostModel`` in ``mm_fir_host.h``, two threads on generated endpoints (``plans/host_runtime.md``)
-- runs it from the scenario the Python host writes, and checks the host against pysim.

Two topologies with one address map (view *k* at ``REGS + k * 4 KB``):

* ``per_view``  -- a 1x4 crossbar; each view its own MI slot and its own front;
* ``one_front`` -- all four views behind ONE front and a generated decoder, on MI0 of a 1x2 crossbar.

    run = run_xsi("one_front", work_dir)      # needs Vivado and the kernel's csynth (mm_fir_build)
    parse_kv(run.output, "STATUS")            # {'nsamp': 200, 'ncfg': 2}
    run.cycles, run.trace_mismatches          # 611, []

The C++ host reports its bus timing (the ``DONE`` and ``OP`` lines); the data -- every message that
crossed each host endpoint -- comes back as **traces** (``<workspace>/traces/<endpoint>``), which
:func:`trace_report` decodes here, in Python, into the ``STATUS`` / ``RESP`` / ``Y`` lines.  The gate
is ``tests/examples/test_mm_fir_xsi.py``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from examples.mm_fir.mm_fir import PKT as _ITEM_PKT
from examples.mm_fir.mm_fir import FirRespHdr, FirStatus, MmFirSystem
from waveflow.build.axi_xbar import AxiXbarConfig
from waveflow.build.system_top import SystemTopSpec, beat, stall, system_top_spec
from waveflow.build.system_xsi import XsiRun, run_system_xsi
from waveflow.utils.burst_io import read_burst_bundle

ROOT = Path(__file__).resolve().parent
RTL = ROOT / "mm_fir_proj" / "solution1" / "syn" / "verilog"

#: Two topologies, one address map (view k at REGS + k * 4 KB either way):
#:   per_view  -- a 1x4 crossbar, each view its own MI slot and its own front (Stages 1-2);
#:   one_front -- all four views behind ONE front and a generated decoder (Stage 4), on MI0 of a 1x2
#:                crossbar.  MI1 is a stub nothing addresses: a 1x1 crossbar is degenerate (create_ip
#:                generates an inconsistent 2-MI IP for it -- see AxiXbarConfig), and a real system has
#:                more than one slave anyway.
#: Their crossbars' IP names.  Nothing else about the top is written here: :func:`system_spec` walks
#: the pysim system (``waveflow.build.system_top``) -- the crossbar's ranges are where
#: ``assign_address_ranges`` set them (``plans/bus_address_map.md`` D4).
XBAR_NAMES = {"per_view": "xbar_mm4_1x4", "one_front": "xbar_mm1_1x2"}

NSAMP, SWITCH_AT, PKT = 200, 101, 16
TAPS_A = [3, -1, 4, 1, -5]
TAPS_B = [2, 7, 1, -8, 2, 8, 1, -8]
PLAN = [(0, TAPS_A), (SWITCH_AT, TAPS_B)]

def timing_probes(sysm: MmFirSystem) -> dict:
    """Timing probes: one-bit handshakes the top exposes as outputs when built with ``probes=True``;
    the testbench samples them every cycle and prints the cycles each fired.  Off for the gate.  Each
    names the kernel port it watches; the system top resolves the net."""
    fir = sysm.fir
    return {
        "in": beat(fir.s_in),             # the kernel takes a word from queue in (header or samples)
        "cfg": beat(fir.s_cfg),           # ... a config word
        "out": beat(fir.m_out),           # a result into queue out
        "resp": beat(fir.m_resp),         # a response word
        "stat": beat(fir.m_status),       # a status word
        "out_full": stall(fir.m_out),     # the kernel held up by a full queue out
    }


def scenario_x() -> np.ndarray:
    return np.random.default_rng(7).integers(-2000, 2000, size=NSAMP)


def system(topology: str) -> MmFirSystem:
    """The pysim system for *topology*, on the gate's scenario."""
    if topology not in XBAR_NAMES:
        raise ValueError(f"topology must be one of {sorted(XBAR_NAMES)}, got {topology!r}")
    return MmFirSystem(x=list(scenario_x()), plan=PLAN, pkt=PKT, one_front=topology == "one_front")


def system_spec(topology: str, sysm: MmFirSystem | None = None) -> SystemTopSpec:
    """The RTL top for *topology*, walked from the pysim system: the kernel is the cut, so its device
    (views, adaptor) is inside, and the host's bus master and interrupt lines are top ports."""
    sysm = sysm or system(topology)
    return system_top_spec(sysm.xbar, [sysm.fir], top="mm_fir_top", xbar_name=XBAR_NAMES[topology])


def xbar_config(topology: str) -> AxiXbarConfig:
    """The RTL crossbar for *topology*, generated from the pysim system's own crossbar -- the same
    slaves at the same ranges, so an address is written once (``MM_BASE`` + the type's layout)."""
    return system_spec(topology).xbar


def trace_report(traces) -> str:
    """The data the host collected, decoded from its traces: ``STATUS`` (the final status),
    ``RESP`` (responses, and how many did not echo what the scenario expected), ``Y`` (the outputs)."""
    traces = Path(traces)
    expected = [(int(b[2]), int(b[3])) for b in read_burst_bundle(Path(traces).parent / "scenario")
                if int(b[0]) == _ITEM_PKT]
    resp = [FirRespHdr().deserialize(np.asarray(b, dtype=np.uint64), word_bw=64)
            for b in read_burst_bundle(traces / "qresp")]
    bad = sum(1 for r, (tx, want) in zip(resp, expected)
              if (int(r.tx_id), int(r.cfg_id)) != (tx, want)) + abs(len(resp) - len(expected))
    lines = [f"RESP n={len(resp)} mismatches={bad}"]
    status = read_burst_bundle(traces / "status")
    if status:
        st = FirStatus().deserialize(np.asarray(status[-1], dtype=np.uint64), word_bw=64)
        lines.append(f"STATUS nsamp={int(st.nsamp)} ncfg={int(st.ncfg)}")
    y = [int(w) for b in read_burst_bundle(traces / "qout") for w in np.asarray(b, dtype=np.uint64)]
    lines.append("Y" + "".join(f" {w:x}" for w in y))
    return "\n".join(lines) + "\n"


def probe_runs(out: str) -> dict[str, list[tuple[int, int]]]:
    """``{probe: [(start_cycle, length), ...]}`` from a ``probes=True`` run's PROBE lines."""
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


def run_xsi(topology: str, work_dir, timeout: int = 3600, probes: bool = False) -> XsiRun:
    """The system at RTL (``waveflow.build.system_xsi.run_system_xsi``): its top, its host's C++ twin
    and the harness generated from the pysim system, run under XSI, and the host checked against pysim
    on the same scenario (``run.trace_mismatches``).  The returned output carries the host's report
    plus :func:`trace_report`'s lines.

    Needs Vivado (``create_ip`` + xsim) and the kernel's RTL (``python -m examples.mm_fir.mm_fir_build``).
    """
    sysm = system(topology)
    run = run_system_xsi(sysm, work_dir, top="mm_fir_top", xbar_name=XBAR_NAMES[topology],
                         workspace=f"mm_fir_{topology}",
                         probes=timing_probes(sysm) if probes else None, timeout=timeout)
    run.output += trace_report(run.traces)
    return run
