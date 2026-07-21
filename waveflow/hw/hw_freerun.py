"""hw_freerun.py — :class:`FreeRunComp`, the free-running (``ap_ctrl_none``) synthesizable component.

**One class, two shapes.**  A ``FreeRunComp`` is either

* **standalone** — you implement :meth:`run_iter` (one firing) and the base loops it forever; it
  lowers to a single free-running ``ap_ctrl_none`` ``hls::task`` whose body is that ``run_iter``; or
* a **composite** — you ``add_comp`` sub-components (themselves ``FreeRunComp``\\ s) wired by
  ``add_if`` interfaces, and name the ``boundary`` ports the generator walks (``ordered_subcomps`` /
  ``internal_edges`` are derived); it lowers to one ``hls::task`` per child plus one channel per
  internal edge.

A standalone component is literally the **1-task degenerate case** of a composite: it has one task
(itself) and no internal edges (see the derived :attr:`boundary` / :attr:`ordered_subcomps` /
:attr:`internal_edges` below), so :func:`~waveflow.build.composite_gen.composite_top_spec` walks both
through the *same* generator.  **There is no separate composite class** — the top level of a design or
a testbench is a ``FreeRunComp`` too, and a composite is just one that has sub-components instead of a
body.  The kind is decided by content, not type: :meth:`_kind` returns ``'standalone'`` or
``'composite'`` by inspecting whether the component overrode ``run_iter`` or added children.  See
``plans/one_component_two_flows.md``.

**Body XOR children** is the invariant: exactly one of "overrides ``run_iter``" and "has
sub-components" holds.  It is an *instance* fact, not a class fact — a subclass's children arrive in
``__post_init__``, after the base constructor has run — so it is checked post-construction, wherever
the two shapes diverge: :meth:`run_proc` (the pysim schedule point) and
:func:`~waveflow.build.codegen_dispatch.codegen_path` (the codegen dispatch point), both via
:meth:`_kind`.

For the ``hls::task`` re-firing model: the runtime re-fires the leaf body on each new job (see
``waveflow/build/mem_r_stream_task.h``: *"the hls::task runtime RE-FIRES this on each new command
(there is NO internal command loop)"*), so ``run_iter`` **is** the task function and the ``while True``
in :meth:`_run_iter_forever` is only the discrete-event stand-in for that re-firing.  There is no
"before the loop" in an ``hls::task``; keep persistent state on ``self`` (set in ``__post_init__``).

Because the execution model is declared by the class (``control_mode = FREE_RUNNING``), codegen never
has to infer free-running-ness by finding a ``while`` loop at the extracted root.  See
``plans/exec_model_classes.md`` and ``docs/guide/components/taxonomy.md``.
"""
from __future__ import annotations

from typing import ClassVar

from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.hw_component import ControlMode, HwComponent
from waveflow.simulation.simobj import ProcessGen


class FreeRunComp(HwComponent):
    """A free-running synthesizable component — a leaf (implements :meth:`run_iter`) **or** a composite
    (has sub-components). Lowers to a free-running ``ap_ctrl_none`` ``hls::task`` top."""

    control_mode: ClassVar[ControlMode] = ControlMode.FREE_RUNNING
    _kernel_method: ClassVar[str] = 'run_iter'

    #: The codegen targets that **exist for this kind** — a free-running kernel is the DUT of Flow 2,
    #: whether a leaf (one ``hls::task``) or a composite (one per child).  One target name,
    #: ``composite_kernel``, for both: a leaf is the 1-task degenerate case.  It declares the *path*,
    #: not a guarantee — :func:`~waveflow.build.codegen_check.check` is what answers whether a given
    #: component actually makes it (see :class:`~waveflow.hw.hw_hostactivated.HostActivated` for why
    #: this is ``potential_`` and not ``supported_``).
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    # -- kind: standalone (body) XOR composite (children) --------------------------------------------

    def _kind(self) -> str:
        """``'standalone'`` (overrides :meth:`run_iter`) or ``'composite'`` (has sub-components) — never
        both, never neither.  The **body-XOR-children** invariant, enforced post-construction (children
        are populated in the subclass ``__post_init__``, so this cannot run at base-construction time)."""
        has_body = type(self).run_iter is not FreeRunComp.run_iter
        has_children = bool(self.sub_comps)
        if has_body and has_children:
            raise TypeError(
                f"{type(self).__name__} defines both a run_iter body and sub-components; a "
                f"FreeRunComp is one or the other (body XOR children). Move the body into a "
                f"sub-component, or drop the sub-components."
            )
        if has_body:
            return 'standalone'
        if has_children:
            return 'composite'
        raise TypeError(
            f"{type(self).__name__} has neither a run_iter body nor sub-components; it must either "
            f"implement run_iter (a standalone kernel) or add_comp sub-components (a composite)."
        )

    # -- boundary / subcomps / edges: derived for a leaf, overridable for a composite ---------------
    #
    # A leaf derives all three (it has no body-less structure to declare); a composite assigns them in
    # its __post_init__.  Each is a property whose getter returns the assigned override if present
    # (a composite) and otherwise derives the leaf value.  The setter stores under a private key so a
    # composite's ``self.boundary = [...]`` works despite the class-level property.

    @property
    def boundary(self) -> tuple[tuple[str, object], ...]:
        """The top's boundary ports as ``(name, endpoint)`` pairs, in top-signature order.

        A **composite** assigns the port *names* in ``__post_init__`` (``self.boundary = ["s_cmd",
        ...]``); the endpoints and their order are derived from the graph by
        :func:`~waveflow.build.composite_gen.derive_boundary` — a child endpoint not bound to one of
        the composite's internal interfaces *is* a boundary port, in ``add_comp`` × ``add_endpoint``
        order.  Only the names are declared, because local names collide (two children both call
        their AXI port ``m_mem``).  A list of ``(name, endpoint)`` pairs is still accepted.

        A **leaf** derives the whole thing: the *order* is :meth:`kernel_task`'s signature (so the
        top's C++ parameter list and the task's call args are literally the same list and cannot
        disagree) and the *endpoint* is the attribute.  Direction comes from the endpoint's type
        (:func:`~waveflow.build.composite_gen.kind_of_endpoint`) and the bundle from the assembler's
        policy (:func:`~waveflow.build.composite_gen.bundle_map`), so a leaf declares *nothing*.
        ``composite_top_spec`` walks a leaf exactly as it walks a composite.
        """
        ov = self.__dict__.get('_boundary')
        if ov is not None:
            if ov and all(isinstance(e, str) for e in ov):
                from waveflow.build.composite_gen import derive_boundary
                return derive_boundary(self, ov)
            return ov
        if self.sub_comps:
            raise TypeError(
                f"{type(self).__name__} is a composite (has sub-components) but does not declare "
                f"self.boundary; a composite must name its boundary ports in __post_init__ (a list of "
                f"port names, in add_comp x add_endpoint order). Only a leaf derives its boundary "
                f"entirely (from kernel_task()'s signature)."
            )
        return tuple((attr, getattr(self, attr)) for attr in self.kernel_task().signature)

    @boundary.setter
    def boundary(self, value) -> None:
        self.__dict__['_boundary'] = value

    @property
    def ordered_subcomps(self) -> list:
        """The active sub-tasks, in emit order.  A **leaf**'s one task is itself (the 1-task case); a
        **composite** assigns this (or, if it does not, the children in ``add_comp`` order)."""
        ov = self.__dict__.get('_ordered_subcomps')
        if ov is not None:
            return ov
        return list(self.sub_comps.values()) if self.sub_comps else [self]

    @ordered_subcomps.setter
    def ordered_subcomps(self, value) -> None:
        self.__dict__['_ordered_subcomps'] = value

    @property
    def internal_edges(self) -> list:
        """The internal channels wiring the sub-tasks.

        A **leaf** wires nothing (every port is a boundary port).  A **composite** derives these from
        the interfaces it registered with ``add_if`` — see
        :func:`~waveflow.build.composite_gen.derive_internal_edges`.  ``add_if`` already records the
        master↔slave connection *and* the lowering kind (its type), so declaring a parallel edge list
        could only restate it or contradict it.  An explicit assignment still wins, for a composite
        that needs an edge the graph does not describe."""
        ov = self.__dict__.get('_internal_edges')
        if ov is not None:
            return ov
        if self.interfaces:
            from waveflow.build.composite_gen import derive_internal_edges
            return derive_internal_edges(self)
        return []

    @internal_edges.setter
    def internal_edges(self, value) -> None:
        self.__dict__['_internal_edges'] = value

    # -- timing calibration (opt-in) -----------------------------------------------------------------
    #
    # A leaf that wants its pysim timing calibrated against RTL attaches a TimingModel with
    # :meth:`add_timing_model`.  Its presence turns on per-firing recording: the base loop times each
    # firing (first input -> end of run_iter) and, when the body called :meth:`timed_delay`, keeps a
    # row of {features, current_dly, span} on :attr:`firing_records`.  A component with no model pays
    # nothing (the attributes do not exist and the loop takes the plain path).  See
    # ``plans/timing_model.md``; the DAG's CollectTimingStep reads firing_records after a pysim run.

    def add_timing_model(self, tm) -> None:
        """Attach a :class:`~waveflow.calib.timing_model.TimingModel`, enabling per-firing recording.
        The model itself is built by the component (it knows its ``clk`` and calib dir)."""
        self._timing_model = tm
        self.firing_records: list[dict] = []

    @property
    def timing_model(self):
        """The attached model, or ``None`` — the signal the collection steps discover on."""
        return getattr(self, "_timing_model", None)

    def timed_delay(self, features: dict) -> float:
        """Predict this firing's additional delay from the attached model AND record the firing for
        later calibration.  Returns 0.0 (recording nothing) when no model is attached, so the body
        can call it unconditionally — ``yield self.timeout(self.timed_delay({...}))`` is a no-op on
        an uncalibrated component and the current firing's delay on a calibrated one.

        The delay is in the model's output unit — time when the model carries a ``clk`` (the usual
        case), so it goes straight to :meth:`timeout`."""
        tm = self.timing_model
        if tm is None:
            return 0.0
        dly = tm.predict(features)[0]
        self._pending_firing = {**features, "current_dly": dly}
        return dly

    # -- simulation ----------------------------------------------------------------------------------

    def run_proc(self) -> ProcessGen[None] | None:
        """A **leaf** drives :meth:`run_iter` forever (the runtime's re-firing, in the DES); a
        **composite** is *passive* (returns ``None``) — its children own the concurrent processes.

        This is a plain method, not a generator function, so it can return ``None`` for the composite
        case (a generator function would return a generator, never ``None``, and always be scheduled).
        """
        return self._run_iter_forever() if self._kind() == 'standalone' else None

    def _run_iter_forever(self) -> ProcessGen[None]:
        if self.timing_model is None:
            while True:                                 # plain path: no recording overhead
                yield from self.run_iter()
        while True:
            t0 = self.now
            self._pending_firing = None                 # timed_delay sets this if the body predicts
            yield from self.run_iter()
            if self._pending_firing is not None:
                # `end` (absolute sim time of firing completion) mirrors the RTL table's ap_done
                # column, so the pysim firing cadence -- the period -- is diff(ends), the same way
                # the RTL period is measured.  `span` is the firing's own duration.
                self._pending_firing["span"] = self.now - t0
                self._pending_firing["end"] = self.now
                self.firing_records.append(self._pending_firing)

    def run_iter(self) -> ProcessGen[None]:
        """One firing of the free-running loop — the ``hls::task`` body.  **Override this for a leaf.**
        The default is the not-overridden sentinel: a composite leaves it be (its children do the
        work), and :meth:`_kind` detects the override by identity against this base method."""
        raise NotImplementedError(
            f"{type(self).__name__}.run_iter is not implemented. A leaf FreeRunComp must override "
            f"run_iter; a composite must not call it (it has no body — its children do the work)."
        )
        yield  # unreachable: marks this a generator function so an accidental call yields cleanly


def discover_timing_models(root) -> list[tuple["FreeRunComp", object]]:
    """Walk *root* and its ``sub_comps`` for every attached ``TimingModel``.

    Returns ``[(component, model), ...]``.  Mirrors ``discover_dyn_params`` (per-object) lifted to a
    tree walk — the DAG's collect/fit steps use it to find which components to collect for, and a
    component with no model is simply absent from the list."""
    out: list[tuple[FreeRunComp, object]] = []
    seen: set[int] = set()
    stack = [root]
    while stack:
        c = stack.pop()
        if id(c) in seen:
            continue
        seen.add(id(c))
        tm = getattr(c, "timing_model", None)
        if tm is not None:
            out.append((c, tm))
        stack.extend(getattr(c, "sub_comps", {}).values())
    return out
