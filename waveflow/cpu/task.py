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

#: How many distinct points one function's price memo holds before it starts afresh.
PRICE_MEMO_MAX = 65536


def eval_cost(cost: CycleCost, feats: Mapping[str, float]) -> float:
    """*cost* at *feats*: a number is itself, a model is its prediction, never below zero."""
    if isinstance(cost, (int, float)):
        return max(0.0, float(cost))
    return max(0.0, float(cost.predict_feat(feats)))


#: The cycle-valued use of :func:`eval_cost`, named for what the processor asks it.
eval_cycles = eval_cost


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
    #: :meth:`price`'s memo: ``key -> (features, cycles, energy)``.
    _prices: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    #: What the memo's prices were computed with -- the models and the working set themselves, held
    #: so that a replaced one is noticed by identity (an ``id`` alone could be reused).
    _priced_with: tuple = field(default=(), init=False, repr=False, compare=False)

    def features(
        self, counters: Mapping[str, float], config: CpuConfig
    ) -> dict[str, float]:
        """The model inputs for one call: its counters, plus regime features when known."""
        feats = dict(counters)
        if self.working_set is not None:
            feats.update(regime_features(self.working_set(counters), config))
        return feats

    def price(
        self, counters: Mapping[str, float], config: CpuConfig
    ) -> tuple[dict[str, float], float, float | None]:
        """One call's features, compute cycles and dynamic energy (``None`` without a model).

        Memoized: a price is a pure function of the counters -- and, with a working set, of the
        configuration's cache sizes -- and of the models attached, so a point the function has
        already been priced at costs a lookup, not two model evaluations.  Control code repeats its
        points (a scheduler's counters are small integers), which is where the processor model spent
        most of its time.  Replacing ``cycles``, ``energy_pj`` or ``working_set`` starts the memo
        afresh.  Two things it cannot see: a model refitted in place, and a ``working_set`` that
        depends on anything but the counters -- neither is allowed while the function is in use.
        The features are returned as a fresh dict, as :meth:`features` returns them.
        """
        used = (self.cycles, self.energy_pj, self.working_set)
        if len(used) != len(self._priced_with) or any(
            a is not b for a, b in zip(used, self._priced_with)
        ):
            self._prices.clear()
            self._priced_with = used
        try:
            key: tuple = (tuple(counters.items()),)
            if self.working_set is not None:
                key += (config.l1d_bytes, config.l2_bytes)
            hit = self._prices.get(key)
        except TypeError:  # an unhashable counter: price it every time
            key, hit = (), None
        if hit is None:
            feats = self.features(counters, config)
            cycles = eval_cycles(self.cycles, feats)
            energy = (
                None if self.energy_pj is None else eval_cost(self.energy_pj, feats)
            )
            hit = (feats, cycles, energy)
            if key:
                if len(self._prices) >= PRICE_MEMO_MAX:
                    self._prices.clear()
                self._prices[key] = hit
        return dict(hit[0]), hit[1], hit[2]

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
    #: Dynamic energy of the task's compute, in pJ (0 when its function has no energy model).
    energy_pj: float = 0.0
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
