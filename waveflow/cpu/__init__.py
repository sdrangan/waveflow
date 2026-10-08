"""waveflow.cpu — a loosely-timed, calibrated model of a general-purpose processor.

Software is Python: a :class:`SwFunction` runs its body in zero simulated time and reports the work
it did as counters; its cost model turns those counters into cycles; a :class:`Processor` schedules
the calls over its cores and charges the cycles in one timed event each.  See ``plans/cpu_model.md``.
"""

from waveflow.cpu.area import CpuAreaModel, config_features
from waveflow.cpu.config import IRQ_PRIO, CpuConfig, CycleCost
from waveflow.cpu.processor import Processor
from waveflow.cpu.report import CpuReport, FunctionStats
from waveflow.cpu.task import (
    SwFunction,
    TaskRecord,
    cache_regime,
    eval_cost,
    eval_cycles,
    regime_features,
)

__all__ = [
    "IRQ_PRIO",
    "CpuAreaModel",
    "CpuConfig",
    "CpuReport",
    "CycleCost",
    "FunctionStats",
    "Processor",
    "SwFunction",
    "TaskRecord",
    "cache_regime",
    "config_features",
    "eval_cost",
    "eval_cycles",
    "regime_features",
]
