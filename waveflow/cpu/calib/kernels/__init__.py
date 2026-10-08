"""kernels — the calibration suite: C programs and their bit-exact Python twins.

Three families cover the kinds of code a general-purpose core runs in a signal-processing system,
plus the scheduler's overheads:

* ``sched_ops`` — branchy control code (a micro-scheduler's task-group operations);
* ``cdot_q15`` — compute-bound streaming arithmetic (a Q15 complex dot product);
* ``gather_hist`` — memory-bound indexed access, its working set swept across L1, L2 and DRAM;
* ``ctx_switch``, ``dispatch`` — the context switch and the dequeue-and-call (``swapcontext`` beside
  them, for information only).

Each :class:`Kernel` names its C source, its twin, the argument names the program and the twin share,
the **counters** the cost model reads, and its working set.  The C program prints one JSON line; the
twin returns the same dict.  A twin is also the function a simulation runs: it is what a
:class:`~waveflow.cpu.task.SwFunction` built by :meth:`Kernel.sw_function` calls.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from waveflow.cpu.calib.kernels import numeric
from waveflow.cpu.calib.kernels.sched_ops import sched_ops
from waveflow.cpu.config import CycleCost
from waveflow.cpu.task import SwFunction

#: Where the C sources and ``wf_kernel.h`` live.
KERNEL_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Kernel:
    """One calibration kernel."""

    name: str
    twin: Callable[..., dict]
    #: The argument names the C program takes as ``key=value`` and the twin as keywords.
    args: tuple[str, ...]
    #: Output keys that are work counters: the cost model's features.
    counters: tuple[str, ...]
    #: Bytes the measured call touches, from the output (``None`` when negligible).
    working_set: Callable[[Mapping[str, Any]], float] | None = None
    #: Small points the twin test checks against the host build.
    smoke: tuple[dict, ...] = field(default_factory=tuple)
    #: Measured for information only; never a platform cost.
    informational: bool = False
    #: The measured code's symbols in the static binary; their summed sizes are its code bytes.
    symbols: tuple[str, ...] = ()

    @property
    def source(self) -> Path:
        return KERNEL_DIR / f"{self.name}.c"

    def argv(self, point: Mapping[str, Any]) -> list[str]:
        """The program's ``key=value`` arguments for *point*."""
        return [f"{k}={point[k]}" for k in self.args if k in point]

    def run_twin(self, point: Mapping[str, Any]) -> dict:
        return self.twin(**{k: point[k] for k in self.args if k in point})

    def counters_of(self, out: Mapping[str, Any]) -> dict[str, float]:
        return {k: float(out[k]) for k in self.counters}

    def sw_function(self, cycles: CycleCost, **kw: Any) -> SwFunction:
        """A :class:`SwFunction` that runs the twin and reports its counters."""
        ws = self.working_set

        def fn(**point: Any) -> tuple[dict, dict[str, float]]:
            out = self.run_twin(point)
            return out, self.counters_of(out)

        return SwFunction(
            name=self.name,
            fn=fn,
            cycles=cycles,
            working_set=(lambda c: ws(c)) if ws is not None else None,
            **kw,
        )


KERNELS: dict[str, Kernel] = {
    k.name: k
    for k in (
        Kernel(
            name="sched_ops",
            symbols=("run_op", "tg_insert", "tg_remove", "tg_sort"),
            twin=sched_ops,
            args=("op", "n", "seed"),
            counters=("n_tasks", "n_scanned", "n_moved"),
            working_set=lambda o: 8.0 * (o["n_tasks"] + 1),
            smoke=tuple(
                {"op": op, "n": n, "seed": s}
                for op in ("add", "delete", "reprio", "sort")
                for n, s in ((1, 3), (10, 3), (57, 11), (200, 5))
            )
            + ({"op": "add", "n": 0, "seed": 1},),
        ),
        Kernel(
            name="cdot_q15",
            symbols=("cdot",),
            twin=numeric.cdot_q15,
            args=("n", "seed"),
            counters=("n",),
            working_set=lambda o: 8.0 * o["n"],
            smoke=tuple(
                {"n": n, "seed": s} for n, s in ((0, 1), (1, 2), (100, 5), (4096, 9))
            ),
        ),
        Kernel(
            name="gather_hist",
            symbols=("gather",),
            twin=numeric.gather_hist,
            args=("n", "m", "seed"),
            counters=("n", "m"),
            working_set=lambda o: 4.0 * o["m"],
            smoke=tuple(
                {"n": n, "m": m, "seed": s}
                for n, m, s in (
                    (0, 1, 1),
                    (1000, 37, 9),
                    (5000, 4096, 2),
                    (300, 100_000, 4),
                )
            ),
        ),
        Kernel(
            name="dispatch",
            symbols=("dispatch", "h0", "h1", "h2"),
            twin=numeric.dispatch,
            args=("n", "seed"),
            counters=("n_dispatch",),
            smoke=tuple({"n": n, "seed": s} for n, s in ((0, 1), (50, 2), (1000, 7))),
        ),
        Kernel(
            name="ctx_switch",
            symbols=("pingpong", "wf_switch", "co_entry"),
            twin=numeric.ctx_switch,
            args=("k",),
            counters=("n_switches",),
            smoke=tuple({"k": k} for k in (0, 1, 5, 100)),
        ),
        Kernel(
            name="swapcontext",
            symbols=("pingpong", "co_entry"),
            twin=numeric.swapcontext,
            args=("k",),
            counters=("n_switches",),
            smoke=tuple({"k": k} for k in (0, 1, 5)),
            informational=True,
        ),
    )
}

#: The empty measured region: the markers' own cost, subtracted from every point.  Not a kernel of
#: the suite (it has no counters and no model), so it is kept out of :data:`KERNELS`.
EMPTY = Kernel(name="empty", twin=numeric.empty, args=(), counters=(), smoke=({},))

__all__ = ["EMPTY", "KERNELS", "KERNEL_DIR", "Kernel"]
