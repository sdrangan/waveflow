"""waveflow.sw -- software threads: the host side of a system (``plans/host_runtime.md``).

Hardware modules are clocked and synthesized; software is **threads** that wait on events and on bus
transactions, and whose code between waits takes no simulated time (unless it says so, with
``compute``).  In pysim a :class:`SwThread` is a SimPy process; under XSI its C++ twin is a fiber.  The
primitives here have the same names and meaning on both sides.
"""
from waveflow.sw.threads import (
    SwEvent,
    SwHost,
    SwLock,
    SwQueue,
    SwSemaphore,
    SwThread,
    wait_any,
)

__all__ = ["SwEvent", "SwHost", "SwLock", "SwQueue", "SwSemaphore", "SwThread", "wait_any"]
