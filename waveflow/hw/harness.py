"""harness.py — a module run alone: the harness of one ``SynthUnit`` (``plans/unit_harness.md``).

A harness is not a class: it is an :class:`~waveflow.hw.hw_system.HwSystem` (D2, D12) whose DUT is one
module and whose testbench is stand-ins at that module's boundary.  :func:`harness` builds it:

    sysm = harness(PolyKernel, workload="test")            # or harness(some_kernel_instance)
    sysm.run()                                             # pysim
    add_system_steps(dag, sysm, ...)                       # RTL: csynth, XSI, compare

* **The DUT is a fresh copy** of the module -- its class and its build parameters (``HwParam`` fields),
  on a new :class:`~waveflow.simulation.simulation.Simulation` -- so a module inside a larger system is
  harnessed without disturbing it (D12: a system's DUT is a root).
* **The testbench is generated from the boundary, by port type**: a stream input gets a
  :class:`~waveflow.simulation.stream_tb.StreamDriver` playing ``vectors/<port>``, a stream output a
  :class:`~waveflow.simulation.stream_tb.StreamSink` recording ``vectors/<port>`` -- generic stand-ins
  (D3) that lower to the RTL testbench through their own ``bfm_model()``.  Other port kinds (an AXI-MM
  slave group, ``m_axi``, an interrupt) are the next stage's.
* **The stimulus is the module's**: ``module.workload(kind)`` returns ``{input port: bursts}`` (each burst
  a :class:`~waveflow.utils.burst_io.StreamBurst`); ``kind`` names the scenario (``"test"``, ``"calib"``).
  A module that declares timed phases and a ``calib_holdout`` gets a calibration workload for free
  (:meth:`HwSystem.calibration_workload`, D10).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from waveflow.hw.hw_system import HwSystem, _boundary_eps


def _fresh_module(module, sim):
    """A copy of *module* (an instance or a class) on *sim*: its class, its name and its build
    parameters -- the same rebuild ``system_top`` makes for csynth."""
    from waveflow.hw.clock import Clock
    from waveflow.hw.hw_module import _hw_param_names

    cls = module if isinstance(module, type) else type(module)
    kw: dict[str, Any] = {}
    if not isinstance(module, type):
        kw = {n: getattr(module, n) for n in sorted(_hw_param_names(cls))}
        clk = getattr(module, "clk", None)
        if clk is not None and "clk" in getattr(cls, "__dataclass_fields__", {}):
            kw["clk"] = Clock(freq=clk.freq)
    name = (getattr(cls, "cpp_kernel_name", None)
            or (module.name if not isinstance(module, type) else cls.__name__.lower()))
    return cls(name=name, sim=sim, **kw)


def _is_input(ep) -> bool:
    from waveflow.hw.interface import QueuedTransferIFSlave

    return isinstance(ep, QueuedTransferIFSlave)


def _credit_stand_ins(dut, sim, clk, stand_ins: dict, inputs_of: set) -> set:
    """A :class:`~waveflow.simulation.credit_tb.CreditSink` for each credit stream *dut* produces into,
    a :class:`~waveflow.simulation.credit_tb.CreditSource` for each it consumes, each on a
    ``CreditStreamIF`` at the window the module declares (``credit_links``: ``{port: {"depth": ..,
    "crd_every": ..}}``) -- a producer has its consumer's depth compiled in, so the harness cannot
    guess it.  Returns the ids of the credit streams' halves (the boundary lists them as ports)."""
    from waveflow.hw.reverse_stream import CreditStreamIF, CreditStreamMasterIF, CreditStreamSlaveIF
    from waveflow.simulation.credit_tb import CreditSink, CreditSource

    links = getattr(dut, "credit_links", {}) or {}
    halves: set = set()
    for attr, ep in list(vars(dut).items()):
        if not isinstance(ep, (CreditStreamMasterIF, CreditStreamSlaveIF)):
            continue
        cfg = links.get(attr)
        if cfg is None:
            raise ValueError(f"harness: '{dut.name}' port '{attr}' is a credit stream; its class must "
                             f"state the window its RTL was built with: credit_links = "
                             f"{{{attr!r}: {{'depth': .., 'crd_every': ..}}}}")
        if isinstance(ep, CreditStreamMasterIF):
            si = CreditSink(name=f"{attr}_sink", sim=sim, bitwidth=ep.bitwidth,
                            crd_every=int(cfg["crd_every"]), out_bundle=f"vectors/{attr}")
            master, slave = ep, si.stream_ep
        else:
            si = CreditSource(name=f"{attr}_source", sim=sim, bitwidth=ep.bitwidth,
                              in_bundle=f"vectors/{attr}")
            master, slave = si.stream_ep, ep
            inputs_of.add(attr)
        link = CreditStreamIF(name=f"{attr}_if", sim=sim, clk=clk, bitwidth=ep.bitwidth,
                              depth=int(cfg["depth"]))
        link.bind("master", master)
        link.bind("slave", slave)
        stand_ins[attr] = si
        halves |= {id(ep.fwd_ep), id(ep.crd_ep)}
    return halves


def harness(module, *, workload="test", n_cycles: int = 4000) -> HwSystem:
    """The harness of *module* (a module instance, or its class): an ``HwSystem`` whose DUT is a fresh
    copy of it and whose testbench is a driver per stream input and a sink per stream output.
    *workload* is a kind the module names (``module.workload(kind)``) or the stimulus itself,
    ``{input port: bursts}`` (a one-off scenario).  *n_cycles* bounds the RTL run."""
    from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
    from waveflow.simulation.simulation import Simulation
    from waveflow.simulation.stream_tb import StreamDriver, StreamSink
    from waveflow.utils.burst_io import write_burst_bundle

    sim = Simulation()
    dut = _fresh_module(module, sim)
    if isinstance(workload, str):
        stim = dut.workload(workload) if hasattr(dut, "workload") else {}
    else:
        stim = dict(workload)
    clk = dut.clk
    stand_ins: dict[str, Any] = {}
    inputs_of = {p for p, ep in _boundary_eps(dut) if _is_input(ep)}
    halves = _credit_stand_ins(dut, sim, clk, stand_ins, inputs_of)
    for port, ep in _boundary_eps(dut):
        if id(ep) in halves:            # one half of a credit stream: its stand-in is in place
            continue
        if not isinstance(ep, (StreamIFSlave, StreamIFMaster)):
            raise NotImplementedError(f"harness: '{dut.name}' port '{port}' is a "
                                      f"{type(ep).__name__} -- only stream ports have a stand-in yet")
        tlast = bool(getattr(type(ep), "boundary_tlast", False) or getattr(ep, "has_tlast", False))
        if _is_input(ep):
            si = StreamDriver(name=f"{port}_driver", sim=sim, bitwidth=ep.bitwidth,
                              in_bundle=f"vectors/{port}", has_tlast=tlast)
            pair = (si.stream_ep, ep)
        else:
            si = StreamSink(name=f"{port}_sink", sim=sim, bitwidth=ep.bitwidth,
                            out_bundle=f"vectors/{port}", has_tlast=tlast)
            pair = (ep, si.stream_ep)
        link = StreamIF(name=f"{port}_if", sim=sim, clk=clk, bitwidth=ep.bitwidth)
        link.bind(ep_name="master", endpoint=pair[0])
        link.bind(ep_name="slave", endpoint=pair[1])
        stand_ins[port] = si
    unknown = set(stim) - inputs_of
    if unknown:
        raise ValueError(f"harness: '{dut.name}'.workload({workload!r}) drives ports it has no input "
                         f"of: {sorted(unknown)}")

    def inputs(root: Path) -> None:
        for port, bursts in stim.items():
            write_burst_bundle([b.words for b in bursts], Path(root) / "vectors" / port,
                               tlast=[b.tlast for b in bursts])

    def calibration():
        holdout = getattr(dut, "calib_holdout", None)
        if holdout is None or not getattr(dut, "timing_phases", None):
            return None
        from waveflow.build.phase_calib import CalibWorkload

        return CalibWorkload(harness(module, workload="calib", n_cycles=n_cycles),
                             holdout=dict(holdout))

    sysm = HwSystem(sim, dut, n_cycles=n_cycles, inputs=inputs,
                    fresh=lambda: harness(module, workload=workload, n_cycles=n_cycles),
                    calibration=calibration)
    sysm.stand_ins = stand_ins
    return sysm


def memory_harness(make, script, *, n_cycles: int = 4000, name: str = "mem_unit",
                   fresh=None) -> HwSystem:
    """The harness of an on-chip memory (``plans/unit_harness.md`` Stage 4c): the memory *make(sim)*
    builds, its ``s_mm`` slave behind a direct link (no crossbar: at RTL the master group is wired
    straight to the memory's slot), and an :class:`~waveflow.simulation.mm_tb.MmScript` master playing
    *script* -- a list of ops (:func:`~waveflow.simulation.mm_tb.script_op`) at byte addresses from 0.
    The DUT is the memory and its link, as a device's own harness is (D9); the script master is its
    host.  Its read data is its trace (``rdata``, compared RTL against pysim); each op's start and end
    cycles go beside the traces (:func:`~waveflow.simulation.mm_tb.timing_dir`), the measurement a
    calibration of the memory fits.  *fresh* rebuilds the system (default: the same
    call)."""
    from waveflow.hw.hw_system import DutTop
    from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
    from waveflow.simulation.mm_tb import MmScript
    from waveflow.simulation.simulation import Simulation
    from waveflow.utils.burst_io import write_burst_bundle

    sim = Simulation()
    mem = make(sim)
    bpw = int(mem.word_size) // 8
    # The script addresses the whole memory: one region from 0.
    if mem.alloc(int(mem.nwords_tot)) != 0:
        raise ValueError(f"memory_harness: '{mem.name}' is already allocated; build it fresh")
    master = MmScript(name="script", sim=sim, bitwidth=int(mem.word_size), clk=mem.clk)
    link = AXIMMCrossBarIF(name="link", sim=sim, clk=mem.clk, nports_master=1, nports_slave=1,
                           bitwidth=int(mem.word_size), latency_init=0.0, latency_travel=0.0)
    link.direct = True
    link.bind("master_0", master.m)
    link.bind("slave_0", mem.s_mm)
    assign_address_ranges([mem.s_mm], [(0, int(mem.nwords_tot) * bpw)])
    dut = DutTop.of(name, sim, comps=[mem], ifs=[link])
    ops = [np.asarray(o, dtype=np.uint64) for o in script]

    def inputs(root: Path) -> None:
        write_burst_bundle(ops, Path(root) / "vectors" / "script")

    sysm = HwSystem(sim, dut, n_cycles=n_cycles, inputs=inputs,
                    fresh=fresh or (lambda: memory_harness(make, script, n_cycles=n_cycles, name=name)))
    sysm.stand_ins = {"s_mm": master}
    sysm.script = ops
    master.script = ops
    return sysm


__all__ = ["harness", "memory_harness"]
