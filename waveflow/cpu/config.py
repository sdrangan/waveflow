"""config.py — :class:`CpuConfig`, one point in a processor's design space.

A configuration says *what the processor is*: how many cores, how fast, how much cache, and whether
a ready task may take a core from a running one.  It holds no state and no models of software; the
cost of a piece of software lives on its :class:`~waveflow.cpu.task.SwFunction`, and the configuration
only supplies the facts those costs depend on (the clock to turn cycles into time, the cache sizes the
regime features are measured against).

The defaults describe the Cortex-A53 of a Zynq UltraScale+ RFSoC at speed grade -1, the part on the
RFSoC 4x2: 1.2 GHz (DS926 ``F_APUMAX``), 32 KB L1I and L1D per core and a 1 MB shared L2 (UG1085).
They are a starting point, not a claim about any other configuration — a design-space exploration
changes them, and the calibrated cost models say (through their ``Confidence``) how far they carry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from waveflow.calib.calib import CalibModel

#: A cost given either as a fixed number of cycles or as a calibrated model.  A plain number is the
#: honest form for something not yet measured *and* not worth a model (a test's switch cost); a
#: model is what a calibrated platform supplies, and it carries its own confidence.
CycleCost = Union[float, int, "CalibModel"]

#: The priority an interrupt handler runs at.  Priorities follow SimPy's convention -- a **lower**
#: number is more urgent -- so a handler outranks every task a caller can sensibly name.
IRQ_PRIO = -(2**31)


@dataclass(kw_only=True)
class CpuConfig:
    """One processor configuration: cores, clock, caches, scheduling policy, fixed overheads."""

    #: Names the configuration in reports; not used for anything else.
    name: str = "a53"
    #: Cores that run tasks concurrently.  Each is a token the ready queue hands out.
    n_cores: int = 1
    #: Core clock.  Everything inside the model is in cycles; this converts to simulated seconds.
    f_clk_hz: float = 1.2e9
    #: Per-core L1 instruction cache.
    l1i_bytes: int = 32 * 1024
    #: Per-core L1 data cache — the first boundary of the cache-regime features.
    l1d_bytes: int = 32 * 1024
    #: Shared L2 — the second boundary of the cache-regime features.
    l2_bytes: int = 1024 * 1024
    #: Whether a more urgent ready task takes a core from a less urgent running one.  Interrupt
    #: handlers preempt regardless: an interrupt is not a scheduling decision.
    preemptive: bool = False
    #: Cycles charged when a core starts a task other than the one it ran last (a context switch).
    #: A model here is evaluated with no features — the switch is a constant of the platform.
    switch_cycles: CycleCost = 0
    #: Extra cycles before an interrupt handler's body: entry, vectoring, register save.  A flagged
    #: constant (see ``plans/cpu_model.md`` §14) until a source or a measurement replaces it.
    irq_entry_cycles: CycleCost = 0
    #: Static (leakage) power of one powered core, in mW.  Every core is powered for the whole run,
    #: so a report charges ``n_cores * static_power_mw * elapsed``.  Normally a calibrated
    #: :class:`~waveflow.cpu.area.CpuAreaModel`'s ``leak_mw`` for this configuration -- the whole
    #: configuration's leakage -- divided by ``n_cores``, as :meth:`CpuPlatform.cpu_config` does.
    static_power_mw: float = 0.0

    def __post_init__(self) -> None:
        if self.n_cores < 1:
            raise ValueError(f"n_cores must be >= 1, got {self.n_cores}")
        if self.f_clk_hz <= 0:
            raise ValueError(f"f_clk_hz must be positive, got {self.f_clk_hz}")
        if self.static_power_mw < 0:
            raise ValueError(
                f"static_power_mw must be >= 0, got {self.static_power_mw}"
            )
        for name in ("l1i_bytes", "l1d_bytes", "l2_bytes"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")

    @property
    def period(self) -> float:
        """Seconds per cycle."""
        return 1.0 / self.f_clk_hz
