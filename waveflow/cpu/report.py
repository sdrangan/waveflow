"""report.py — :class:`CpuReport`, what a run of a :class:`~waveflow.cpu.processor.Processor` cost.

Confidence is computed **here**, when a report is asked for, and never on the simulation's hot path:
evaluating a model's confidence costs about as much as running the task, and a run of a hundred
thousand tasks needs it once per distinct feature point, not once per task.  A function's reported
confidence is its *weakest* call's — the level a decision built on its numbers can rely on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from waveflow.calib.confidence import Confidence, ConfidenceLevel

if TYPE_CHECKING:
    from waveflow.cpu.config import CycleCost
    from waveflow.cpu.task import TaskRecord


def cost_confidence(cost: CycleCost, feats: dict) -> Confidence:
    """The confidence of *cost* at *feats*; a bare number is a default, so ``UNCALIBRATED``."""
    if isinstance(cost, (int, float)):
        return Confidence(
            level=ConfidenceLevel.UNCALIBRATED,
            facts={"summary": f"a fixed cost of {float(cost):g} cycles, not a model"},
        )
    return cost.confidence_feat(feats)


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
    #: The weakest call's confidence (``None`` until the report fills it in).
    confidence: Confidence | None = None

    @property
    def latency_mean_s(self) -> float:
        return self.latency_sum_s / self.n if self.n else 0.0

    @property
    def wait_mean_s(self) -> float:
        """Mean queueing delay."""
        return self.wait_sum_s / self.n if self.n else 0.0


@dataclass
class CpuReport:
    """Utilization, per-function latency and queueing, and confidences, for one processor run."""

    config_name: str
    elapsed_s: float
    core_busy_s: list[float]
    functions: dict[str, FunctionStats] = field(default_factory=dict)
    records: list[TaskRecord] = field(default_factory=list)

    @property
    def utilization(self) -> list[float]:
        """Per core: busy time over elapsed time (0 for a zero-length run)."""
        if self.elapsed_s <= 0:
            return [0.0 for _ in self.core_busy_s]
        return [b / self.elapsed_s for b in self.core_busy_s]


def build_report(
    config_name: str,
    elapsed_s: float,
    core_busy_s: list[float],
    records: list[TaskRecord],
) -> CpuReport:
    """Fold *records* into per-function stats, computing each distinct confidence once."""
    report = CpuReport(config_name, elapsed_s, list(core_busy_s), records=list(records))
    seen: dict[tuple, Confidence] = {}
    for rec in records:
        st = report.functions.get(rec.name)
        if st is None:
            st = report.functions[rec.name] = FunctionStats(rec.name)
        st.n += 1
        st.cycles += rec.cycles
        st.latency_sum_s += rec.latency_s
        st.latency_max_s = max(st.latency_max_s, rec.latency_s)
        st.wait_sum_s += rec.wait_s
        st.wait_max_s = max(st.wait_max_s, rec.wait_s)
        if rec.func is None:
            continue
        key = (id(rec.func.cycles), tuple(sorted(rec.feats.items())))
        conf = seen.get(key)
        if conf is None:
            conf = seen[key] = cost_confidence(rec.func.cycles, rec.feats)
        if st.confidence is None or conf.level < st.confidence.level:
            st.confidence = conf
    return report
