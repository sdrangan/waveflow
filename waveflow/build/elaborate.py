"""elaborate.py — the elaboration contract.

Codegen depends on a component's *(class, compile-time parameters)*, never on a
specific runtime instance.  :func:`elaborate` is the single entry that builds a
component purely to read its **structure** — endpoints, sub-components,
interfaces, and boundary — HDL-style *elaboration*.

The contract: a component's structure is a **pure function of its
``HwParam``/``HwConst`` parameters**; ``name``, ``sim``, and runtime data are
elaboration context and must not affect it.  When that holds, codegen
``= elaborate(class, param_set) → structure → C++``, one output per param-set,
instance-independent — any real instance with those params matches the
generated C++ by construction.

Precedent — :class:`~waveflow.hw.dataschema.DataSchema` gets this *for free*
(its structure *is* class attributes + classmethods, read with no
instantiation).  :class:`~waveflow.hw.hw_component.HwComponent` builds structure
*imperatively* in ``__post_init__``, so this module **enforces by contract**
(:func:`assert_param_pure`) what DataSchema gets by construction.

Before this module the instantiation was a scattered, sim-coupled,
*unenforced* trick: ``cls(name="_codegen", sim=Simulation())`` appeared at three
codegen sites.  They now all route here.
"""
from __future__ import annotations

from typing import Any

import simpy

from waveflow.simulation.simulation import Simulation


class ElabContext(Simulation):
    """A sim-free stand-in for :class:`Simulation`, used only to *elaborate*.

    Codegen never runs the sim — it constructs a component solely to read its
    ports.  This context supplies exactly what construction needs from the
    ``SimObj`` lifecycle: :meth:`add_obj` (inherited) plus a SimPy ``env`` for
    the resources endpoints allocate in their ``__post_init__``.  It is never
    driven by ``run_sim()``.

    The ``env`` is created **lazily**, so a component that declares its
    structure without touching the sim never allocates one — reinforcing that
    structure is independent of the simulation.  ``ElabContext`` *is a*
    ``Simulation`` (subclass), so construction behaves byte-for-byte as it did
    with a plain ``Simulation()``.
    """

    #: Marker so callers/tests can distinguish an elaboration context from a
    #: real, runnable :class:`Simulation`.
    is_elaboration: bool = True

    def __init__(self) -> None:
        # Deliberately does not call ``super().__init__`` — that eagerly builds
        # a ``simpy.Environment``.  We defer it to :attr:`env` so a truly
        # sim-free elaboration allocates nothing.
        self._sim_objs: list = []
        self._env: simpy.Environment | None = None

    @property
    def env(self) -> simpy.Environment:
        if self._env is None:
            self._env = simpy.Environment()
        return self._env

    @env.setter
    def env(self, value: simpy.Environment) -> None:
        self._env = value

    def run_sim(self) -> None:  # pragma: no cover - defensive
        raise RuntimeError(
            "ElabContext is an elaboration-only stand-in for Simulation; it "
            "must not be run.  Build a real Simulation() to run a component."
        )


def elaborate(
    comp_class: type,
    params: dict[str, Any] | None = None,
    *,
    name: str = "_codegen",
) -> Any:
    """Elaborate *comp_class* with *params* and return the built structure.

    Constructs the component in a fresh :class:`ElabContext` with the given
    compile-time ``HwParam``/``HwConst`` overrides applied through the normal
    ``__init__`` path (no immutability bypass).  The returned instance is a
    faithful structural stand-in for any real instance with the same params —
    that is the elaboration contract, enforced by :func:`assert_param_pure`.

    This is the single instantiation site for codegen: the scattered
    ``cls(name="_codegen", sim=Simulation())`` calls all route here.
    """
    overrides = dict(params or {})
    return comp_class(name=name, sim=ElabContext(), **overrides)
