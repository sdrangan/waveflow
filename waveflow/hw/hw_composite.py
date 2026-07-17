"""hw_composite.py — :class:`CompositeComp`, a **thin alias-subclass** of :class:`FreeRunComp`.

There is no longer a separate composite class.  A composite *is* a :class:`~waveflow.hw.hw_freerun.
FreeRunComp` that has sub-components instead of a ``run_iter`` body — all of the composite machinery
(the ``boundary`` / ``ordered_subcomps`` / ``internal_edges`` a composite declares, the passive
``run_proc``, the body-XOR-children invariant) lives on ``FreeRunComp`` now, where a leaf is the
1-task degenerate case of the same walk.  See ``plans/one_component_two_flows.md``.

``CompositeComp`` survives only to carry a **different codegen target name** (``composite_kernel``
rather than ``free_running_kernel``) and to keep the readable ``class MyTop(CompositeComp):``
declaration sites working unchanged.  Stage 4 of that plan collapses the two target names into one, at
which point this subclass can be deleted and its users re-parented to ``FreeRunComp`` directly.

On ``control_mode``: it is ``AUTO`` here (inherited), and — stated honestly — **nothing reads it
today**.  The generator (:func:`~waveflow.build.composite_gen.render_top`) emits ``ap_ctrl_none``
unconditionally; it does not consult ``control_mode``.  ``AUTO`` is a *declared-but-unimplemented*
value, the same status a declared-but-unreachable codegen target has: a real name whose reader is
future work, scheduled in ``plans/exec_model_classes.md`` ("honoring explicit ``control_mode``; keep
the regmap-presence inference as a fallback").  Do **not** read it as "the control mode is *derived*
from the boundary" — no such derivation exists; a regmap boundary does not, today, make the top
host-activated, and when that reader is built a regmap-bearing boundary is a Flow-1 case
(:class:`~waveflow.hw.hw_hostactivated.HostActivated`'s job), not a composite's.
"""
from __future__ import annotations

from typing import ClassVar

from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.hw_freerun import FreeRunComp


class CompositeComp(FreeRunComp):
    """A composite ``FreeRunComp``: sub-components + internal edges, **no kernel body**.

    Subclasses declare their structure in ``__post_init__`` (``add_comp`` children, ``add_if`` edges,
    and the ``boundary`` / ``ordered_subcomps`` / ``internal_edges`` the generator walks) and do **not**
    override ``run_iter`` — a composite has no body; its children do the work.  That "body XOR children"
    rule is the :meth:`~waveflow.hw.hw_freerun.FreeRunComp._kind` invariant, checked post-construction
    (a subclass's children arrive in ``__post_init__``, after the base constructor runs, so it cannot be
    a class-definition-time check).

    The only thing this class adds over ``FreeRunComp`` is the codegen **target name**: a composite
    lowers to ``composite_kernel``, a leaf to ``free_running_kernel`` — two names for one product that
    Stage 4 of ``plans/one_component_two_flows.md`` collapses.  Everything else is inherited.
    """

    #: A composite lowers to ``composite_kernel`` (one ``hls::task`` per child + one channel per
    #: internal edge, ``ap_ctrl_none``).  This override is the whole reason the subclass exists; it goes
    #: away when Stage 4 collapses ``free_running_kernel`` and ``composite_kernel`` into one name.
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})
