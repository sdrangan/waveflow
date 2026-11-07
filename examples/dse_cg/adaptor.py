"""adaptor.py -- DseAdaptor, the base class a design subclasses to be explored.

The brief generator (``brief.py``), the service (``service.py``) and the scripted policies
(``policies.py``) see a design only through a ``DseAdaptor``; nothing else in this directory knows
which design it is exploring.  A subclass implements what only it can know -- its space, its prose,
how to build its system, its golden model -- and inherits the rest: defaults, legality, the
architecture (the sub-top tree and its interconnects, the same at every point), each sub-top's
identity at a point, its synthesis, and which sub-tops each parameter rebuilds.

The LTBF system's adaptor is ``ltbf_cg_mem.py`` (V1 in ``plans/dse_cg.md``).  This file knows nothing
about CG; it moves into the ``waveflow`` package once a second design uses it.

What is provided follows from :meth:`space` and the built system alone (legality, the architecture
and its check, which parts each parameter rebuilds); what drives the tools -- the identities, pysim,
synthesis, csynth and the RTL run -- is the subclass's, because it runs the design's own build flow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class DseAdaptor(ABC):
    """One design, as DSE sees it.  Subclass it and implement the abstract members.

    ======================  ==================================================================
    implement               what
    ======================  ==================================================================
    ``name``, ``platform``  the design's name; the 2025.1 calibration platform its models use
    :meth:`space`           parameters, constraints, workloads, as data
    :meth:`context`         the hand-written prose of ``DSE.md``
    :meth:`build_system`    the ``HwSystem`` at a design point
    :meth:`golden`          the untimed bit-exact simulation
    ======================  ==================================================================

    ======================  ==================================================================
    implement for the tools  what (each runs the design's own build flow)
    ======================  ==================================================================
    :meth:`identities`      each built node's identity at a point: the service's cache and
                            charge key
    :meth:`hls_identities`  each HLS kernel's identity at a point: the same, for ``hls_estimate``
    :meth:`sim_identity`    an RTL simulation's identity: the same, for ``rtlsim``
    :meth:`pysim`           the system in pysim, resources from the calibrated models
    :meth:`synth`           one node of the architecture out of context (the per-device route)
    :meth:`rtlsim`          the DUT at RTL (the incremental XSI flow)
    :meth:`hls_estimate`    one HLS kernel C-synthesized, its report parsed (the ``csynth`` step)
    ======================  ==================================================================

    ======================  ==================================================================
    provided                what
    ======================  ==================================================================
    :meth:`defaults`        every parameter's default
    :meth:`fill`            a point with its missing parameters defaulted, unknown names refused
    :meth:`legal`           the first constraint a point breaks, or ``None``
    :meth:`legal_with`      a point completed to a legal design that keeps its values
    :meth:`spec`            the DUT as synthesized (``SystemTopSpec``), its top :attr:`TOP`
    :meth:`architecture_at` the sub-top tree and its interconnects at a point, with its sizes
    :meth:`architecture`    the same at the defaults, checked to be point-invariant
    :meth:`rebuilds`        for each design parameter, the built nodes a new value rebuilds
    ======================  ==================================================================

    Class attributes: :attr:`BUILT` and :attr:`MODELLED` name the nodes that are built (synthesized
    and simulated at RTL) and modelled (simulated, never synthesized), with a description each;
    :meth:`node_of` maps a sub-top or instance name to its node.

    A design parameter may rebuild nothing: one that sets a modelled part (a memory model's speed)
    or the host's software (a scheduler setting) changes no node's identity, only
    :meth:`sim_identity`'s, so a new value costs an RTL simulation and no synthesis.

    The architecture is what DSE.md shows the agent; the identities are bookkeeping only the
    service sees.
    """

    name: ClassVar[str]
    platform: ClassVar[str]
    #: The built nodes (the system top's sub-tops by name, and ``xbar``, the crossbar and the top's
    #: glue), each with a description; the modelled ones likewise.
    BUILT: ClassVar[dict[str, str]] = {}
    MODELLED: ClassVar[dict[str, str]] = {}
    #: The system top's module name, the same at every point so a part's Verilog does not depend on
    #: where it was built.
    TOP: ClassVar[str] = "dse_top"

    # -- implement ----------------------------------------------------------------------------

    @abstractmethod
    def space(self) -> dict:
        """Parameters, constraints and workloads, as data::

            {"parameters": [{"name", "kind": "design" | "workload", "values", "default",
                             "description"}, ...],
             "constraints": [{"rule": "<Python expression over parameter names>", "why"}, ...],
             "workloads": [{"name", "fixes": {...}, "valid_for": [metric, ...],
                            "description"}, ...]}

        Every legal point of this space is measured before any scored run (the agents' space is
        the measured grid), so it must be small enough to measure; plan ``plans/dse_cg.md``.

        The shape ``dselib.space()`` returns, minus ``rebuilds``, which :meth:`rebuilds` adds.
        """

    @abstractmethod
    def context(self) -> dict:
        """The hand-written parts of ``DSE.md``: title, description (what the system does),
        data flow (how work moves through the architecture: what no walk of the tree can say),
        doc pages, workload descriptions, metrics (name, unit, description), and the modelled parts
        (each part simulated but not synthesized: its model in pysim and at RTL, and how its
        resources are counted).  Facts only: no objective, no advice."""

    @abstractmethod
    def build_system(self, params: dict, workload: Any = None, rtl: bool = False) -> Any:
        """The ``HwSystem`` at design point ``params`` (filled), playing *workload* (default: the
        design's timing workload), for the RTL flow when *rtl*: what is simulated, synthesized, and
        walked for the architecture.  Synthesis and the architecture do not depend on the workload."""

    @abstractmethod
    def golden(self, params: dict, workload: dict) -> dict:
        """Untimed bit-exact accuracy: the body of ``dselib.golden``."""

    # -- override if needed -------------------------------------------------------------------

    def pysim(self, params: dict, workload: dict) -> dict:
        """Accuracy (exact) + cycles, per-block activity, resources (calibrated): the body of
        ``dselib.pysim``.  Default: run :meth:`build_system`'s system on the workload in pysim for
        cycles and activity, and ``estimate_system`` for resources."""
        raise NotImplementedError(f"{type(self).__name__} runs its own build flow")

    def synth(self, node: str, params: dict) -> dict:
        """Synthesize node ``node`` of :meth:`architecture` (with everything under it) at
        ``params``, out of context, and return its row: ``lut``, ``ff``, ``dsp``, ``bram``, timing,
        ``status``, and the per-instance utilization report as an artifact.

        Called only by the live backend, and only for an identity the ledger does not hold yet.
        Default: the per-device route (``synth_route="per_device"``, ``waveflow.build.system_ooc``)
        on that sub-top, or the crossbar IP.
        """
        raise NotImplementedError(f"{type(self).__name__} runs its own build flow")

    def rtlsim(self, params: dict, workload: dict) -> dict:
        """Simulate the DUT at ``params`` at RTL on the workload and return its row: cycles, the
        bit-exact check against pysim, per-block activity, and the activity table as an artifact.

        Called only by the live backend.  Default: the system flow on :meth:`build_system`'s system
        (``add_system_steps`` to ``system_xsi``), incremental, so a new run on a build already
        elaborated -- a new workload, or a design parameter that rebuilds nothing -- pays for
        simulation alone (:meth:`sim_identity`).
        """
        raise NotImplementedError(f"{type(self).__name__} runs its own build flow")

    def hls_estimate(self, kernel: str, params: dict) -> dict:
        """C-synthesize HLS kernel ``kernel`` (an HLS top of :meth:`build_system`'s DUT) at
        ``params`` and return the compiler's estimates: ``lut``, ``ff``, ``dsp``, ``bram``, ``ii``,
        and ``latency`` where the report gives a number (``None`` for a free-running kernel whose
        latency the compiler cannot compute), with the report as an artifact.

        Only HLS tops have one: the hand-written adaptors and the vendor crossbar do not.  Called
        only by the live backend.  Default: the system flow's ``csynth`` step for that top and its
        report parser.  The same step runs before every synthesis, so measuring the ground truth
        records these as a by-product.
        """
        raise NotImplementedError(f"{type(self).__name__} runs its own build flow")

    # -- provided -----------------------------------------------------------------------------

    def defaults(self) -> dict:
        """Every parameter's default, design and workload."""
        return {p["name"]: p["default"] for p in self.space()["parameters"]}

    def fill(self, params: dict, kind: str = "design") -> dict:
        """``params`` with every missing ``kind`` parameter set to its default.  A name that is not
        a ``kind`` parameter is an error, not ignored: a typo would otherwise evaluate the default."""
        known = {p["name"]: p["default"] for p in self.space()["parameters"] if p["kind"] == kind}
        unknown = sorted(params.keys() - known.keys())
        if unknown:
            raise KeyError(f"not {kind} parameters of {self.name}: {unknown}")
        return {**known, **params}

    def legal(self, params: dict, workload: dict | None = None) -> str | None:
        """``None`` if the point satisfies every constraint, else the first rule it breaks.

        Constraints are evaluated over every parameter, the ones not given at their defaults, so a
        rule that mixes design and workload parameters is checked like any other.
        """
        values = {**self.defaults(), **params, **(workload or {})}
        for c in self.space()["constraints"]:
            if not eval(c["rule"], {"__builtins__": {}}, values):
                return c["rule"]
        return None

    def legal_with(self, point: dict) -> dict:
        """*point* completed to a legal design that keeps its values: itself if legal, else the
        first legal point, the other design parameters tried from their defaults outward.  For
        :meth:`rebuilds` and :meth:`architecture`, where one parameter alone can leave the legal set
        (``R`` alone leaves the systolic shapes, so ``C`` moves with it)."""
        import itertools

        if self.legal(point) is None:
            return point
        rest = [p for p in self.space()["parameters"] if p["kind"] == "design" and p["name"] not in point]
        orders = [[p["default"], *(v for v in p["values"] if v != p["default"])] for p in rest]
        for combo in itertools.product(*orders):
            cand = {**point, **{p["name"]: v for p, v in zip(rest, combo)}}
            if self.legal(cand) is None:
                return cand
        raise ValueError(f"no legal design of {self.name} keeps {point}")

    def node_of(self, name: str) -> str:
        """The node a sub-top or kernel instance belongs to: its name without ``u_`` and ``_sub``.
        Override to group several sub-tops into one node (a memory's banks)."""
        return name.removeprefix("u_").removesuffix("_sub")

    def spec(self, params: dict) -> Any:
        """The DUT as synthesized at *params* (``SystemTopSpec``), its top named :attr:`TOP` at
        every point."""
        from waveflow.build.system_top import SystemTopSpec

        return SystemTopSpec.from_system(self.build_system(params, rtl=True), top=self.TOP)

    def architecture_at(self, params: dict) -> dict:
        """The DUT at *params*, walked from :meth:`spec`: its nodes (each sub-top, grouped by
        :meth:`node_of`, and ``xbar``, with its kind, whether it is built, and its description), the
        interconnects between them (crossbar masters and slaves, stream nets that cross nodes with
        their producer first, interrupt lines, what crosses the DUT boundary), and ``sizes``: the
        memory sub-tops and the crossbar's ports, the sizes inside the nodes a parameter may
        change."""
        spec = self.spec(params)
        node, desc = self.node_of, {**self.MODELLED, **self.BUILT}
        nodes: dict = {}
        for sub in spec.subs:
            n = node(sub.name)
            nodes.setdefault(n, {"name": n, "kind": sub.kind, "built": n in self.BUILT,
                                 "description": desc.get(n, "")})
        nodes["xbar"] = {"name": "xbar", "kind": "crossbar", "built": "xbar" in self.BUILT,
                         "description": desc.get("xbar", "the AXI crossbar")}
        by_prefix = {prefix: (inst, port) for inst, port, prefix in spec.maxi_si}
        masters = sorted({(node(by_prefix[p][0]), by_prefix[p][1]) if p in by_prefix
                          else ("host", "bus (outside the DUT)") for p, _outside in spec.si})
        slaves = sorted({(node(m.group), "memory" if m.kind == "memory"
                          else ", ".join(v.name for v in m.views)) for m in spec.mi})
        ends: dict = {}
        for inst, port, net in spec.axis_nets:
            ends.setdefault(net, []).append((node(inst), port))
        streams = sorted((net, tuple(sorted(e, key=lambda x: (not x[1].startswith("m_"), x))))
                         for net, e in ends.items() if len({n for n, _p in e}) > 1)
        irqs = sorted((node(view.split("_")[0]), view) for _port, view in spec.irqs)
        hw = spec.xbar.hw_params()
        return {"nodes": [nodes[n] for n in sorted(nodes)],
                "interconnects": {"crossbar_masters": masters, "crossbar_slaves": slaves,
                                  "streams": streams, "interrupts": irqs,
                                  "boundary": ["host bus master", "interrupt lines to the host"]},
                "sizes": {"memories": sum(1 for sub in spec.subs if sub.kind == "memory"),
                          "xbar_si": hw["n_si"], "xbar_mi": hw["n_mi"]}}

    def architecture(self) -> dict:
        """The DUT as synthesized, at the defaults: :meth:`architecture_at` without its sizes.

        Parametric DSE means this is the same at every point: only the sizes inside the nodes
        change.  So it is computed once, and checked: the tree's shape at the defaults and at each
        design parameter's extreme values (completed by :meth:`legal_with`) must match, or the
        "parameter" changes the architecture and the adaptor is refused (``ValueError``).
        """
        if getattr(self, "_arch", None) is not None:
            return self._arch
        shape = {k: v for k, v in self.architecture_at({}).items() if k != "sizes"}
        for p in self.space()["parameters"]:
            if p["kind"] != "design":
                continue
            for v in (min(p["values"], key=str), max(p["values"], key=str)):
                got = self.architecture_at(self.legal_with({p["name"]: v}))
                if {k: x for k, x in got.items() if k != "sizes"} != shape:
                    raise ValueError(f"{p['name']} = {v!r} changes the architecture, not a size in it")
        self._arch = shape
        return shape

    def identities(self, params: dict) -> dict[str, str]:
        """``{node: identity}`` at ``params``: for each node of :meth:`architecture`, a hash of what
        would be synthesized.  Cheap -- builds the Python system, synthesizes nothing.

        The service's cache and charge key: a node whose identity the ledger holds costs nothing,
        so two points that share a sub-top share its synthesis.  Replay looks results up by it.
        Never shown to the agent.
        """
        raise NotImplementedError(f"{type(self).__name__} runs its own build flow")

    def hls_identities(self, params: dict) -> dict[str, str]:
        """``{kernel: identity}`` at ``params``: for each HLS top of the DUT, a hash of what would be
        C-synthesized (its generated source and build options).  The cache and charge key for
        ``hls_estimate``, finer than :meth:`identities` because one part may hold several HLS
        kernels.  Never shown to the agent."""
        raise NotImplementedError(f"{type(self).__name__} runs its own build flow")

    def golden_identity(self, params: dict, workload: dict) -> str:
        """What ``golden`` computes at ``params`` and ``workload``, as a key: two calls with one key
        share one computation, so the service charges it once.  Default: the whole point and workload
        (nothing shared); a design whose accuracy depends on fewer parameters names them.  Never shown
        to the agent."""
        import json

        return json.dumps([self.fill(params), workload], sort_keys=True, default=str)

    def sim_identity(self, params: dict, workload: dict) -> dict[str, str]:
        """``{"build": ..., "run": ...}`` at ``params`` and ``workload``: ``build`` combines every
        node's identity (what the RTL is elaborated from), ``run`` adds the design parameters that
        rebuild nothing and the workload.  The cache and charge key for ``rtlsim``: a new ``build``
        pays elaboration and simulation, a new ``run`` on an elaborated ``build`` pays simulation
        alone.  Never shown to the agent."""
        raise NotImplementedError(f"{type(self).__name__} runs its own build flow")

    def rebuilds(self) -> dict[str, list[str]]:
        """For each design parameter, the built nodes whose identity changes when it moves from its
        default to another legal value (:meth:`legal_with` completing the point): :meth:`identities`
        at both, compared node by node; empty for a parameter that rebuilds nothing (it changes
        only :meth:`sim_identity`).  Shown in ``DSE.md`` as each node's "depends on" and each
        parameter's "rebuilds"."""
        base = self.identities({})
        out: dict[str, list[str]] = {}
        for p in self.space()["parameters"]:
            if p["kind"] != "design":
                continue
            changed: set[str] = set()
            for v in p["values"]:
                if v != p["default"]:
                    ids = self.identities(self.legal_with({p["name"]: v}))
                    changed |= {n for n in base if ids[n] != base[n]}
            out[p["name"]] = sorted(changed)
        return out
