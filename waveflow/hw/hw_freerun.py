"""hw_freerun.py — :class:`FreeRunComp`, the free-running (``ap_ctrl_none``) synthesizable component.

A ``FreeRunComp`` loops forever, one job per firing. You implement :meth:`FreeRunComp.run_iter` (a
single firing); the base drives it in an infinite loop for the Python simulation.

This maps to a free-running ``ap_ctrl_none`` ``hls::task`` whose body the runtime **re-fires** each
job — there is no internal loop in the generated task (see
``waveflow/build/mem_r_stream_task.h``: *"the hls::task runtime RE-FIRES this on each new command
(there is NO internal command loop)"*). So ``run_iter`` **is** the task function, and the ``while True``
here is only the discrete-event stand-in for that re-firing. There is no "before the loop" in an
hls::task; keep persistent state on ``self`` (set in ``__post_init__``) — it lowers to ``static`` locals.

Because the execution model is declared by the class (``control_mode = FREE_RUNNING``,
``_kernel_method = 'run_iter'``), codegen never has to infer free-running-ness by finding a ``while``
loop at the extracted root. See ``plans/exec_model_classes.md`` and
``docs/guide/components/taxonomy.md``.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from waveflow.hw.codegen_targets import FREE_RUNNING_KERNEL
from waveflow.hw.hw_component import ControlMode, HwComponent
from waveflow.simulation.simobj import ProcessGen


class FreeRunComp(HwComponent):
    """A free-running synthesizable component: implement :meth:`run_iter` (one firing); the base loops
    it forever. Lowers to a free-running ``ap_ctrl_none`` ``hls::task``."""

    control_mode: ClassVar[ControlMode] = ControlMode.FREE_RUNNING
    _kernel_method: ClassVar[str] = 'run_iter'

    #: The codegen targets that **exist for this kind** — a free-running leaf is the single-task DUT
    #: of Flows 2/3.  It declares the *path*, not a guarantee: the target is not implemented yet, and
    #: even once it is, :func:`~waveflow.build.codegen_check.check` is what answers whether a given
    #: component makes it.  (See :class:`~waveflow.hw.hw_hostactivated.HostActivated` for why this is
    #: ``potential_`` and not ``supported_``.)
    potential_targets: ClassVar[frozenset[str]] = frozenset({FREE_RUNNING_KERNEL})

    #: ``m_axi`` bundle for each ``m_axi`` port when this leaf is compiled as its OWN top, keyed by
    #: endpoint attribute name.  A standalone kernel has one bundle per memory port, and the first is
    #: ``gmem0`` — but bundle assignment belongs to whoever assembles the top (the same
    #: ``MemWStream.m_mem`` is ``gmem0`` standalone and ``gmem1`` inside ``MemCopy``), so a parent
    #: overrides it in its own ``boundary``.  This is the standalone default, not a property of the
    #: port.  See ``plans/endpoint_types_not_tags.md``.
    standalone_bundles: ClassVar[dict[str, str]] = {}

    @property
    def boundary(self) -> tuple[tuple[str, object, str | None], ...]:
        """This leaf's own boundary ports — **a standalone kernel IS the 1-task degenerate case**.

        Derived, not declared: the *order* is :meth:`kernel_task`'s signature (so the top's C++
        parameter list and the task's call args are literally the same list and cannot disagree), the
        *endpoint* is the attribute, and the *kind* comes from the endpoint's type
        (:func:`~waveflow.build.composite_gen.kind_of_endpoint`).  Only the bundle is a choice, and
        :attr:`standalone_bundles` carries the standalone default.

        With this, ``composite_top_spec`` walks a leaf exactly as it walks a composite — verified
        byte-identical against the hand-written table it replaced.  It is what lets
        ``free_running_kernel`` be ``composite_kernel`` with one task, rather than a second product.
        """
        return tuple(
            (attr, getattr(self, attr), self.standalone_bundles.get(attr))
            for attr in self.kernel_task().signature
        )

    @property
    def ordered_subcomps(self) -> list:
        """A leaf's one task is itself — which is what "the 1-task degenerate case" means literally."""
        return [self]

    @property
    def internal_edges(self) -> list:
        """A leaf wires nothing: every port is a boundary port."""
        return []

    def run_proc(self) -> ProcessGen[None]:
        """The pysim golden: drive :meth:`run_iter` forever (the runtime's re-firing, in the DES)."""
        while True:
            yield from self.run_iter()

    @abstractmethod
    def run_iter(self) -> ProcessGen[None]:
        """One firing of the free-running loop — the ``hls::task`` body. Override this."""
        raise NotImplementedError
