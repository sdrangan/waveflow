from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Generator, TypeVar

from waveflow.named import NamedObject

import simpy

if TYPE_CHECKING:
    from .simulation import Simulation


_T = TypeVar('_T')
ProcessGen = Generator[simpy.events.Event, Any, _T]
ProcessFactory = Callable[[], ProcessGen[None]]


@dataclass(frozen=True)
class ActionRecord(object):
    """Timing record for one action invocation."""

    name: str
    start: float
    end: float


@dataclass(frozen=True)
class ActionOverlap(object):
    """Represents an overlap between two actions on the same object."""

    previous: ActionRecord
    current: ActionRecord


@dataclass
class SimObj(NamedObject):
    """
    Base class for simulation entities built on top of ``simpy``.

    A ``SimObj`` owns one or more concurrent processes registered with a shared
    ``simpy.Environment``.  Subclasses typically register long-running loops
    that consume and produce transactions.

    Each ``SimObj`` registers itself with the provided :class:`Simulation`
    instance so that :meth:`Simulation.run_sim` can drive the standard
    three-phase lifecycle:

    * :meth:`pre_sim`  — setup / validation before the event loop starts
    * :meth:`run_proc` — optional SimPy generator process
    * :meth:`post_sim` — inspection / finalization after the event loop ends
    """

    sim: Simulation | None = None
    """Owning simulation.  The object borrows its environment and registers
    itself with *sim* during :meth:`__post_init__`."""

    track_action_overlaps: bool = True

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        """Initialize the process registry and register with the simulation."""
        super().__post_init__()
        if self.sim is None:
            # No explicit ``sim=`` — fall back to the ambient Simulation, if one is active
            # (a ``with sim.as_current():`` block, e.g. the runnable SeqTB harness).  This is
            # the only new sim-resolution path; explicit-``sim=`` construction is untouched, and
            # with no ambient sim set the original "sim must be provided" contract still holds.
            from .simulation import current_simulation
            ambient = current_simulation()
            if ambient is None:
                raise ValueError("sim must be provided.")
            self.sim = ambient

        self.processes: list[simpy.events.Process] = []
        self._process_factories: list[tuple[str, ProcessFactory]] = []
        self.action_history: list[ActionRecord] = []
        self.action_overlaps: list[ActionOverlap] = []

        self.sim.add_obj(self)

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def pre_sim(self) -> None:
        """Called once before the simulation event loop starts.

        Override to perform per-object setup, validation, or initial event
        scheduling.  The default implementation is a no-op.
        """

    def run_proc(self) -> ProcessGen[None] | None:
        """Return a SimPy generator process to schedule, or ``None``.

        Returning ``None`` (the default) marks the object as *passive*; it
        participates only through :meth:`pre_sim` / :meth:`post_sim`.
        Active objects should override this method and ``yield`` SimPy events.
        """
        return None

    def post_sim(self) -> None:
        """Called once after the simulation event loop ends.

        Override to collect statistics, assert invariants, or emit reports.
        The default implementation is a no-op.
        """

    def error_cleanup(self) -> None:
        """Called when the simulation terminates with an error.

        Override to release resources (close files, etc.) that must be
        cleaned up regardless of whether ``run_proc`` completed.  Must be
        safe to call at any point, even if ``run_proc`` never started.
        The default implementation is a no-op.
        """

    # ------------------------------------------------------------------
    # Environment helpers
    # ------------------------------------------------------------------

    @property
    def env(self) -> simpy.Environment:
        """The shared simulation environment."""
        return self.sim.env

    @property
    def now(self) -> float:
        """Current simulation timestamp."""
        return float(self.env.now)

    def timeout(self, delay: float) -> simpy.events.Timeout:
        """Convenience wrapper around ``env.timeout``.

        Marked ``@sim_only`` (applied at module end to avoid a circular import): a
        timeout models simulation latency and has no hardware meaning, so the kernel
        extractor strips ``yield self.timeout(...)`` from an extracted body (C-sim is
        untimed).


        Parameters
        ----------
        delay : float
            Time to wait in seconds. Must be non-negative.

        Example
        -------
        ``yield self.timeout(5)  # wait for 5 seconds``
        """
        if delay < 0:
            raise ValueError("delay must be non-negative.")
        return self.env.timeout(delay)

    def event(self) -> simpy.events.Event:
        """Create a plain SimPy event in the shared environment."""
        return self.env.event()

    def process(self, generator: ProcessGen[Any]) -> simpy.events.Process:
        """
        Register and start a process generator in the environment.

        Parameters
        ----------
        generator : Generator
            A SimPy process generator yielding events.
        """
        proc = self.env.process(generator)
        self.processes.append(proc)
        return proc

    def add_process(self, name: str, factory: ProcessFactory, autostart: bool = True) -> None:
        """
        Register a named process factory.

        Parameters
        ----------
        name : str
            Name for introspection/debugging.
        factory : Callable[[], Generator]
            Zero-argument callable that returns a process generator.
        autostart : bool
            If ``True``, the process is started immediately.
        """
        if not name:
            raise ValueError("name must be non-empty.")
        self._process_factories.append((name, factory))
        if autostart:
            self.process(factory())

    def start_registered_processes(self) -> None:
        """Start all registered process factories."""
        for _, factory in self._process_factories:
            self.process(factory())

    def transaction_queue(self, capacity: int | float = float("inf")) -> simpy.Store:
        """
        Create a transaction queue associated with this simulation environment.

        Parameters
        ----------
        capacity : int | float
            Queue capacity. Defaults to unbounded.
        """
        return simpy.Store(self.env, capacity=capacity)

    def resource(self, capacity: int = 1) -> simpy.Resource:
        """Create a shared resource primitive tied to this environment."""
        if capacity <= 0:
            raise ValueError("capacity must be positive.")
        return simpy.Resource(self.env, capacity=capacity)

    def container(self, capacity: float, init: float = 0.0) -> simpy.Container:
        """Create a level-based container tied to this environment."""
        return simpy.Container(self.env, capacity=capacity, init=init)

    def action(
        self,
        name: str,
        processing_delay: float = 0.0,
    ) -> ProcessGen[None]:
        """
        Track one action window and optionally model its latency.

        This method is intended to be yielded from inside SimPy processes:
        ``yield from self.action("decode", processing_delay=3)``.

        Parameters
        ----------
        name : str
            Action name.
        processing_delay : float
            Non-negative action delay.
        """
        if not name:
            raise ValueError("name must be non-empty.")
        if processing_delay < 0:
            raise ValueError("processing_delay must be non-negative.")

        start = self.now
        if processing_delay > 0:
            yield self.timeout(processing_delay)
        end = self.now

        current = ActionRecord(name=name, start=start, end=end)
        self._record_action(current)
        return current

    def _record_action(self, current: ActionRecord) -> None:
        self.action_history.append(current)

        if not self.track_action_overlaps:
            return

        for prev in self.action_history[:-1]:
            if prev.end > current.start and current.end > prev.start:
                self.action_overlaps.append(ActionOverlap(previous=prev, current=current))

    def active_overlap_count(self) -> int:
        """Return the number of detected overlapping action windows."""
        return len(self.action_overlaps)

    def clear_action_logs(self) -> None:
        """Clear collected action history and overlap records."""
        self.action_history.clear()
        self.action_overlaps.clear()


# ``SimObj.timeout`` is simulation-only latency: the kernel extractor must strip
# ``yield self.timeout(...)`` from an extracted body (``on_start`` / ``run_iter``),
# since C-sim is untimed.  The ``@sim_only`` marker is applied here — at module end,
# after ``SimObj`` is fully defined — rather than as an inline decorator, because
# ``waveflow.hw.synth`` pulls in ``waveflow.hw`` (which imports this module), so a
# top-of-file import would form a circular import.  ``synth`` itself has no deps, so
# by this point the marker imports cleanly.
from waveflow.hw.synth import sim_only as _sim_only  # noqa: E402

SimObj.timeout = _sim_only(SimObj.timeout)  # type: ignore[method-assign]
