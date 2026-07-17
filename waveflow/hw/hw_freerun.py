"""hw_freerun.py — :class:`FreeRunComp`, the free-running (``ap_ctrl_none``) synthesizable component.

**One class, two shapes.**  A ``FreeRunComp`` is either

* a **leaf** — you implement :meth:`run_iter` (one firing) and the base loops it forever; it lowers
  to a single free-running ``ap_ctrl_none`` ``hls::task`` whose body is that ``run_iter``; or
* a **composite** — you ``add_comp`` sub-components (themselves ``FreeRunComp``\\ s) wired by
  ``add_if`` interfaces, and declare the ``boundary`` / ``ordered_subcomps`` / ``internal_edges`` the
  generator walks; it lowers to one ``hls::task`` per child plus one channel per internal edge.

A leaf is literally the **1-task degenerate case** of a composite: it has one task (itself) and no
internal edges (see the derived :attr:`boundary` / :attr:`ordered_subcomps` / :attr:`internal_edges`
below), so :func:`~waveflow.build.composite_gen.composite_top_spec` walks both through the *same*
generator.  There is no separate composite class — a composite is a ``FreeRunComp`` that has
sub-components instead of a body.  (``CompositeComp`` survives in ``hw_composite.py`` only as a thin
subclass that renames the codegen target; it is the same machinery.  See
``plans/one_component_two_flows.md``.)

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

    # -- kind: leaf (body) XOR composite (children) --------------------------------------------------

    def _kind(self) -> str:
        """``'leaf'`` (overrides :meth:`run_iter`) or ``'composite'`` (has sub-components) — never both,
        never neither.  The **body-XOR-children** invariant, enforced post-construction (children are
        populated in the subclass ``__post_init__``, so this cannot run at base-construction time)."""
        has_body = type(self).run_iter is not FreeRunComp.run_iter
        has_children = bool(self.sub_comps)
        if has_body and has_children:
            raise TypeError(
                f"{type(self).__name__} defines both a run_iter body and sub-components; a "
                f"FreeRunComp is one or the other (body XOR children). Move the body into a "
                f"sub-component, or drop the sub-components."
            )
        if has_body:
            return 'leaf'
        if has_children:
            return 'composite'
        raise TypeError(
            f"{type(self).__name__} has neither a run_iter body nor sub-components; it must either "
            f"implement run_iter (a leaf kernel) or add_comp sub-components (a composite)."
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

        A **composite** assigns this in ``__post_init__``.  A **leaf** derives it: the *order* is
        :meth:`kernel_task`'s signature (so the top's C++ parameter list and the task's call args are
        literally the same list and cannot disagree) and the *endpoint* is the attribute.  Direction
        comes from the endpoint's type (:func:`~waveflow.build.composite_gen.kind_of_endpoint`) and the
        bundle from the assembler's policy (:func:`~waveflow.build.composite_gen.bundle_map`), so a leaf
        declares *nothing* — its boundary is a consequence of the ports it has and the signature it
        exposes.  ``composite_top_spec`` walks a leaf exactly as it walks a composite.
        """
        ov = self.__dict__.get('_boundary')
        if ov is not None:
            return ov
        if self.sub_comps:
            raise TypeError(
                f"{type(self).__name__} is a composite (has sub-components) but does not declare "
                f"self.boundary; a composite must set its boundary ports in __post_init__. Only a leaf "
                f"derives its boundary (from kernel_task()'s signature)."
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
        """The internal channels wiring the sub-tasks.  A **leaf** wires nothing (every port is a
        boundary port); a **composite** assigns this."""
        ov = self.__dict__.get('_internal_edges')
        return ov if ov is not None else []

    @internal_edges.setter
    def internal_edges(self, value) -> None:
        self.__dict__['_internal_edges'] = value

    # -- simulation ----------------------------------------------------------------------------------

    def run_proc(self) -> ProcessGen[None] | None:
        """A **leaf** drives :meth:`run_iter` forever (the runtime's re-firing, in the DES); a
        **composite** is *passive* (returns ``None``) — its children own the concurrent processes.

        This is a plain method, not a generator function, so it can return ``None`` for the composite
        case (a generator function would return a generator, never ``None``, and always be scheduled).
        """
        return self._run_iter_forever() if self._kind() == 'leaf' else None

    def _run_iter_forever(self) -> ProcessGen[None]:
        while True:
            yield from self.run_iter()

    def run_iter(self) -> ProcessGen[None]:
        """One firing of the free-running loop — the ``hls::task`` body.  **Override this for a leaf.**
        The default is the not-overridden sentinel: a composite leaves it be (its children do the
        work), and :meth:`_kind` detects the override by identity against this base method."""
        raise NotImplementedError(
            f"{type(self).__name__}.run_iter is not implemented. A leaf FreeRunComp must override "
            f"run_iter; a composite must not call it (it has no body — its children do the work)."
        )
        yield  # unreachable: marks this a generator function so an accidental call yields cleanly
