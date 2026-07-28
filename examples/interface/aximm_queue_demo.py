"""
aximm_queue_demo.py — SPSC ring buffer over an AXI-MM crossbar.

A producer and a consumer, each its own master on a crossbar, share a single
memory-backed FIFO (:class:`~waveflow.hw.aximm_queue.AXIMMQueue`).  The ring's
storage and its head/tail pointers all live in one ``MemoryMod`` region; the
two sides coordinate purely through memory (decision 1), with no shared Python
object.  The queue capacity is deliberately small, so the producer blocks on a
full ring and the consumer's draining is what lets it make progress — the
backpressure that proves the blocking ``write``/``get`` (decision 8).

Topology
--------
  Producer (master_0) ──┐
                        ├── AXIMMCrossBarIF ──── ring region (slave_0, FULL)
  Consumer (master_1) ──┘                        in one MemoryMod (s_mm)

      ring region (AXIMMQueueLayout @ base, capacity slots):
        word 0 : head      (consumer-owned)
        word 1 : tail      (producer-owned)
        word 2 : capacity
        word 3 : reserved
        data   : capacity slots × elem_words words

Scenario
--------
  1. The producer ``write``s ``np.arange(N)`` into a ``capacity``-slot ring,
     blocking whenever the ring fills.
  2. The consumer ``get``s all ``N`` words, blocking whenever the ring is empty.
  3. The harness asserts the consumer received the producer's exact sequence,
     in order, with no loss or duplication, and prints a timing summary.

Run standalone::

    python -m examples.interface.aximm_queue_demo
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from waveflow.hw.aximm_queue import AXIMMQueue, AXIMMQueueLayout
from waveflow.hw.clock import Clock
from waveflow.hw.memif import (
    AXIMMCrossBarIF,
    MMIFMaster,
    assign_address_ranges,
)
from waveflow.hw.memory import MemoryMod
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# Producer / consumer masters
# ---------------------------------------------------------------------------

@dataclass
class Producer(SimObj):
    """Enqueues a known sequence, blocking when the ring is full."""

    layout: AXIMMQueueLayout | None = None
    data: np.ndarray | None = None
    poll_interval: float = 1.0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.master_ep = MMIFMaster(sim=self.sim, bitwidth=self.layout.mem_bw)
        self.queue = AXIMMQueue(master=self.master_ep, layout=self.layout)
        self.done_time: float = 0.0

    def run_proc(self) -> ProcessGen[None]:
        # The producer owns setup: zero head/tail once before either side runs.
        yield from self.queue.reset()
        t0 = self.now
        yield from self.queue.write(self.data, poll_interval=self.poll_interval)
        self.done_time = self.now
        print(f"[Producer] wrote {len(self.data)} words into a "
              f"{self.layout.capacity}-slot ring, done at t={self.now:.3f} "
              f"(dt={self.now - t0:.3f})")


@dataclass
class Consumer(SimObj):
    """Dequeues ``count`` words, blocking when the ring is empty."""

    layout: AXIMMQueueLayout | None = None
    count: int = 0
    poll_interval: float = 1.0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.master_ep = MMIFMaster(sim=self.sim, bitwidth=self.layout.mem_bw)
        self.queue = AXIMMQueue(master=self.master_ep, layout=self.layout)
        self.received: np.ndarray | None = None
        self.done_time: float = 0.0

    def run_proc(self) -> ProcessGen[None]:
        t0 = self.now
        got = yield from self.queue.get(count=self.count, poll_interval=self.poll_interval)
        self.received = np.array(got)
        self.done_time = self.now
        print(f"[Consumer] received {len(self.received)} words, done at "
              f"t={self.now:.3f} (dt={self.now - t0:.3f})")


# ---------------------------------------------------------------------------
# Demo harness
# ---------------------------------------------------------------------------

class AXIMMQueueDemo:
    """Wires the producer, consumer, crossbar and ring memory, then runs."""

    BASE_ADDR = 0x1000
    CAPACITY = 8          # usable depth 7 — small enough to force backpressure
    N = 64                # words to stream through it
    MEM_BW = 32

    def __init__(self) -> None:
        self.sim = Simulation()
        self.clk = Clock(freq=100.0)   # 100 Hz → 1 cycle = 0.01 s

        self.layout = AXIMMQueueLayout(
            base_addr=self.BASE_ADDR, capacity=self.CAPACITY, mem_bw=self.MEM_BW
        )
        # External shared memory (decision 9): inline=False + one alloc places the
        # ring at byte 0, where the crossbar (global − base) delivers it.
        total_words = self.layout.total_bytes // self.layout.word_bytes
        self.mem = MemoryMod(
            sim=self.sim, word_size=self.MEM_BW, inline=False, clk=self.clk,
            latency_init=2.0, latency_per_word=1.0,
        )
        self.mem.alloc(total_words)

        self.data = np.arange(self.N, dtype=np.uint32)
        self.producer = Producer(sim=self.sim, layout=self.layout, data=self.data)
        self.consumer = Consumer(sim=self.sim, layout=self.layout, count=self.N)

        self.xbar = AXIMMCrossBarIF(
            sim=self.sim, clk=self.clk,
            nports_master=2, nports_slave=1, bitwidth=self.MEM_BW,
            latency_init=2.0, latency_read_return=2.0,
        )
        self.xbar.bind("master_0", self.producer.master_ep)
        self.xbar.bind("master_1", self.consumer.master_ep)
        self.xbar.bind("slave_0", self.mem.s_mm)
        assign_address_ranges(
            [self.mem.s_mm], [(self.layout.base_addr, self.layout.total_bytes)]
        )

    def run_and_check(self) -> np.ndarray:
        print("=== AXI-MM memory-backed queue demo ===")
        print(f"ring: base=0x{self.BASE_ADDR:04x}, capacity={self.CAPACITY} "
              f"(usable {self.CAPACITY - 1}), streaming N={self.N} words\n")

        self.sim.run_sim()

        received = self.consumer.received
        assert received is not None, "consumer never produced a result"
        assert np.array_equal(received, self.data), (
            f"FIFO mismatch:\n  expected {self.data}\n  got      {received}"
        )

        print(f"\nProducer finished at t={self.producer.done_time:.3f}, "
              f"consumer at t={self.consumer.done_time:.3f}, "
              f"sim end t={self.sim.env.now:.3f}.")
        print("All checks passed: consumer received the producer's exact "
              "sequence, in order.")
        return received


def run_and_check() -> np.ndarray:
    """Module-level entry point: build the demo and run its self-check."""
    return AXIMMQueueDemo().run_and_check()


if __name__ == "__main__":
    run_and_check()
