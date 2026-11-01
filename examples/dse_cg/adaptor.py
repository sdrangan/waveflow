"""adaptor.py -- DseAdaptor, the base class a design subclasses to be explored.

The brief generator (``brief.py``), the service (``service.py``) and the scripted policies
(``policies.py``) see a design only through a ``DseAdaptor``; nothing else in this directory knows
which design it is exploring.  A subclass implements what only it can know -- its space, its prose,
how to build its system, its golden model -- and inherits the rest: defaults, legality, the
architecture (the sub-top tree and its interconnects, the same at every point), each sub-top's
identity at a point, its synthesis, and which sub-tops each parameter rebuilds.

The CG detector's adaptor is ``cg_detector.py`` (V1 in ``plans/dse_cg.md``).  This file knows nothing
about CG; it moves into the ``waveflow`` package once a second design uses it.

Skeleton: :meth:`defaults`, :meth:`fill` and :meth:`legal` are written, because they follow from
:meth:`space` alone; the defaults that need a built system are plan phases P1b and P2.
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
    override if needed      default
    ======================  ==================================================================
    :meth:`pysim`           run :meth:`build_system`'s system in pysim, resources from the
                            calibrated models
    :meth:`synth`           synthesize one node of the architecture out of context (the
                            per-device route)
    :meth:`rtlsim`          simulate :meth:`build_system`'s DUT at RTL (the incremental XSI flow)
    :meth:`hls_estimate`    C-synthesize one HLS kernel and parse its report (the ``csynth`` step)
    ======================  ==================================================================

    ======================  ==================================================================
    provided                what
    ======================  ==================================================================
    :meth:`defaults`        every parameter's default
    :meth:`fill`            a point with its missing parameters defaulted, unknown names refused
    :meth:`legal`           the first constraint a point breaks, or ``None``
    :meth:`architecture`    the sub-top tree and its interconnects; checks it is point-invariant
    :meth:`identities`      each node's identity at a point: the service's cache and charge key
    :meth:`hls_identities`  each HLS kernel's identity at a point: the same, for ``hls_estimate``
    :meth:`rebuilds`        for each design parameter, the sub-tops a new value rebuilds
    ======================  ==================================================================

    The architecture is what DSE.md shows the agent; the identities are bookkeeping only the
    service sees.
    """

    name: ClassVar[str]
    platform: ClassVar[str]

    # -- implement ----------------------------------------------------------------------------

    @abstractmethod
    def space(self) -> dict:
        """Parameters, constraints and workloads, as data::

            {"parameters": [{"name", "kind": "design" | "workload", "values", "default",
                             "description"}, ...],
             "constraints": [{"rule": "<Python expression over parameter names>", "why"}, ...],
             "workloads": [{"name", "fixes": {...}, "valid_for": [metric, ...]}, ...]}

        The shape ``dselib.space()`` returns, minus ``rebuilds``, which :meth:`rebuilds` adds.
        """

    @abstractmethod
    def context(self) -> dict:
        """The hand-written parts of ``DSE.md``: title, description (what the system does),
        data flow (how work moves through the architecture: what no walk of the tree can say),
        doc pages, workload descriptions, metrics (name, unit, description).  Facts only: no
        objective, no advice."""

    @abstractmethod
    def build_system(self, params: dict) -> Any:
        """The ``HwSystem`` at design point ``params`` (filled): what is simulated, synthesized, and
        walked for the architecture."""

    @abstractmethod
    def golden(self, params: dict, workload: dict) -> dict:
        """Untimed bit-exact accuracy: the body of ``dselib.golden``."""

    # -- override if needed -------------------------------------------------------------------

    def pysim(self, params: dict, workload: dict) -> dict:
        """Accuracy (exact) + cycles, per-block activity, resources (calibrated): the body of
        ``dselib.pysim``.  Default: run :meth:`build_system`'s system on the workload in pysim for
        cycles and activity, and ``estimate_system`` for resources."""
        raise NotImplementedError("P1b")

    def synth(self, node: str, params: dict) -> dict:
        """Synthesize node ``node`` of :meth:`architecture` (with everything under it) at
        ``params``, out of context, and return its row: ``lut``, ``ff``, ``dsp``, ``bram``, timing,
        ``status``, and the per-instance utilization report as an artifact.

        Called only by the live backend, and only for an identity the ledger does not hold yet.
        Default: the per-device route (``synth_route="per_device"``, ``waveflow.build.system_ooc``)
        on that sub-top, or the crossbar IP.
        """
        raise NotImplementedError("P1b")

    def rtlsim(self, params: dict, workload: dict) -> dict:
        """Simulate the DUT at ``params`` at RTL on the workload and return its row: cycles, the
        bit-exact check against pysim, per-block activity, and the activity table as an artifact.

        Called only by the live backend.  Default: the system flow on :meth:`build_system`'s system
        (``add_system_steps`` to ``system_xsi``), incremental, so a new workload on a design already
        elaborated pays for simulation alone.
        """
        raise NotImplementedError("P1b")

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
        raise NotImplementedError("P1b")

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
        rule that mixes design and workload parameters (``nit <= K``) is checked like any other.
        """
        values = {**self.defaults(), **params, **(workload or {})}
        for c in self.space()["constraints"]:
            if not eval(c["rule"], {"__builtins__": {}}, values):
                return c["rule"]
        return None

    def architecture(self) -> dict:
        """The DUT as synthesized, at the defaults: the tree of nodes (each sub-top -- a device or a
        memory -- and the crossbar, with its kind and the modules it holds) and the interconnects
        between them (crossbar slots, stream nets that cross sub-tops, interrupt lines, and what
        crosses the DUT boundary), walked from ``SystemTopSpec.from_system(build_system(...))``.

        Parametric DSE means this is the same at every point: only the sizes inside the nodes
        change.  So it is computed once, and checked: the tree's shape at the defaults and at each
        parameter's extreme values must match, or the "parameter" changes the architecture and the
        adaptor is refused.
        """
        raise NotImplementedError("P2")

    def identities(self, params: dict) -> dict[str, str]:
        """``{node: identity}`` at ``params``: for each node of :meth:`architecture`, a hash of what
        would be synthesized.  Cheap -- builds the Python system, synthesizes nothing.

        The service's cache and charge key: a node whose identity the ledger holds costs nothing,
        so two points that share a sub-top share its synthesis.  Replay looks results up by it.
        Never shown to the agent.
        """
        raise NotImplementedError("P1b")

    def hls_identities(self, params: dict) -> dict[str, str]:
        """``{kernel: identity}`` at ``params``: for each HLS top of the DUT, a hash of what would be
        C-synthesized (its generated source and build options).  The cache and charge key for
        ``hls_estimate``, finer than :meth:`identities` because one part may hold several HLS
        kernels.  Never shown to the agent."""
        raise NotImplementedError("P1b")

    def rebuilds(self) -> dict[str, list[str]]:
        """For each design parameter, the nodes whose identity changes when it moves from its
        default to another legal value: :meth:`identities` at both, compared node by node.  Shown in
        ``DSE.md`` as each node's "depends on" and each parameter's "rebuilds"."""
        raise NotImplementedError("P2")
