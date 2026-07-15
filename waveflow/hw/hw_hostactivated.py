"""hw_hostactivated.py — :class:`HostActivated`, the host-activated (regmap-launched) synth leaf.

A ``HostActivated`` is the **invocation** execution model: it carries a
:class:`~waveflow.hw.regmap.VitisRegMapMMIFSlave`, and the host writes ``ap_start`` to launch
:meth:`on_start` — one run per trigger (``ap_ctrl_hs``: the ``ap_start``/``ap_done`` handshake). The
kernel reads its inputs from the register map, computes, writes results, and returns. This is the
model ``poly`` / ``simp_fun`` use; ``HostActivated`` makes it an explicit, **class-dispatched** kind
instead of a plain :class:`~waveflow.hw.hw_component.HwComponent` reaching ``on_start`` only through a
regmap fallback.

It is a :class:`~waveflow.hw.hw_component.HwComponent` that declares ``_kernel_method = 'on_start'``.
Codegen dispatch is by class (:func:`~waveflow.build.codegen_dispatch.codegen_path`): a
``HostActivated`` lowers ``on_start`` as its kernel body. See ``plans/exec_model_classes.md`` and
``docs/guide/components/taxonomy.md``.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar

from waveflow.hw.codegen_targets import CONTROL_DRIVEN_KERNEL
from waveflow.hw.hw_component import ControlMode, HwComponent
from waveflow.simulation.simobj import ProcessGen


class HostActivated(HwComponent):
    """A host-activated (regmap-launched) synthesizable leaf: implement :meth:`on_start`.

    Contract:

    - Carries a :class:`~waveflow.hw.regmap.VitisRegMapMMIFSlave` (the ``ap_start``/``ap_done`` +
      register file). This is the *defining* property; because a component's endpoints are populated
      by the subclass ``__post_init__`` **after** the ``HwComponent`` super-chain runs (the same
      construction-ordering limit :class:`~waveflow.hw.hw_composite.CompositeComp` hits with its
      children), it is not construction-checked here — it is enforced by the codegen path (a kernel
      with no regmap emits no ``s_axilite`` control interface).
    - Implements :meth:`on_start` — the entry the codegen path lowers as the kernel body.
    - Must **not** define ``run_iter`` — that is a *free-running* leaf's entry
      (:class:`~waveflow.hw.hw_freerun.FreeRunComp`); a host-activated leaf runs once per trigger, not
      continuously. Enforced at class-definition time (:meth:`__init_subclass__`).
    """

    #: The invocation control protocol (``ap_ctrl_hs`` / ``ap_start``-``ap_done``).  Declared for
    #: intent; ``control_mode`` is not yet consumed by the pragma emitter (that rides with a real
    #: consumer later — see ``plans/exec_model_classes.md``), so this changes no generated output.
    control_mode: ClassVar[ControlMode] = ControlMode.PER_INVOCATION

    #: The codegen targets that **exist for this kind** — a host-activated leaf is the DUT of
    #: Flow 1 (``ap_ctrl_hs`` + ``s_axilite``), and of no other flow.
    #:
    #: Deliberately **not** ``supported_targets``: "supported" would read as a guarantee, sneaking
    #: synthesizability back onto the class — the very thing removing ``SynthComp`` was meant to stop
    #: (``docs/guide/components/taxonomy.md``: *synthesizability is a codegen/usage axis, not a class
    #: fact*).  The class states the **kind**; :func:`~waveflow.build.codegen_check.check` states the
    #: **fitness** of a particular component.  Read by ``build/`` via ``getattr`` (house style, same as
    #: ``cpp_kernel_name`` / ``control_mode``).
    potential_targets: ClassVar[frozenset[str]] = frozenset({CONTROL_DRIVEN_KERNEL})

    #: Intent metadata: a host-activated leaf's body is ``on_start``.  The codegen dispatch is
    #: class-based (:func:`~waveflow.build.codegen_dispatch.codegen_path`); this states the same
    #: intent declaratively.
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

    @abstractmethod
    def on_start(self) -> ProcessGen[None]:
        """The kernel body: read the input regmap fields, compute, write the output fields, return.

        Launched once per ``ap_start`` trigger (the runtime clears/sets ``ap_done`` around it). May run
        synchronously (a pure combinational/scalar kernel returns ``None``) or yield SimPy events (a
        latency/stream model). Override this."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Invocation — one call, mirroring the generated C++ function call
    # ------------------------------------------------------------------

    def _invoke_fields(self) -> tuple[Any, list[str], list[str]]:
        """Derive the invocation regmap + input/output field names from the endpoints.

        Shared by :meth:`run_once` (synchronous) and :meth:`run_once_sim` (sim-driven) so the
        two invocation paths cannot drift.  Returns ``(regmap, inputs, outputs)`` where:

        - ``regmap`` is the component's :class:`~waveflow.hw.regmap.VitisRegMap` (from its
          :class:`~waveflow.hw.regmap.VitisRegMapMMIFSlave` endpoint);
        - ``inputs`` = host-writable fields (``RW`` / ``W``), declaration order — the positional
          args, matching :func:`~waveflow.build.hwgen.kernel_signature`;
        - ``outputs`` = host-readable fields (``R``), declaration order — the return value(s).

        The auto ``ap_*`` control regs are skipped.  Stream-bearing kernels (input
        ``StreamIFSlave`` / output ``StreamIFMaster``, e.g. ``poly``) need push/pop marshalling
        and raise :class:`NotImplementedError` — that is a follow-on.
        """
        from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
        from waveflow.hw.regmap import RegAccess, VitisRegMapMMIFSlave

        # 5a scope: pure regmap-scalar. Stream push/pop marshalling is a Phase-5b follow-on.
        if any(isinstance(v, (StreamIFSlave, StreamIFMaster)) for v in vars(self).values()):
            raise NotImplementedError(
                f"{type(self).__name__}.run_once: stream-bearing invocation is a Phase-5b follow-on; "
                f"only pure-regmap host-activated kernels are supported today."
            )
        slave = next(
            (v for v in vars(self).values() if isinstance(v, VitisRegMapMMIFSlave)), None)
        if slave is None:
            raise TypeError(
                f"{type(self).__name__}.run_once: no VitisRegMapMMIFSlave endpoint found — a "
                f"HostActivated must carry a regmap."
            )
        regmap = slave.regmap
        # Walk the fields in declaration order (== kernel-signature order), skipping the auto ap_* regs.
        app = [(n, f) for n, f in regmap._fields.items() if not f.is_vitis_auto]
        inputs = [n for n, f in app if f.access in (RegAccess.RW, RegAccess.W)]
        outputs = [n for n, f in app if f.access is RegAccess.R]
        return regmap, inputs, outputs

    def run_once(self, *args: Any) -> Any:
        """Invoke this kernel **once** and return its output(s) — the Python mirror of the C++ call.

        A host-activated kernel's C++ realization *is* a function — e.g. ``simp_fun(x, a, b, y)`` —
        so its invocation is one call.  ``run_once`` gives the Python side the same shape::

            z = dut.run_once(x, a, b)      # vs. regmap.set(...); <invoke>; regmap.get(...)

        **Signature = the kernel signature.**  Inputs and outputs are derived from the *endpoints*,
        the same source :func:`~waveflow.build.hwgen.kernel_signature` uses, so they cannot drift:

        - **inputs** = the host-writable regmap fields (``RW`` / ``W``), in declaration order — the
          positional ``args``;
        - **outputs** = the host-readable regmap fields (``R``), in declaration order — the return.

        Argument order matches the kernel-signature endpoint order exactly, so a testbench's
        ``run_once(...)`` lowers 1:1 to the generated C++ call (Phase 5b).  Returns the single output
        value when there is one, else a tuple in signature order.

        The invocation itself reuses the kernel body (:meth:`on_start`) — the same entry the
        ``ap_start`` launch path (``VitisRegMapMMIFSlave._launch``) runs — rather than re-implementing
        it; ``run_once`` only wraps it with the endpoint-derived arg/return marshalling.

        **Scope (Phase 5a): pure regmap-scalar** (``simp_fun``-style).  Stream-bearing kernels
        (input ``StreamIFSlave`` / output ``StreamIFMaster``, e.g. ``poly``) need push/pop marshalling
        and are a follow-on — they raise :class:`NotImplementedError` here.  A ``on_start`` that yields
        (a latency/stream model) likewise needs a sim-driven path — use :meth:`run_once_sim`.
        """
        regmap, inputs, outputs = self._invoke_fields()
        if len(args) != len(inputs):
            raise TypeError(
                f"{type(self).__name__}.run_once() takes {len(inputs)} input(s) {inputs}, "
                f"got {len(args)}"
            )
        for name, value in zip(inputs, args):
            regmap.set(name, value)
        result = self.on_start()      # the kernel body — the _launch core, not re-implemented
        if result is not None:
            raise NotImplementedError(
                f"{type(self).__name__}.run_once: on_start yields (a generator) — a sim-driven "
                f"invocation is a follow-on; use run_once_sim (drive it inside a Simulation)."
            )
        outs = [regmap.get(name) for name in outputs]
        return outs[0] if len(outs) == 1 else tuple(outs)

    def run_once_sim(self, *args: Any) -> ProcessGen[Any]:
        """Sim-driven mirror of :meth:`run_once`: drive ``on_start`` through SimPy so the caller
        can **time** the invocation::

            t0 = self.now
            y  = yield from dut.run_once_sim(x, a, b)   # advances the clock through on_start
            t1 = self.now                               # total transaction latency

        Same endpoint-derived signature as :meth:`run_once` (see :meth:`_invoke_fields`), so the two
        cannot drift.  Works whether ``on_start`` yields (a latency/stream model — this drives it and
        advances the clock) or is synchronous (``on_start`` returns ``None`` — the ``yield from`` is
        skipped, so this behaves like :meth:`run_once`).  Must be driven inside a running
        :class:`~waveflow.simulation.simulation.Simulation` (it is a generator).
        """
        regmap, inputs, outputs = self._invoke_fields()
        if len(args) != len(inputs):
            raise TypeError(
                f"{type(self).__name__}.run_once_sim() takes {len(inputs)} input(s) {inputs}, "
                f"got {len(args)}"
            )
        for name, value in zip(inputs, args):
            regmap.set(name, value)
        result = self.on_start()      # the kernel body — the _launch core, not re-implemented
        if result is not None:
            yield from result         # drives a yielding on_start; advances the clock
        outs = [regmap.get(name) for name in outputs]
        return outs[0] if len(outs) == 1 else tuple(outs)

    #: ``z = dut(x, a, b)`` — the more literal spelling of :meth:`run_once`.
    __call__ = run_once
