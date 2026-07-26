from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

import simpy


#: The ambient (current) :class:`Simulation`.  A ``SimObj`` constructed with no explicit
#: ``sim=`` picks this up (see :meth:`Simulation.as_current` and ``SimObj.__post_init__``),
#: so code that builds a component inside a running sim — e.g. a single-process ``SeqTB.main()``
#: doing ``dut = SimpFun()`` — need not thread ``sim=`` through.  ``None`` when no
#: simulation has entered :meth:`Simulation.as_current`, in which case a sim-less construction
#: still raises (behaviour is unchanged for explicit-``sim=`` callers and outside ``as_current``).
_current_simulation: contextvars.ContextVar["Simulation | None"] = contextvars.ContextVar(
    "waveflow_current_simulation", default=None,
)


def current_simulation() -> "Simulation | None":
    """Return the ambient :class:`Simulation` set by :meth:`Simulation.as_current`, or ``None``."""
    return _current_simulation.get()


class Simulation:
    """
    Runtime coordination object for a SimPy-based simulation.

    ``Simulation`` owns the ``simpy`` environment and the list of registered
    ``SimObj`` instances.  It orchestrates the standard three-phase lifecycle:

    1. ``pre_sim()``  — called on every registered object before the run.
    2. ``run_proc()`` — optional generator process; scheduled via
       ``env.process()`` when not ``None``.
    3. ``post_sim()`` — called on every registered object after the run.
    """

    def __init__(self) -> None:
        self.env: simpy.Environment = simpy.Environment()
        self._sim_objs: list = []

    @contextmanager
    def as_current(self) -> Iterator["Simulation"]:
        """Make this the ambient :class:`Simulation` for the duration of the ``with`` block.

        Inside the block, a ``SimObj`` (e.g. a :class:`~waveflow.hw.hw_module.HwModule`)
        constructed with no explicit ``sim=`` binds to *this* simulation instead of raising.
        Outside the block — and for any construction that still passes ``sim=`` — behaviour is
        unchanged.  Used by the runnable single-process ``SeqTB`` harness so ``main()`` can do a
        bare ``dut = SimpFun()`` that is identical to the codegen (no-sim) form::

            with sim.as_current():
                env.process(tb.main())
                sim.env.run()
        """
        token = _current_simulation.set(self)
        try:
            yield self
        finally:
            _current_simulation.reset(token)

    def add_obj(self, obj: object) -> None:
        """Register a ``SimObj`` with this simulation.

        Called automatically from ``SimObj.__init__`` so callers rarely need
        to invoke this directly.
        """
        self._sim_objs.append(obj)

    def run_sim(self) -> None:
        """Execute the full simulation lifecycle.

        Steps
        -----
        1. Call ``pre_sim()`` on all registered objects in registration order.
        2. Schedule each object's ``run_proc()`` generator as a SimPy process
           (objects whose ``run_proc()`` returns ``None`` are skipped).
        3. Advance the simulation via ``env.run()``.
        4. Call ``post_sim()`` on all registered objects in registration order.
        """
        for obj in self._sim_objs:
            obj.pre_sim()

        for obj in self._sim_objs:
            proc = obj.run_proc()
            if proc is not None:
                self.env.process(proc)

        try:
            self.env.run()
        except Exception:
            for obj in self._sim_objs:
                obj.error_cleanup()
            raise

        for obj in self._sim_objs:
            obj.post_sim()
