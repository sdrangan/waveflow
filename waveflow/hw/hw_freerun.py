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

from waveflow.hw.hw_component import ControlMode, HwComponent
from waveflow.simulation.simobj import ProcessGen


class FreeRunComp(HwComponent):
    """A free-running synthesizable component: implement :meth:`run_iter` (one firing); the base loops
    it forever. Lowers to a free-running ``ap_ctrl_none`` ``hls::task``."""

    control_mode: ClassVar[ControlMode] = ControlMode.FREE_RUNNING
    _kernel_method: ClassVar[str] = 'run_iter'

    def run_proc(self) -> ProcessGen[None]:
        """The pysim golden: drive :meth:`run_iter` forever (the runtime's re-firing, in the DES)."""
        while True:
            yield from self.run_iter()

    @abstractmethod
    def run_iter(self) -> ProcessGen[None]:
        """One firing of the free-running loop — the ``hls::task`` body. Override this."""
        raise NotImplementedError
