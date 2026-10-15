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
    for port, ep in _boundary_eps(dut):
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
    unknown = set(stim) - {p for p, ep in _boundary_eps(dut) if _is_input(ep)}
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


__all__ = ["harness"]
