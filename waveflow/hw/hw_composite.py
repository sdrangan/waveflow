"""hw_composite.py — :class:`CompositeComp`, the hierarchical (bodyless) synthesizable component.

A ``CompositeComp`` is a **structural** component: it owns sub-components (:meth:`add_comp`) wired by
internal interfaces (:meth:`add_if`), and has **no kernel body of its own**.  Its C++ is the composite
top (:func:`waveflow.build.composite_gen.composite_top_spec` — one ``hls::task`` per active
child + one channel per internal edge), not an extracted ``run_iter``/``run_proc`` body.

It is a **passive sibling** of :class:`~waveflow.hw.hw_freerun.FreeRunComp`, *not* a subclass — a
composite is-not-a free-running leaf.  It has no ``run_iter`` and does not override ``run_proc``, so
``run_proc`` stays the ``SimObj`` default (``None`` — *passive*, no process scheduled: see
``simobj.py``).  Its children, registered via :meth:`~waveflow.hw.component.Component.add_comp`, are
independent scheduled ``SimObj`` s that own the concurrent processes.  There is no busy loop and no
"must not terminate" — there is simply no process at this level to run.

``control_mode`` is left ``AUTO``, and — stated honestly — **nothing reads it today**.  The generator
(:func:`~waveflow.build.composite_gen.render_top`) emits ``ap_ctrl_none`` unconditionally; it does not
consult ``control_mode`` at all.  ``AUTO`` is a *declared-but-unimplemented* value, the same status a
declared-but-unreachable codegen target has (see :mod:`waveflow.hw.codegen_targets`): a real name whose
reader is future work, scheduled in ``plans/exec_model_classes.md`` ("honoring explicit
``control_mode``; keep the regmap-presence inference as a fallback").

Do **not** read ``AUTO`` as "the control mode is *derived* from the boundary" — no such derivation
exists.  It is a plan, not a mechanism; a regmap boundary does not, today, make the top host-activated.
When that reader is built, a regmap-bearing boundary is a Flow-1 (host-activated) case anyway, which is
:class:`~waveflow.hw.hw_hostactivated.HostActivated`'s job, not a composite's.  See
``plans/one_component_two_flows.md`` and ``docs/guide/components/taxonomy.md``.
"""
from __future__ import annotations

from typing import ClassVar

from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.hw_component import HwComponent


class CompositeComp(HwComponent):
    """A hierarchical synthesizable component: sub-components + internal edges, **no kernel body**.

    Subclasses declare their structure in ``__post_init__`` (``add_comp`` children, ``add_if`` edges,
    and the ``boundary``/``ordered_subcomps``/``internal_edges`` descriptors the composite generator
    walks).  They must **not** define ``run_iter`` — a composite has no body; its children do the work.
    That contract is enforced at class-definition time (:meth:`__init_subclass__`).
    """

    #: The codegen targets that **exist for this kind** — a composite is the multi-task DUT of
    #: Flows 2/3 (one ``hls::task`` per child + one channel per internal edge, ``ap_ctrl_none``).
    #: Not implemented yet; see :class:`~waveflow.hw.hw_hostactivated.HostActivated` for why this is
    #: ``potential_`` and not ``supported_``.
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Class-level contract: a composite has NO body of its own, so it must not carry a `run_iter`
        # (that is FreeRunComp's leaf-kernel entry).  We cannot check "has sub-components" here or in
        # __post_init__ (the super-chain runs before the subclass populates add_comp), but "declares a
        # run_iter" is a pure class fact — fail loudly on it.  CompositeComp itself has no run_iter, so
        # a truthy lookup means a subclass added one.
        if getattr(cls, 'run_iter', None) is not None:
            raise TypeError(
                f"{cls.__name__} is a CompositeComp but defines run_iter(); a composite has no kernel "
                f"body of its own (its children do the work — codegen is the composite top). Move the "
                f"body into a FreeRunComp sub-component, or derive from FreeRunComp instead."
            )
