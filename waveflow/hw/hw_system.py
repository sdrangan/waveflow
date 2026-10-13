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
    kernel, and each other child (an on-chip memory) itself, in the tree's order."""
    from waveflow.hw.mm_device import MmSlaveDevice

    return [c.kernel if isinstance(c, MmSlaveDevice) else c for c in root.sub_comps.values()]


class HwSystem(ABC):
    """A design and its environment: the element graph, simulated by :meth:`run`, cut per build.

    A subclass builds the graph on ``self.sim`` and implements :meth:`dut` and :meth:`run`::

        @dataclass
        class MarkovSystem(HwSystem):
            jobs: list

            def __post_init__(self):
                self.sim = Simulation()
                ...                              # the kernels, memory, host, crossbar, wiring

            def dut(self):
                return self.dut_top                  # DutTop.of("markov_dut", ...), built above

            def run(self):
                self.sim.run_sim(until=self.host.done)
                return self.host.results
    """

    #: The simulation holding every module and interface of the graph; set by the subclass.
    sim: "Simulation"

    @abstractmethod
    def dut(self) -> HwModule:
        """The default DUT: the root of the synthesized tree (:class:`DutTop`), its children the
        devices and on-chip memories in the order their sub-tops are rendered."""

    @abstractmethod
    def run(self) -> Any:
        """Simulate the whole graph in pysim and return what the system reports."""

    def write_scenario(self, root) -> None:
        """Write the scenario -- every testbench participant's inputs -- under *root*, and anchor the
        participants there, so a pysim run and an RTL run started from *root* read the same bytes.

        The default is the software host's (``SwHost.write_scenario``).  A system with no host
        overrides this: the scenario is the example's (what commands, what memory contents)."""
        from pathlib import Path

        from waveflow.build.system_xsi import discover

        host = discover(self)[1]
        if host is None:
            raise NotImplementedError(f"{type(self).__name__} has no software host to write its "
                                      f"scenario: override write_scenario(root)")
        host.scenario = Path(root).as_posix()
        host.write_scenario(root)

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
        return dut_members(self.dut())

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
        name = getattr(type(first), "cpp_kernel_name", None) or first.name
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
