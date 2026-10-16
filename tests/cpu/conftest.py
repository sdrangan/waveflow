"""Shared helpers for the processor-model tests.

Tests run at ``f_clk_hz = 1.0``, so one cycle is one simulated second and every hand-computed
timeline is exact integer arithmetic.
"""

from __future__ import annotations

import pytest

from waveflow.cpu import CpuConfig, Processor, SwFunction
from waveflow.simulation.simulation import Simulation


def fixed(name: str, cycles: float, result=None) -> SwFunction:
    """A function with a fixed cost that returns *result* (default: its own name)."""
    out = name if result is None else result
    return SwFunction(name=name, fn=lambda: (out, {}), cycles=cycles)


class Run:
    """A simulation with one processor and a list of timed submissions."""

    def __init__(self, **cfg):
        cfg.setdefault("f_clk_hz", 1.0)
        self.sim = Simulation()
        self.cpu = Processor(name="cpu", sim=self.sim, config=CpuConfig(**cfg))
        self.results: dict[str, tuple[float, object]] = {}

    def submit(
        self, at: float, func: SwFunction, prio: int = 0, irq: bool = False, key=None
    ):
        """Submit *func* at time *at*; its (completion time, result) lands in ``results[key]``."""
        key = key or func.name

        def proc():
            if at:
                yield self.sim.env.timeout(at)
            if irq:
                r = yield from self.cpu.interrupt(func)
            else:
                r = yield from self.cpu.execute(func, prio=prio)
            self.results[key] = (self.sim.env.now, r)

        self.sim.env.process(proc())
        return self

    def run(self):
        self.sim.env.run()
        return self

    def rec(self, name: str):
        (r,) = [r for r in self.cpu.records if r.name == name]
        return r


@pytest.fixture
def make_run():
    return Run
