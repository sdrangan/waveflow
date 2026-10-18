"""hw_system.py — the :class:`HwSystem` base class: a design and its environment, as one object.

``plans/system_model.md`` S0.  Every system flow -- XSI simulation (``run_system_xsi``,
``add_system_steps``), the system top (``SystemTopSpec.from_system``), synthesis and the resource
estimate -- takes a system object.  This class states what such an object provides, so an author reads
the contract here instead of finding it in the consumers:

* ``sim`` -- the :class:`~waveflow.simulation.simulation.Simulation` holding every module and interface
  of the graph.  The subclass builds the graph (typically in ``__post_init__``) and sets it;
* :meth:`dut` -- the **default DUT**: a :class:`DutTop`, the root of the tree of modules synthesized
  into the design -- each device (a kernel with its views and the bus writers acting for it), each
  on-chip memory -- with the interfaces between them (the crossbar) as its internal interfaces.
  Everything else on the graph is the testbench.  A build may draw another cut (``inside=``); this is
  the one it takes otherwise.  DUT and testbench are roles a cut assigns, not kinds of module
  (``plans/design_cut.md``);
* :meth:`run` -- simulate the whole graph in pysim.

And the **testbench contract** (``plans/system_model.md`` S3b), what lets the system flow verify any
testbench, not only a software host:

* :meth:`write_scenario` -- write every testbench participant's inputs under a root, and anchor them
  there.  The default is the host's (``SwHost.write_scenario``); a system with no host writes its own.
* :meth:`recordings` -- the bundles testbench participants record under that root: each sink's
  ``out_bundle``, each memory's ``dump_segs``.  Derived from the participants' own declarations, so a
  system states nothing; the flow compares each one, RTL against pysim -- unless the participant says
  otherwise: ``recording_check = "example"`` (an attribute, default ``"exact"``) marks a recording
  pysim cannot match by construction (an RF sink, where pysim cannot see sub-block loss), which the
  example's own check reads instead (:meth:`compared_recordings`).
* :meth:`clock` -- the clock a run's length is counted in.

A system is not an :class:`~waveflow.hw.hw_module.HwModule`: it is never synthesized as a whole, and it
holds software (the host) and testbench models as well as hardware.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from waveflow.hw.hw_module import HwModule

if TYPE_CHECKING:
    from waveflow.simulation.simobj import SimObj
    from waveflow.simulation.simulation import Simulation


@dataclass
class DutTop(HwModule):
    """The root of a system's DUT: a composite whose children are its devices and on-chip memories,
    and whose internal interfaces are the links between them (the crossbar, a routed stream).

    It is structure, not behaviour: its children simulate, and it adds no process.  Build it from the
    parts::

        DutTop.of("markov_dut", sim, comps=[gen_dev, chain_dev, mem], ifs=[xbar, u_link])
    """

    @classmethod
    def of(cls, name: str, sim, *, comps=(), ifs=()) -> "DutTop":
        top = cls(name=name, sim=sim)
        for c in comps:
            top.add_comp(c)
        for i in ifs:
            top.add_if(i)
        return top


def dut_members(root: HwModule) -> list:
    """The cut a DUT tree stands for, as the list the system top is walked from: each device's
    kernel, and each other child (an on-chip memory) itself, in the tree's order.  A DUT that is
    itself a member -- a csynth'd kernel, a memory, or one device (a harness's DUT, D12) -- is its own
    cut."""
    from waveflow.hw.memory import MemoryMod
    from waveflow.hw.mm_device import MmSlaveDevice

    if isinstance(root, MmSlaveDevice):
        return [root.kernel]
    if getattr(type(root), "cpp_kernel_name", None) or isinstance(root, MemoryMod):
        return [root]
    return [c.kernel if isinstance(c, MmSlaveDevice) else c for c in root.sub_comps.values()]


class HwSystem(ABC):
    """A design and its environment: a :class:`~waveflow.simulation.simulation.Simulation` plus one
    named DUT (``plans/unit_harness.md`` D12).  Two ways to make one:

    * **Directly** -- build the modules on a simulation, then name the DUT; the testbench is everything
      else, derived (:meth:`participants`), never listed::

          sim = Simulation()
          dut = MarkovDut(name="markov_dut", sim=sim)
          host = MarkovHost(name="host", sim=sim, jobs=jobs)
          host.bind(dut.s_axi)
          sysm = HwSystem(sim, dut)

      The constructor checks the D12 rules: the DUT is registered in the simulation, it is a root, every
      testbench participant can be lowered to the RTL testbench, and no DUT boundary port is unbound.
      :func:`~waveflow.hw.harness.harness` makes one this way around a single module.
    * **By subclassing** (the systems written before D12): build the graph on ``self.sim`` and implement
      :meth:`dut` and :meth:`run`::

          @dataclass
          class MySystem(HwSystem):
              jobs: list

              def __post_init__(self):
                  self.sim = Simulation()
                  ...                              # the kernels, memory, host, crossbar, wiring

              def dut(self):
                  return self.dut_top                  # DutTop.of("my_dut", ...), built above

              def run(self):
                  self.sim.run_sim(until=self.host.done)
                  return self.host.results
    """

    #: The simulation holding every module and interface of the graph.
    sim: "Simulation"

    def __init__(self, sim: "Simulation", dut: HwModule, *, n_cycles: int | None = None,
                 inputs=None, check=None, run_until=None, fresh=None, calibration=None) -> None:
        """*sim* holds every module; *dut* is the one root module synthesized.  The rest describes the
        run: *n_cycles* bounds the RTL run (``vectors/run.json``), *inputs* ``(root) -> None`` writes
        what the testbench plays, *check* ``(system) -> Any`` is what :meth:`run` returns, *run_until*
        ends a pysim run that never runs out of events, *fresh* ``() -> HwSystem`` rebuilds an
        unrun copy (:meth:`fresh`), *calibration* ``() -> CalibWorkload`` is
        :meth:`calibration_workload`."""
        self.sim = sim
        self._dut = dut
        self.n_cycles = n_cycles
        self._inputs = inputs
        self._check = check
        self.run_until = run_until
        self._fresh = fresh
        self._calibration = calibration
        self._root = None
        self.clk = getattr(dut, "clk", None)
        widths = [int(ep.bitwidth) for _n, ep in _boundary_eps(dut) if getattr(ep, "bitwidth", None)]
        if widths:
            self.data_width = max(widths)
        self._validate()

    def _validate(self) -> None:
        """The D12 rules, each a refusal naming what to fix."""
        dut = self._dut
        if not any(o is dut for o in self.sim._sim_objs):
            raise ValueError(f"HwSystem: the DUT '{dut.name}' is not a SimObj of this simulation -- "
                             f"build it with sim=<the system's simulation>")
        if getattr(dut, "parent", None) is not None:
            raise ValueError(f"HwSystem: the DUT '{dut.name}' is a sub-module of "
                             f"'{dut.parent.name}' -- a system's DUT is a root; a sub-module's own "
                             f"harness gets a fresh copy of it (waveflow.hw.harness.harness)")
        bad = [o.name for o in self.participants() if not _lowerable(o)]
        if bad:
            raise ValueError(f"HwSystem: testbench participants with no testbench lowering (a "
                             f"bfm_model(), an xsi_model() or a C++ twin): {bad} -- an object that "
                             f"cannot be lowered belongs in a plain Simulation, not an HwSystem")
        loose = [n for n, ep in _boundary_eps(dut) if getattr(ep, "interface", None) is None]
        if loose:
            raise ValueError(f"HwSystem: DUT '{dut.name}' boundary ports bound to nothing: {loose}")

    def dut(self) -> HwModule:
        """The DUT: the root of the synthesized tree (for a subclass, a :class:`DutTop` whose children
        are the devices and on-chip memories, in the order their sub-tops are rendered)."""
        dut = getattr(self, "_dut", None)
        if dut is None:
            raise NotImplementedError(f"{type(self).__name__}: implement dut() or construct with "
                                      f"HwSystem(sim, dut)")
        return dut

    def participants(self) -> "list[SimObj]":
        """The testbench, derived: every root ``SimObj`` of the simulation outside the DUT -- each a
        testbench participant (an interface is an edge, not a participant)."""
        from waveflow.hw.interface import Interface, InterfaceEndpoint

        root = self.dut()
        return [o for o in self.sim._sim_objs
                if getattr(o, "parent", None) is None and o is not root
                and not isinstance(o, (Interface, InterfaceEndpoint))]

    def fresh(self) -> "HwSystem":
        """An unrun copy of this system -- what a second pysim pass (a calibration's) runs."""
        import dataclasses

        if getattr(self, "_fresh", None) is not None:
            return self._fresh()
        if dataclasses.is_dataclass(self):
            return dataclasses.replace(self)
        raise NotImplementedError(f"{type(self).__name__}: no way to rebuild it -- pass fresh=")

    def run(self) -> Any:
        """Simulate the whole graph in pysim; return *check*'s value (or the system)."""
        import tempfile

        if self._root is None:
            with tempfile.TemporaryDirectory() as tmp:
                self.write_scenario(tmp)
                self.sim.run_sim(until=self.run_until)
        else:
            self.sim.run_sim(until=self.run_until)
        return self._check(self) if self._check is not None else self

    def write_scenario(self, root) -> None:
        """Write the scenario -- every testbench participant's inputs -- under *root*, and anchor the
        participants there, so a pysim run and an RTL run started from *root* read the same bytes.

        The default: a system built directly writes its *inputs* and the run's bound
        (``vectors/run.json``) and anchors every participant that reads or records a bundle at *root*;
        otherwise the software host's (``SwHost.write_scenario``).  A subclass with no host overrides
        this: the scenario is the example's (what commands, what memory contents)."""
        from pathlib import Path

        from waveflow.build.system_xsi import discover

        if getattr(self, "_inputs", None) is not None:
            import json

            from waveflow.utils.burst_io import write_run_params

            root = Path(root)
            run_json = root / "vectors" / "run.json"
            run_json.unlink(missing_ok=True)      # a previous scenario's params are not this one's
            self._inputs(root)
            if self.n_cycles is not None:
                # The inputs may write run params of their own (a memory's arena); the bound joins them.
                params = (json.loads(run_json.read_text(encoding="utf-8")) if run_json.is_file()
                          else {})
                write_run_params(root / "vectors", **{**params, "n_cycles": int(self.n_cycles)})
            top = self.dut()
            for o in self.elements():
                if not _inside(o, top) and any(getattr(o, a, None) for a in (
                        "in_bundle", "out_bundle", "load_segs", "dump_segs")):
                    o.root = root
            self._root = root
            return
        host = discover(self)[1]
        if host is None:                # no bus to find it by: the one participant that writes one
            hosts = [o for o in self.participants() if callable(getattr(o, "write_scenario", None))]
            host = hosts[0] if len(hosts) == 1 else None
        if host is None:
            raise NotImplementedError(f"{type(self).__name__} has no software host to write its "
                                      f"scenario: override write_scenario(root)")
        host.scenario = Path(root).as_posix()
        host.write_scenario(root)

    def calibration_workload(self):
        """The workload that calibrates this design's declared timed phases, or None (the default:
        nothing to calibrate) -- a :class:`~waveflow.build.phase_calib.CalibWorkload`: a system of
        the same design whose scenario produces the feature values to fit on, and the values held
        out to test the fit.  The one hand-written piece of a calibration
        (:func:`~waveflow.build.phase_calib.add_calib_steps` generates the rest)."""
        make = getattr(self, "_calibration", None)
        return make() if make is not None else None

    def recordings(self) -> tuple[str, ...]:
        """The bundles the testbench records, relative to the scenario root: each participant
        outside the DUT that declares an ``out_bundle`` (a sink) or ``dump_segs`` (a memory).  The
        system flow compares each, RTL against pysim.  A software host's traces are not here: they go
        to its own trace directory and are compared there."""
        root = self.dut()

        def in_dut(mod) -> bool:
            while mod is not None:
                if mod is root:
                    return True
                mod = getattr(mod, "parent", None)
            return False

        out: list[str] = []
        counts = False
        for o in self.elements():
            if in_dut(o):
                continue
            if getattr(o, "out_bundle", ""):
                out.append(str(o.out_bundle))
            out += [str(seg.bundle) for seg in (getattr(o, "dump_segs", None) or ())]
            counts = counts or _reports_counters(o)
        if counts:
            from waveflow.build.system_top import COUNTERS_DIR

            out.append(COUNTERS_DIR)
        return tuple(dict.fromkeys(out))

    def acceptances(self) -> tuple[str, ...]:
        """The bundles the testbench's drivers record **acceptance** into, relative to the scenario
        root: each participant outside the DUT that declares an ``accept_bundle`` (a
        :class:`~waveflow.simulation.stream_tb.StreamDriver`; a harness sets one per stream input).
        Only the RTL run writes them -- each word the DUT accepted with its cycle, the input-side
        twin of a sink's arrival cycles -- in its run directory (``<work>/xsi_run/<bundle>``); pysim's
        times are the simulation's own, so they are not compared."""
        root = self.dut()
        return tuple(dict.fromkeys(str(o.accept_bundle) for o in self.elements()
                                   if not _inside(o, root) and getattr(o, "accept_bundle", "")))

    def compared_recordings(self) -> tuple[str, ...]:
        """The recordings compared **exactly**, RTL against pysim: every :meth:`recordings` entry
        whose participant has not set ``recording_check = "example"``.  The rest are still gathered
        from both runs, for the example's own check."""
        root = self.dut()

        def in_dut(mod) -> bool:
            while mod is not None:
                if mod is root:
                    return True
                mod = getattr(mod, "parent", None)
            return False

        from waveflow.build.system_top import COUNTERS_DIR

        # Model counters are each backend's own bookkeeping, in its own units (an RTL converter
        # counts words, pysim's edge counts blocks): never compared exactly, always the example's.
        skip: set[str] = {COUNTERS_DIR}
        for o in self.elements():
            if in_dut(o) or getattr(o, "recording_check", "exact") == "exact":
                continue
            if getattr(o, "out_bundle", ""):
                skip.add(str(o.out_bundle))
            skip |= {str(seg.bundle) for seg in (getattr(o, "dump_segs", None) or ())}
        return tuple(r for r in self.recordings() if r not in skip)

    def clock(self):
        """The clock a run's length is counted in: the host's, else the system's ``clk``."""
        from waveflow.build.system_xsi import discover

        host = discover(self)[1]
        return host.clk if host is not None else getattr(self, "clk", None)

    def cut(self) -> list:
        """The default cut as a list -- each device's kernel and each memory (:func:`dut_members`):
        what ``inside=`` takes, and what a build uses when it is not given one."""
        cut = dut_members(self.dut())
        if not cut:
            raise ValueError(f"{type(self).__name__}: the DUT '{self.dut().name}' puts nothing in the "
                             f"cut -- no kernel, memory or device; an RTL run of it would be vacuous")
        return cut

    def elements(self) -> "list[SimObj]":
        """Every module and interface of the graph, in the order they were created."""
        return list(self.sim._sim_objs)


class GraphSystem(HwSystem):
    """A system built around one **graph** -- a composite holding the design and its testbench
    participants -- with every derivable part of the contract derived (``plans/system_model.md``
    S3f).  A subclass states only what is its own::

        @dataclass
        class StateAccumSystem(GraphSystem):
            nvec: int = 5

            def graph(self):
                return StateAccumTB(name="xsi_tb", sim=Simulation(), nvec=self.nvec)

            def dut_modules(self, g):
                return [g.dut]

            def write_inputs(self, root):
                write_scenario(root, self.nvec)

    and, optionally, :meth:`check` (the golden on a pysim run).  The defaults:

    * :meth:`dut` -- a :class:`DutTop` of :meth:`dut_modules`, named for the first one's csynth'd
      module (or the class's ``dut_name``);
    * ``clk`` -- the first DUT module's clock; ``data_width`` -- the widest of the DUT's boundary
      ports (the width of the top's stream ports and ``m_axi`` groups: there is no crossbar to take
      it from);
    * :meth:`write_scenario` -- :meth:`write_inputs`, then the run's bound (``vectors/run.json``,
      :attr:`n_cycles`, beside any run params the inputs wrote), then every testbench participant that reads or records a bundle anchored at
      the root, so pysim records what the RTL records;
    * :meth:`run` -- pysim, to the graph's ``run_until`` when it has one (a converter never runs out
      of events), then :meth:`check`.

    A recording pysim cannot match by construction is marked on its participant in :meth:`graph`
    (``recording_check = "example"``)."""

    def __post_init__(self) -> None:
        self.tb = self.graph()
        self.sim = self.tb.sim
        mods = list(self.dut_modules(self.tb))
        first = mods[0]
        from waveflow.hw.hw_module import kernel_name

        name = kernel_name(first) or first.name
        self.dut_top = DutTop.of(getattr(self, "dut_name", None) or f"{name}_dut", self.sim,
                                 comps=mods)
        self.clk = first.clk
        self.data_width = max(_boundary_widths(mods))
        self._root = None

    # -- what a subclass states ------------------------------------------------------------------

    @abstractmethod
    def graph(self) -> HwModule:
        """Build and return the graph: the design and its testbench participants, wired."""

    @abstractmethod
    def dut_modules(self, graph) -> list:
        """The modules of *graph* inside the default cut."""

    def write_inputs(self, root) -> None:
        """Write the scenario's inputs (what the drivers and sources play) under *root*."""

    def check(self) -> Any:
        """The golden on a finished pysim run; the default returns the graph."""
        return self.tb

    # -- derived ---------------------------------------------------------------------------------

    @property
    def n_cycles(self) -> int:
        """The RTL run's cycle bound: the graph's ``n_cycles``."""
        return int(self.tb.n_cycles)

    def dut(self) -> DutTop:
        return self.dut_top

    def write_scenario(self, root) -> None:
        import json
        from pathlib import Path

        from waveflow.utils.burst_io import write_run_params

        root = Path(root)
        run_json = root / "vectors" / "run.json"
        run_json.unlink(missing_ok=True)          # a previous scenario's bound is not this one's
        self.write_inputs(root)
        # The inputs may write run params of their own (a memory's arena size); the bound joins them.
        params = json.loads(run_json.read_text(encoding="utf-8")) if run_json.is_file() else {}
        write_run_params(root / "vectors", **{"n_cycles": self.n_cycles, **params})
        top = self.dut()
        for o in self.elements():
            if _inside(o, top):
                continue
            if any(getattr(o, a, None) for a in ("in_bundle", "out_bundle", "load_segs",
                                                  "dump_segs")):
                o.root = root
        self._root = root

    def run(self) -> Any:
        import tempfile

        until = getattr(self.tb, "run_until", None)
        if self._root is None:
            with tempfile.TemporaryDirectory() as tmp:
                self.write_scenario(tmp)
                self.sim.run_sim(until=until)
        else:
            self.sim.run_sim(until=until)
        return self.check()


def _inside(mod, root) -> bool:
    """Is *mod* in the tree under *root*?"""
    while mod is not None:
        if mod is root:
            return True
        mod = getattr(mod, "parent", None)
    return False


def _boundary_eps(mod) -> list:
    """``[(name, endpoint), ...]`` of *mod*'s boundary."""
    from waveflow.build.composite_gen import _unpack_boundary

    return [_unpack_boundary(e) for e in getattr(mod, "boundary", ())]


def _lowerable(obj) -> bool:
    """Can testbench participant *obj* be lowered to the RTL testbench: a ``bfm_model()``, an
    ``xsi_model()``, or a C++ twin (a software host)?"""
    from waveflow.hw.hw_module import declares_hook

    if declares_hook(obj, "bfm_model") or declares_hook(obj, "xsi_model"):
        return True
    from waveflow.sw.threads import SwHost

    return isinstance(obj, SwHost)


def _boundary_widths(mods) -> list[int]:
    """The bit widths of *mods*' boundary endpoints."""
    from waveflow.build.composite_gen import _unpack_boundary

    out = [int(ep.bitwidth) for m in mods for _n, ep in (_unpack_boundary(e) for e in m.boundary)
           if getattr(ep, "bitwidth", None)]
    if not out:
        raise ValueError(f"no DUT boundary port of {[m.name for m in mods]} states a bit width")
    return out


def _reports_counters(obj) -> bool:
    """Does testbench element *obj* -- a participant, or an interface realized as a channel --
    declare a C++ model that keeps counters (``BfmModel`` / ``ChannelModel.reports_counters``)?"""
    from waveflow.hw.hw_module import declares_hook

    try:
        if declares_hook(obj, "bfm_model"):
            from waveflow.build.composite_gen import bfm_models

            return any(bm.reports_counters for bm in bfm_models(obj))
        if declares_hook(obj, "xsi_model"):
            return bool(getattr(obj.xsi_model(), "reports_counters", False))
    except Exception:                  # an element whose model cannot be resolved records nothing
        return False
    return False


__all__ = ["DutTop", "GraphSystem", "HwSystem", "dut_members"]
