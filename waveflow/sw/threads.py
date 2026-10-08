"""threads.py — software threads, and the channels between them (``plans/host_runtime.md``).

A :class:`SwHost` is the software side of a system: a module outside the synthesized cut that owns
what software physically has -- a bus master and the interrupt lines it waits on -- and runs one or
more :class:`SwThread`\\ s.  In pysim a thread is a SimPy process: **code between waits runs in zero
simulated time**, and everything it waits on is a SimPy event.  Its C++ twin under XSI is a fiber with
the same primitives (``plans/host_runtime.md``, the API table).

Every blocking primitive is a generator, used with ``yield from``, as every endpoint call already is:

=================================  ===========================================================
``yield from irq.wait()``          an interrupt line high (at once, without yielding, if it is)
``k = yield from wait_any(a, b)``  the first of several to be ready; returns its index
``yield from self.compute(n)``     *n* host clock cycles of software execution time
``yield from ev.wait()``           a :class:`SwEvent` set
``yield from sem.acquire()``       a :class:`SwSemaphore` / :class:`SwLock` count available
``yield from q.put(m)`` / ``get``  a :class:`SwQueue` with room / with a message
=================================  ===========================================================

**Waitables.**  ``wait_any`` takes anything with ``_sw_ready()`` (is it satisfied now?) and
``_sw_change()`` (a SimPy event that fires when it may have become so): an ``IrqIFSink``, a
:class:`SwEvent`, a :class:`SwSemaphore` (a count > 0) or a :class:`SwQueue` (a message waiting).  It
waits only -- it does not take the semaphore or the message.

**Determinism.**  Threads of one host are SimPy processes in one environment, so they interleave at
their waits exactly as SimPy orders events; the C++ scheduler resumes them in the same order.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar

import simpy

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import DynParam, HwModule
from waveflow.simulation.simobj import ProcessGen


# ---------------------------------------------------------------------------
# Waiting on several things
# ---------------------------------------------------------------------------

def wait_any(*waitables) -> ProcessGen[int]:
    """Wait until any of *waitables* is ready; return the index of the first ready one, in argument
    order.  Returns at once, without yielding, if one already is."""
    if not waitables:
        raise ValueError("wait_any() needs at least one thing to wait on")
    env = _env_of(waitables[0])
    while True:
        for k, w in enumerate(waitables):
            if w._sw_ready():
                return k
        yield simpy.AnyOf(env, [w._sw_change() for w in waitables])


def _env_of(w) -> simpy.Environment:
    env = getattr(w, "env", None) or getattr(getattr(w, "owner", None), "env", None)
    if env is None:
        raise TypeError(f"{w!r} is not a waitable this runtime knows (no simulation environment)")
    return env


class _Waitable:
    """A software channel's readiness, and the event that wakes its waiters when it changes."""

    def __init__(self, owner, name: str = "") -> None:
        self.owner = owner              # any SimObj: the channel lives in its environment
        self.name = name
        self._change: simpy.Event | None = None

    @property
    def env(self) -> simpy.Environment:
        return self.owner.env

    def _sw_change(self) -> simpy.Event:
        if self._change is None or self._change.triggered:
            self._change = self.env.event()
        return self._change

    def _changed(self) -> None:
        if self._change is not None and not self._change.triggered:
            self._change.succeed()

    def _sw_ready(self) -> bool:                       # pragma: no cover - every channel overrides
        raise NotImplementedError

    def _wait_ready(self) -> ProcessGen[None]:
        while not self._sw_ready():
            yield self._sw_change()


# ---------------------------------------------------------------------------
# Channels between threads
# ---------------------------------------------------------------------------

class SwEvent(_Waitable):
    """A flag threads wait on: ``set()`` wakes every waiter and stays set until ``clear()``.
    (A level, like an interrupt line -- a thread that waits on a set event returns at once.)"""

    def __init__(self, owner, name: str = "") -> None:
        super().__init__(owner, name)
        self.is_set = False

    def set(self) -> None:
        self.is_set = True
        self._changed()

    def clear(self) -> None:
        self.is_set = False

    def _sw_ready(self) -> bool:
        return self.is_set

    def wait(self) -> ProcessGen[None]:
        yield from self._wait_ready()


class SwSemaphore(_Waitable):
    """A counting semaphore: ``acquire()`` waits for a count and takes it, ``release()`` returns one.
    Waiters are served in the order they wake (SimPy's order), never by priority."""

    def __init__(self, owner, count: int, name: str = "") -> None:
        super().__init__(owner, name)
        if int(count) < 0:
            raise ValueError(f"{name or 'semaphore'}: a count is >= 0, got {count}")
        self.count = int(count)

    def _sw_ready(self) -> bool:
        return self.count > 0

    def acquire(self) -> ProcessGen[None]:
        yield from self._wait_ready()
        self.count -= 1

    def release(self) -> None:
        self.count += 1
        self._changed()


class SwLock(SwSemaphore):
    """A mutex: a semaphore of one.  Releasing a lock that is not held is an error (ownership is not
    tracked: threads are cooperative, so a release can only come from code the host wrote)."""

    def __init__(self, owner, name: str = "") -> None:
        super().__init__(owner, 1, name)

    def release(self) -> None:
        if self.count >= 1:
            raise RuntimeError(f"{self.name or 'lock'}: released while not held")
        super().release()


class SwQueue(_Waitable):
    """A software message queue between threads: ``put`` waits for room (*capacity* messages, or
    unbounded), ``get`` waits for a message.  As a waitable it is ready when a message is waiting."""

    def __init__(self, owner, capacity: int | None = None, name: str = "") -> None:
        super().__init__(owner, name)
        self.capacity = capacity
        self.items: deque = deque()
        self._room = _RoomWaitable(self)

    def _sw_ready(self) -> bool:
        return bool(self.items)

    def put(self, msg: Any) -> ProcessGen[None]:
        yield from self._room._wait_ready()
        self.items.append(msg)
        self._changed()

    def get(self) -> ProcessGen[Any]:
        yield from self._wait_ready()
        msg = self.items.popleft()
        self._room._changed()
        return msg

    @property
    def room(self) -> "_RoomWaitable":
        """The queue's *room*, as a waitable for ``wait_any``."""
        return self._room


class _RoomWaitable(_Waitable):
    def __init__(self, q: SwQueue) -> None:
        super().__init__(q.owner, f"{q.name}.room")
        self.q = q

    def _sw_ready(self) -> bool:
        return self.q.capacity is None or len(self.q.items) < self.q.capacity


# ---------------------------------------------------------------------------
# Threads, and the host that runs them
# ---------------------------------------------------------------------------

@dataclass
class SwThread:
    """One running software thread: a SimPy process, with a name and a way to wait for its end."""

    name: str
    process: simpy.events.Process

    @property
    def done(self) -> bool:
        return not self.process.is_alive

    def join(self) -> ProcessGen[Any]:
        """Wait for the thread to finish; returns what its body returned."""
        return (yield self.process)


@dataclass
class SwHost(HwModule):
    """The software side of a system: owns a bus master and interrupt inputs, runs threads.

    A subclass creates what software physically has with :meth:`add_bus_master` and :meth:`add_irq`
    (each registered as an endpoint, so a C++ realization can bind to it), leaves its program endpoints
    to the system wiring, and writes ``main()`` -- the first thread.  More threads are started with
    :meth:`start`.  ``run_proc`` runs ``main``; a host whose run ends defines it to return when done.
    """

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: The scenario bundle both realizations run (a host writes it with its own ``write_scenario``);
    #: empty: the host builds its scenario in memory.
    scenario: DynParam[str] = ""
    #: Where each endpoint's trace is dumped after the run; empty: not dumped.
    trace_dir: DynParam[str] = ""

    #: The C++ realization: the class name, and its header -- beside the module defining the host.
    #: The class derives from the generated ``<Host>_endpoints`` (``waveflow/build/sw_host_gen.py``).
    cpp_model: ClassVar[str | None] = None
    cpp_header: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.threads: list[SwThread] = []

    # -- what software physically has --------------------------------------------------------------

    def add_bus_master(self, attr: str, **kw):
        """Create this host's bus master (an ``MMIFMaster``) as attribute *attr*, and register it."""
        from waveflow.hw.memif import MMIFMaster
        ep = MMIFMaster(name=f"{self.name}_{attr}", sim=self.sim, **kw)
        setattr(self, attr, ep)
        self.add_endpoint(ep)
        return ep

    def add_irq(self, attr: str):
        """Create the host's end of an interrupt line (an ``IrqIFSink``) as *attr*, and register it."""
        from waveflow.hw.irq import IrqIFSink
        ep = IrqIFSink(name=f"{self.name}_{attr}", sim=self.sim)
        setattr(self, attr, ep)
        self.add_endpoint(ep)
        return ep

    # -- threads --------------------------------------------------------------------------------------

    def start(self, body: Callable[..., ProcessGen[Any]], *args, name: str | None = None) -> SwThread:
        """Start a thread running ``body(*args)`` now; return it."""
        t = SwThread(name or getattr(body, "__name__", "thread"), self.process(body(*args)))
        self.threads.append(t)
        return t

    def main(self) -> ProcessGen[None]:  # pragma: no cover - a host overrides it
        raise NotImplementedError(f"{type(self).__name__} defines no main() thread")
        yield

    def run_proc(self) -> ProcessGen[None]:
        yield from self.main()

    # -- the scenario both realizations run ------------------------------------------------------------

    def scenario_bursts(self) -> list:  # pragma: no cover - a host with a C++ twin overrides it
        """The host's scenario as word messages, one burst per item -- the layout is the host's own."""
        raise NotImplementedError(f"{type(self).__name__} defines no scenario_bursts()")

    def write_scenario(self, path) -> None:
        """Write :meth:`scenario_bursts` as a burst bundle at *path* -- the file both realizations run
        (the C++ one always, the Python one when :attr:`scenario` names it)."""
        from waveflow.utils.burst_io import write_burst_bundle
        write_burst_bundle(self.scenario_bursts(), path)

    # -- traces --------------------------------------------------------------------------------------

    def post_sim(self) -> None:
        """With :attr:`trace_dir` set, dump every memory-mapped endpoint's trace -- what crossed it --
        as a burst bundle named after the endpoint's attribute, as the C++ realization does."""
        super().post_sim()
        if self.trace_dir:
            from pathlib import Path

            from waveflow.build.sw_host_gen import traced_endpoints
            from waveflow.hw.mm_host import write_trace
            for name, ep in traced_endpoints(self):
                write_trace(ep, Path(self.trace_dir) / name)

    # -- the C++ realization --------------------------------------------------------------------------

    def bfm_model(self):
        """The C++ twin (``cpp_model`` in ``cpp_header``), spanning the bus master and every interrupt
        input -- derived, so a host declares only the two names."""
        from waveflow.build.composite_gen import BfmModel
        from waveflow.build.hwcodegen import LoweringError
        from waveflow.build.sw_host_gen import host_ports

        if not self.cpp_model or not self.cpp_header:
            raise LoweringError(f"{type(self).__name__} names no C++ realization: set cpp_model and "
                                f"cpp_header on the class")
        return BfmModel(self.cpp_model, ports=host_ports(self), header=self.cpp_header)

    # -- software time ------------------------------------------------------------------------------

    def compute(self, cycles: float) -> ProcessGen[None]:
        """*cycles* host clock cycles of software execution time -- the only way code between waits
        takes simulated time."""
        if cycles < 0:
            raise ValueError(f"compute() takes cycles >= 0, got {cycles}")
        if cycles:
            yield self.timeout(cycles * self.clk.period)


__all__ = ["SwEvent", "SwHost", "SwLock", "SwQueue", "SwSemaphore", "SwThread", "wait_any"]
