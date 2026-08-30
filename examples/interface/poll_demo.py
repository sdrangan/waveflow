"""poll_demo.py — the LT polling-overhead model (``MMIFMaster.poll_until``).

A master that *polls* a memory word (a queue consumer watching a ring tail, a
core spinning on a doorbell) imposes two distinct costs on a shared bus.  The
loosely-timed model in :meth:`waveflow.hw.memif.MMIFMaster.poll_until` charges
both in *O(transactions)* — it never steps the sim every poll cycle.  This demo
isolates each cost in its own scenario so the output reads as a teaching script.

Topology (one shared FULL slave, so the pollers and the bursts contend)::

      Producer  (master_0) ──┐
      Poller    (master_1) ──┼── AXIMMCrossBarIF ──── Ram (slave_0, FULL)
      Burster   (master_2) ──┘

Scenarios
---------
  A. **Discovery latency.**  The poller ``poll_until``\\s a flag the producer sets
     later; the satisfying read is observed a deterministic ``(poll_interval-1)/2``
     cycles after the event — the mean event-to-next-poll gap (decision D1).
  B. **Bandwidth steal.**  A burst read on the shared slave is timed with and
     without a concurrent active poller; the poller contributes ``ov`` to the
     bus, and the burst's *per-word* time is stretched by ``1/(1-ov)`` (the fixed
     init/address latency is not).
  C. **Saturation clamp-and-warn.**  A 1-cycle poll is ``ov=1`` — polling alone
     saturates the bus.  ``ov`` is clamped to 0.99 and one loud warning fires
     (decision D2): the model flags the exact mistake a coarse fixed poll hides.

See ``plans/poll_until_lt_model.md`` and ``docs/guide/timing_model/poll.md``.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np

from waveflow.hw.aximm import (
    AXIMMCrossBarIF,
    MMIFMaster,
    MMIFSlave,
    assign_address_ranges,
    Words,
)
from waveflow.hw.clock import Clock
from waveflow.hw.memif import Eq
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# Slave: a dict-backed RAM that can be polled
# ---------------------------------------------------------------------------

@dataclass
class Ram(SimObj):
    """Word-addressed RAM (dict).  To be *pollable* a slave must expose
    ``peek_read`` — an untimed synchronous snapshot of the backing store that
    ``poll_until`` reads while it waits (its bus cost is modeled aggregately, so
    the peek itself takes zero sim time)."""

    access_latency: float = 1.0   # cycles of rx_read access time

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self._mem: dict[int, int] = {}
        self.slave_ep = MMIFSlave(
            sim=self.sim,
            bitwidth=32,
            rx_write_proc=self.rx_write,
            rx_read_proc=self.rx_read,
            peek_read=self.peek,          # <- makes this slave pollable
        )

    _WORD = 4   # byte-addressed: one 32-bit word per 4 byte addresses

    def rx_write(self, words: Words, local_addr: int) -> ProcessGen[None]:
        for i, w in enumerate(words):
            self._mem[local_addr + i * self._WORD] = int(w)
        yield self.timeout(0)

    def rx_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        yield self.timeout(self.access_latency / 1.0)
        return self.peek(nwords, local_addr)

    def peek(self, nwords: int, local_addr: int) -> Words:
        """Untimed snapshot — same data path as a read, but no sim time."""
        return np.array(
            [self._mem.get(local_addr + i * self._WORD, 0) for i in range(nwords)],
            dtype=np.uint32,
        )


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------

@dataclass
class Producer(SimObj):
    """Writes ``value`` to ``flag_addr`` after ``set_at`` cycles."""

    flag_addr: int = 0x0000
    value: int = 1
    set_at: float = 10.0

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.master_ep = MMIFMaster(sim=self.sim, bitwidth=32)
        self.set_time: float = -1.0

    def run_proc(self) -> ProcessGen[None]:
        yield self.timeout(self.set_at)
        yield self.process(
            self.master_ep.write(np.array([self.value], dtype=np.uint32), self.flag_addr)
        )
        self.set_time = self.now


@dataclass
class Poller(SimObj):
    """Blocks on ``poll_until`` until ``cond`` holds on ``addr``."""

    addr: int = 0x0000
    cond: object = None
    poll_interval: float = 8.0
    start_delay: float = 0.0

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.master_ep = MMIFMaster(sim=self.sim, bitwidth=32)
        self.observed_time: float = -1.0
        self.observed_value: int = -1

    def run_proc(self) -> ProcessGen[None]:
        if self.start_delay > 0:
            yield self.timeout(self.start_delay)
        self.observed_value = yield from self.master_ep.poll_until(
            self.addr, self.cond, self.poll_interval
        )
        self.observed_time = self.now


@dataclass
class Burster(SimObj):
    """Reads an ``nwords``-word burst from ``addr`` once, recording its duration."""

    addr: int = 0x0040
    nwords: int = 64
    start_delay: float = 1.0

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.master_ep = MMIFMaster(sim=self.sim, bitwidth=32)
        self.duration: float = -1.0

    def run_proc(self) -> ProcessGen[None]:
        yield self.timeout(self.start_delay)
        t0 = self.now
        yield from self.master_ep.read(self.nwords, self.addr)
        self.duration = self.now - t0


# ---------------------------------------------------------------------------
# Scenario harnesses — each builds an isolated Simulation
# ---------------------------------------------------------------------------

# freq=1.0 ⇒ one cycle is one second, so printed times read directly in cycles.
def _crossbar(sim: Simulation, clk: Clock, nmasters: int) -> AXIMMCrossBarIF:
    xbar = AXIMMCrossBarIF(
        sim=sim, clk=clk,
        nports_master=nmasters, nports_slave=1, bitwidth=32,
        latency_init=2.0, latency_read_return=2.0,
    )
    return xbar


FLAG = 0x0000
DATA = 0x0040


def scenario_discovery() -> float:
    """A: the discovery delay is the deterministic mean ``(poll_interval-1)/2``."""
    sim = Simulation()
    clk = Clock(freq=1.0)
    ram = Ram(sim=sim)
    poll_interval = 8.0
    producer = Producer(sim=sim, flag_addr=FLAG, value=1, set_at=10.0)
    poller = Poller(sim=sim, addr=FLAG, cond=Eq(1), poll_interval=poll_interval)

    xbar = _crossbar(sim, clk, nmasters=2)
    xbar.bind("master_0", producer.master_ep)
    xbar.bind("master_1", poller.master_ep)
    xbar.bind("slave_0", ram.slave_ep)
    assign_address_ranges([ram.slave_ep], [(0x0000, 0x1000)])

    sim.run_sim()

    discovery = poller.observed_time - producer.set_time
    expected = (poll_interval - 1.0) / 2.0
    print("=== A. discovery latency ===")
    print(f"  producer set flag=1 at t={producer.set_time:.3f}")
    print(f"  poller observed it at  t={poller.observed_time:.3f}  (value={poller.observed_value})")
    print(f"  discovery delay = {discovery:.3f} cycles  "
          f"(expected mean (poll_interval-1)/2 = {expected:.3f})")
    print()
    assert poller.observed_value == 1
    assert abs(discovery - expected) < 1e-9
    return discovery


def _burst_duration(with_poller: bool, poll_interval: float = 4.0) -> float:
    sim = Simulation()
    clk = Clock(freq=1.0)
    ram = Ram(sim=sim)
    burster = Burster(sim=sim, addr=DATA, nwords=64, start_delay=1.0)

    masters = [burster.master_ep]
    poller = None
    if with_poller:
        # Poll a flag that is never set, so the poller stays active (and so keeps
        # contributing ov) for the whole burst.
        poller = Poller(sim=sim, addr=FLAG, cond=Eq(1), poll_interval=poll_interval)
        masters.append(poller.master_ep)

    xbar = _crossbar(sim, clk, nmasters=len(masters))
    for i, m in enumerate(masters):
        xbar.bind(f"master_{i}", m)
    xbar.bind("slave_0", ram.slave_ep)
    assign_address_ranges([ram.slave_ep], [(0x0000, 0x1000)])

    # The never-satisfied poller would run forever, so we can't use run_sim();
    # schedule the processes by hand and stop the moment the burst completes.
    for obj in sim._sim_objs:
        obj.pre_sim()
    if poller is not None:
        sim.env.process(poller.run_proc())
    burst_proc = sim.env.process(burster.run_proc())
    sim.env.run(until=burst_proc)
    return burster.duration


def scenario_bandwidth() -> tuple[float, float]:
    """B: an active poller steals bandwidth; the burst's per-word time stretches
    by ``1/(1-ov)``."""
    base = _burst_duration(with_poller=False)
    poll_interval = 4.0                         # ov = 1/4 = 0.25
    derated = _burst_duration(with_poller=True, poll_interval=poll_interval)
    ov = 1.0 / poll_interval
    stretch = 1.0 / (1.0 - ov)
    print("=== B. bandwidth steal ===")
    print(f"  burst (64 words) alone:         {base:.3f} cycles")
    print(f"  burst + poller (interval={poll_interval:.0f}, ov={ov:.2f}): "
          f"{derated:.3f} cycles")
    print(f"  per-word stretch 1/(1-ov) = {stretch:.3f}x "
          f"(init/address latency is NOT derated, so total < {stretch:.2f}x)")
    print()
    assert derated > base
    return base, derated


def scenario_saturation() -> None:
    """C: a 1-cycle poll is ov=1 — clamp to 0.99 and warn loudly."""
    print("=== C. saturation clamp-and-warn ===")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _burst_duration(with_poller=True, poll_interval=1.0)   # ov = 1/1 = 1.0
    polling_bound = [w for w in caught if "polling-bound" in str(w.message)]
    for w in polling_bound:
        print(f"  WARNING: {w.message}")
    print()
    assert polling_bound, "expected a polling-bound clamp warning at poll_interval=1"


def run_and_check() -> None:
    print("=== poll_until LT polling-overhead model -- toy demo ===\n")
    scenario_discovery()
    scenario_bandwidth()
    scenario_saturation()
    print("All checks passed.")


if __name__ == "__main__":
    run_and_check()
