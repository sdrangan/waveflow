"""report.py — :class:`CpuReport`, what a run of a :class:`~waveflow.cpu.processor.Processor` cost.

Four axes, as the design-space exploration needs them: **timing** (latency, queueing delay,
utilization), **energy** (each task's dynamic energy plus every core's static power over the run),
**footprint** (code bytes, the largest working set, the cache level it lives in) and — per
configuration rather than per run — **area** (:mod:`waveflow.cpu.area`).

Confidence is computed **here**, when a report is asked for, and never on the simulation's hot path:
evaluating a model's confidence costs about as much as running the task, and a run of a hundred
thousand tasks needs it once per distinct feature point, not once per task.  A function's reported
confidence is its *weakest* call's — the level a decision built on its numbers can rely on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from waveflow.calib.confidence import Confidence, ConfidenceLevel
from waveflow.cpu.task import cache_regime

if TYPE_CHECKING:
    from waveflow.cpu.config import CpuConfig, CycleCost
    from waveflow.cpu.task import TaskRecord

#: pJ per (mW x s): 1 mW for 1 s is 1e-3 J, which is 1e9 pJ.
PJ_PER_MW_S = 1e9


def cost_confidence(cost: CycleCost, feats: dict) -> Confidence:
    """The confidence of *cost* at *feats*; a bare number is a default, so ``UNCALIBRATED``."""
    if isinstance(cost, (int, float)):
        return Confidence(
            level=ConfidenceLevel.UNCALIBRATED,
            facts={"summary": f"a fixed cost of {float(cost):g}, not a model"},
        )
    return cost.confidence_feat(feats)


def _weaker(a: Confidence | None, b: Confidence) -> Confidence:
    return b if a is None or b.level < a.level else a


@dataclass
class FunctionStats:
    """Per-function totals over the finished calls."""

    name: str
    n: int = 0
    cycles: float = 0.0
    latency_sum_s: float = 0.0
    latency_max_s: float = 0.0
    wait_sum_s: float = 0.0
    wait_max_s: float = 0.0
    #: Dynamic energy over all calls, pJ.
    energy_pj: float = 0.0
    #: Code bytes of the function (from calibration), when known.
    code_bytes: int | None = None
    #: The largest working set any call declared, bytes (``None`` when none did).
    ws_max: float | None = None
    #: The weakest call's cycle confidence (``None`` until the report fills it in).
    confidence: Confidence | None = None
    #: The weakest call's energy confidence (``None`` when the function has no energy model).
    energy_confidence: Confidence | None = None
    #: Where :attr:`ws_max` lives under the run's configuration: ``"l1"``, ``"l2"``, ``"dram"``.
    regime: str | None = None

    @property
    def latency_mean_s(self) -> float:
        return self.latency_sum_s / self.n if self.n else 0.0

    @property
    def wait_mean_s(self) -> float:
        """Mean queueing delay."""
        return self.wait_sum_s / self.n if self.n else 0.0


@dataclass
class CpuReport:
    """Utilization, latency, queueing, energy, footprint and confidence for one processor run."""

    config_name: str
    elapsed_s: float
    core_busy_s: list[float]
    functions: dict[str, FunctionStats] = field(default_factory=dict)
    records: list[TaskRecord] = field(default_factory=list)
    #: Static power of one core, mW, as the run was configured.
    static_power_mw: float = 0.0

    @property
    def utilization(self) -> list[float]:
        """Per core: busy time over elapsed time (0 for a zero-length run)."""
        if self.elapsed_s <= 0:
            return [0.0 for _ in self.core_busy_s]
        return [b / self.elapsed_s for b in self.core_busy_s]

    @property
    def dynamic_pj(self) -> float:
        """Dynamic energy of every finished task, pJ."""
        return sum(st.energy_pj for st in self.functions.values())

    @property
    def static_pj(self) -> float:
        """Every core's static power over the elapsed time, pJ."""
        n_cores = len(self.core_busy_s)
        return n_cores * self.static_power_mw * self.elapsed_s * PJ_PER_MW_S

    @property
    def total_pj(self) -> float:
        return self.dynamic_pj + self.static_pj


def build_report(
    config_name: str,
    elapsed_s: float,
    core_busy_s: list[float],
    records: list[TaskRecord],
    *,
    static_power_mw: float = 0.0,
    config: CpuConfig | None = None,
) -> CpuReport:
    """Fold *records* into per-function stats, computing each distinct confidence once."""
    report = CpuReport(
        config_name,
        elapsed_s,
        list(core_busy_s),
        records=list(records),
        static_power_mw=static_power_mw,
    )
    seen: dict[tuple, Confidence] = {}

    def conf_of(cost, feats) -> Confidence:
        key = (id(cost), tuple(sorted(feats.items())))
        c = seen.get(key)
        if c is None:
            c = seen[key] = cost_confidence(cost, feats)
        return c

    for rec in records:
        st = report.functions.get(rec.name)
        if st is None:
            st = report.functions[rec.name] = FunctionStats(rec.name)
        st.n += 1
        st.cycles += rec.cycles
        st.energy_pj += rec.energy_pj
        st.latency_sum_s += rec.latency_s
        st.latency_max_s = max(st.latency_max_s, rec.latency_s)
        st.wait_sum_s += rec.wait_s
        st.wait_max_s = max(st.wait_max_s, rec.wait_s)
        if "ws" in rec.feats:
            ws = float(rec.feats["ws"])
            st.ws_max = ws if st.ws_max is None else max(st.ws_max, ws)
        func = rec.func
        if func is None:
            continue
        st.code_bytes = func.code_bytes
        st.confidence = _weaker(st.confidence, conf_of(func.cycles, rec.feats))
        if func.energy_pj is not None:
            st.energy_confidence = _weaker(
                st.energy_confidence, conf_of(func.energy_pj, rec.feats)
            )
    if config is not None:
        for st in report.functions.values():
            if st.ws_max is not None:
                st.regime = cache_regime(st.ws_max, config)
    return report
