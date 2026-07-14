"""hw_hostactivated.py — :class:`HostActivated`, the host-activated (regmap-launched) synth leaf.

A ``HostActivated`` is the **invocation** execution model: it carries a
:class:`~waveflow.hw.regmap.VitisRegMapMMIFSlave`, and the host writes ``ap_start`` to launch
:meth:`on_start` — one run per trigger (``ap_ctrl_hs``: the ``ap_start``/``ap_done`` handshake). The
kernel reads its inputs from the register map, computes, writes results, and returns. This is the
model ``poly`` / ``simp_fun`` use; ``HostActivated`` makes it an explicit, **class-dispatched** kind
instead of a plain :class:`~waveflow.hw.hw_component.HwComponent` reaching ``on_start`` only through a
regmap fallback.

It is a :class:`~waveflow.hw.hw_component.SynthComp` that declares ``_kernel_method = 'on_start'``.
Codegen dispatch is by class (:func:`~waveflow.build.codegen_dispatch.codegen_path`): a
``HostActivated`` lowers ``on_start`` as its kernel body. See ``plans/exec_model_classes.md`` and
``docs/guide/components/taxonomy.md``.
"""
from __future__ import annotations

from typing import ClassVar

from waveflow.hw.hw_component import ControlMode, SynthComp


class HostActivated(SynthComp):
    """A host-activated (regmap-launched) synthesizable leaf: implement :meth:`on_start`.

    Contract:

    - Carries a :class:`~waveflow.hw.regmap.VitisRegMapMMIFSlave` (the ``ap_start``/``ap_done`` +
      register file). This is the *defining* property; because a component's endpoints are populated
      by the subclass ``__post_init__`` **after** the ``SynthComp`` super-chain runs (the same
      construction-ordering limit :class:`~waveflow.hw.hw_composite.CompositeComp` hits with its
      children), it is not construction-checked here — it is enforced by the codegen path (a kernel
      with no regmap emits no ``s_axilite`` control interface).
    - Implements :meth:`on_start` — verified at construction by
      :meth:`~waveflow.hw.hw_component.SynthComp._check_synthesizable` via ``_kernel_method``.
    - Must **not** define ``run_iter`` — that is a *free-running* leaf's entry
      (:class:`~waveflow.hw.hw_freerun.FreeRunComp`); a host-activated leaf runs once per trigger, not
      continuously. Enforced at class-definition time (:meth:`__init_subclass__`).
    """

    #: The invocation control protocol (``ap_ctrl_hs`` / ``ap_start``-``ap_done``).  Declared for
    #: intent; ``control_mode`` is not yet consumed by the pragma emitter (that rides with a real
    #: consumer later — see ``plans/exec_model_classes.md``), so this changes no generated output.
    control_mode: ClassVar[ControlMode] = ControlMode.PER_INVOCATION

    #: Intent metadata (and the entry :meth:`~waveflow.hw.hw_component.SynthComp._check_synthesizable`
    #: verifies exists at construction): a host-activated leaf's body is ``on_start``.  The codegen
    #: dispatch is class-based (:func:`~waveflow.build.codegen_dispatch.codegen_path`); this states the
    #: same intent declaratively.
    _kernel_method: ClassVar[str] = 'on_start'

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Class-level contract: a host-activated leaf runs once per trigger, so it must not carry a
        # free-running `run_iter`. HostActivated itself has no run_iter, so a truthy lookup means a
        # subclass added one. (Mirrors CompositeComp's class-level check.)
        if getattr(cls, 'run_iter', None) is not None:
            raise TypeError(
                f"{cls.__name__} is a HostActivated but defines run_iter(); a host-activated leaf "
                f"runs once per trigger (on_start), not continuously. Implement on_start(), or derive "
                f"from FreeRunComp instead."
            )
