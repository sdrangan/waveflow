"""mm_fir_xsi.py — the mm_fir system at RTL: crossbar + adaptor + kernel, driven by the C++ host.

Rung 3 of ``plans/mm_slave_adaptor.md``'s witness.  Everything here is real RTL -- AMD's crossbar
(:mod:`waveflow.build.axi_xbar`), the hand-written adaptor leaves, and the csynth'd ``mm_fir`` kernel --
under one Verilog top, simulated through XSI.  Nothing about that system is restated here
(``plans/xsi_system_top.md``):

* the **top** is walked from the pysim system (:func:`system_spec`, ``waveflow.build.system_top``) with
  the kernel as the cut;
* the **host** is :class:`~examples.mm_fir.mm_fir.FirHost`'s own C++ realization, ``mm_fir_host.h``
  beside it, named by its ``bfm_model()``, and it runs the **scenario bundle** the Python host writes
  (:meth:`~examples.mm_fir.mm_fir.FirHost.write_scenario`) -- the same file the pysim host can run;
* the **harness** that instantiates it is generated (``system_tb_spec`` / ``render_system_tb``), and
  the address map comes from the same pysim system (:func:`address_headers`).

Two topologies with one address map (view *k* at ``REGS + k * 4 KB``):

* ``per_view``  -- a 1x4 crossbar; each view its own MI slot and its own front;
* ``one_front`` -- all four views behind ONE front and a generated decoder, on MI0 of a 1x2 crossbar.

    out = run_xsi("one_front", work_dir)      # needs Vivado and the kernel's csynth (mm_fir_build)
    parse_kv(out, "STATUS")                   # {'nsamp': 200, 'ncfg': 2}

The C++ host reports its bus timing (the ``DONE`` and ``OP`` lines); the data -- every message that
crossed each host endpoint -- comes back as **traces** (``<workspace>/traces/<endpoint>``), which
:func:`trace_report` decodes here, in Python, into the ``STATUS`` / ``RESP`` / ``Y`` lines.  The gate
is ``tests/examples/test_mm_fir_xsi.py``; it also runs the pysim host on the same scenario and requires
each endpoint's trace to be byte-identical.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np

from examples.mm_fir.mm_fir import PKT as _ITEM_PKT
from examples.mm_fir.mm_fir import FirRespHdr, FirStatus, MmFirSystem
from waveflow.build.axi_xbar import AxiXbarConfig, generate_axi_xbar
from waveflow.build.mm_adaptor_gen import leaf_sources
from waveflow.build.system_top import (
    SystemTopSpec,
    beat,
    render_system_tb,
    render_system_top,
    stall,
    system_tb_spec,
    system_top_spec,
)
from waveflow.build.xsi_workspace import XsiWorkspace
from waveflow.hw.mm_device import bus_address_headers
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


def address_headers() -> dict[str, str]:
    """The address-map headers the C++ host includes, found by walking the pysim system's crossbar
    (``bus_address_headers``): the FIR TYPE's layout (``mm_fir_layout.h``, from ``MmFir.mm_views``) and
    this SYSTEM's bases (``mm_fir_bases.h``).  The host combines them -- ``at(mm_fir_layout::qin,
    FIR)`` -- and restates neither.  The same for both topologies."""
    return bus_address_headers(system("per_view").xbar, system="mm_fir")


def workspace(topology: str, work_dir, probes: bool = False) -> Path:
    """The XSI workspace directory for *topology* under *work_dir*."""
    return Path(work_dir).resolve() / f"mm_fir_{topology}{'_probes' if probes else ''}"


def scenario_path(topology: str, work_dir, probes: bool = False) -> Path:
    """The scenario bundle both hosts run -- written into the workspace by :func:`run_xsi`."""
    return workspace(topology, work_dir, probes) / "scenario"


def trace_dir(topology: str, work_dir, probes: bool = False) -> Path:
    """Where the C++ host dumps its endpoints' traces (one bundle per endpoint)."""
    return workspace(topology, work_dir, probes) / "traces"


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


def run_xsi(topology: str, work_dir, timeout: int = 3600, probes: bool = False) -> str:
    """Generate the crossbar, the top and the testbench, and run XSI.  Returns the host's report
    followed by :func:`trace_report`.

    Needs Vivado (``create_ip`` + xsim) and the kernel's RTL (``python -m examples.mm_fir.mm_fir_build``).
    """
    if not RTL.is_dir():
        raise FileNotFoundError(f"no csynth RTL at {RTL}: run python -m examples.mm_fir.mm_fir_build")
    sysm = system(topology)
    spec = system_spec(topology, sysm)
    ip = generate_axi_xbar(spec.xbar, Path(work_dir).resolve() / "ip")
    ws = XsiWorkspace(workspace(topology, work_dir, probes), top=spec.top)
    host = sysm.host
    host.scenario = scenario_path(topology, work_dir, probes).as_posix()
    host.trace_dir = trace_dir(topology, work_dir, probes).as_posix()
    host.write_scenario(host.scenario)
    shutil.rmtree(host.trace_dir, ignore_errors=True)        # a stale trace would describe another run
    prb = timing_probes(sysm) if probes else None
    tb = system_tb_spec(spec, sysm.xbar, [host], probes=list(prb or ()))
    main, tb_files = render_system_tb(spec, tb)
    ws.prepare(rtl_files=ip.sim_files + leaf_sources() + sorted(RTL.glob("*.v")) + [f"{spec.top}.v"],
               include_dirs=ip.include_dirs, tb_name="mm_fir_tb", tb_cpp=main,
               extra_files={f"{spec.top}.v": render_system_top(spec, prb or None),
                            **tb_files, **address_headers()})
    return ws.run(timeout=timeout) + trace_report(host.trace_dir)
