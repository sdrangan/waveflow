"""task.py — :class:`SwFunction` (a piece of software and what it costs) and :class:`TaskRecord`.

The split that makes the model fast is the one ``TimingModel`` already uses for hardware: the
**function** runs in Python, all at once and in zero simulated time, and reports the *work it did* as
counters (items scanned, queue depth, …); the **cost model** turns those counters into cycles.  The
processor then charges the cycles in one timed event.  Nothing steps per instruction, and the Python
function stays the single source of truth for what the software computes.

Counters are the right features because the cost of control code is set by what it *did*, not by the
size of its input: a scheduler that finds its task at the head of a queue is cheap whatever the
queue's length.  A function that cannot say what it did can still run, with a fixed cost.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from waveflow.cpu.config import CpuConfig, CycleCost

if TYPE_CHECKING:
    from waveflow.calib.calib import CalibModel

#: What a software function returns: its result and the counters its cost is a function of.
SwResult = tuple[Any, Mapping[str, float]]


def eval_cycles(cost: CycleCost, feats: Mapping[str, float]) -> float:
    """Cycles for *cost* at *feats*: a number is itself, a model is its prediction (never below 0)."""
    if isinstance(cost, (int, float)):
        return float(cost)
    return max(0.0, float(cost.predict_feat(feats)))


def regime_features(ws_bytes: float, config: CpuConfig) -> dict[str, float]:
    """The cache-regime features of a working set against *config*'s caches.

    ``ws_over_l1`` and ``ws_over_l2`` are zero while the data fits and grow linearly past each
    boundary, so a linear cost model can bend at the two places a cache-bound kernel's cost does.
    """
    ws = float(ws_bytes)
    return {
        "ws": ws,
        "ws_over_l1": max(0.0, ws - config.l1d_bytes),
        "ws_over_l2": max(0.0, ws - config.l2_bytes),
    }


def cache_regime(ws_bytes: float, config: CpuConfig) -> str:
    """Where a working set lives: ``"l1"``, ``"l2"`` or ``"dram"`` (a boundary value fits)."""
    if ws_bytes <= config.l1d_bytes:
        return "l1"
    if ws_bytes <= config.l2_bytes:
        return "l2"
    return "dram"


@dataclass(kw_only=True)
class SwFunction:
    """A piece of software the processor runs, and the models that price it.

    *fn* is called with the task's arguments and returns ``(result, counters)``.  *cycles* maps the
    counters — plus the regime features, when *working_set* is given — to cycles; *energy_pj* maps the
    same features to dynamic energy in picojoules (pJ, not J: the calibration stack's exactness test
    is an absolute tolerance, and a joule-valued fit would always read as exact).
    """

    name: str
    fn: Callable[..., SwResult]
    cycles: CycleCost
    energy_pj: CalibModel | float | None = None
    #: Bytes the call touches, from its counters.  Drives the regime features and the footprint.
    working_set: Callable[[Mapping[str, float]], float] | None = None
    #: Bytes of code, normally filled from the calibration corpus (the kernel's symbol size).
    code_bytes: int | None = None

    def features(
        self, counters: Mapping[str, float], config: CpuConfig
    ) -> dict[str, float]:
        """The model inputs for one call: its counters, plus regime features when known."""
        feats = dict(counters)
        if self.working_set is not None:
            feats.update(regime_features(self.working_set(counters), config))
        return feats

    @classmethod
    def fixed(cls, name: str, cycles: CycleCost) -> SwFunction:
        """A function with no body and a fixed cost — the model's form of ``compute(cycles)``."""
        return cls(name=name, fn=lambda: (None, {}), cycles=cycles)


@dataclass(slots=True)
class TaskRecord:
    """What one execution of a :class:`SwFunction` cost, in simulated seconds and core cycles.

    Times are absolute simulation times.  ``busy_s`` is the time the task held a core — its compute,
    every switch it paid, and any switch cut short by a preemption — so ``wait_s`` (latency minus
    busy) is exactly the time it spent ready but not running: the queueing delay.
    """

    name: str
    prio: int
    t_submit: float
    t_start: float = -1.0
    t_end: float = -1.0
    #: The core that finished the task.
    core: int = -1
    #: Compute cycles the cost model charged (excluding switches).
    cycles: float = 0.0
    #: Switch cycles paid, including any cut short by a preemption.
    switch_cycles: float = 0.0
    busy_s: float = 0.0
    n_preempted: int = 0
    is_irq: bool = False
    #: The model inputs, kept so confidence can be computed when a report asks, not per task.
    feats: dict = field(default_factory=dict)
    func: SwFunction | None = None

    @property
    def latency_s(self) -> float:
        """Submit to completion."""
        return self.t_end - self.t_submit

    @property
    def wait_s(self) -> float:
        """Time spent ready but not running: the queueing delay."""
        return self.latency_s - self.busy_s
