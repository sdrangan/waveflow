"""hw_composite.py — ``CompositeComp`` is now an **alias** for :class:`FreeRunComp`.

There is no separate composite class, and after the target-vocabulary collapse there is nothing left
for even a thin subclass to carry: a composite and a leaf lower to the *same* target
(``composite_kernel``) through the *same* generator, differing only in whether the component has
sub-components or a ``run_iter`` body.  So ``CompositeComp`` **is** ``FreeRunComp``.

The name survives purely as documentation at declaration sites: ``class MemCopy(CompositeComp):``
reads as "a composite" and ``class Sequencer(FreeRunComp):`` as "a leaf", but they are one class and
the distinction is made by content (``add_comp`` children vs an overridden ``run_iter``), enforced by
:meth:`~waveflow.hw.hw_freerun.FreeRunComp._kind`.  All the machinery — the derived/overridable
``boundary`` / ``ordered_subcomps`` / ``internal_edges``, the passive-vs-looping ``run_proc``, the
body-XOR-children invariant — lives on ``FreeRunComp``.  See ``plans/one_component_two_flows.md``.

This alias can be deleted and its ~5 users re-parented to ``FreeRunComp`` whenever the readability of
the two names stops paying for the indirection; it is kept only to avoid churning those declarations.
"""
from __future__ import annotations

from waveflow.hw.hw_freerun import FreeRunComp

#: A composite is a :class:`FreeRunComp` that has sub-components instead of a body.  Same class.
CompositeComp = FreeRunComp
