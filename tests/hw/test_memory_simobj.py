"""
Tests for MemoryMod as a latency-modeling SimObj (waveflow/hw/memory.py).

These are the first tests that drive a ``Memory`` through a real
``Simulation`` (``run_sim()``) over an actual interconnect, rather than calling
``Memory`` methods directly (those live in ``test_memory.py``).

Coverage
--------
* round-trip   — write an array through s_mm, read it back
* latency      — total time == bus latency + modeled access latency (decision 2)
* zero latency — default knobs add no time, data still correct
* typed path   — write_array / read_array round-trip through the slave
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import numpy.testing as npt

from waveflow.hw.clock import Clock
from waveflow.hw.memory import MemoryMod
from waveflow.hw.memif import DirectMMIF, MMIFMaster
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# Driver SimObj
# ---------------------------------------------------------------------------

@dataclass
class _Driver(SimObj):
    """Runs a caller-supplied program generator and stashes its result."""

    master: MMIFMaster | None = None
    program: Callable[["_Driver"], ProcessGen[Any]] | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.result: Any = None

    def run_proc(self) -> ProcessGen[None]:
        self.result = yield from self.program(self)


def _wire(sim, mem, *, clk, latency_write=0.0, latency_read=0.0,
          latency_read_return=0.0, program=None):
    """Wire one _Driver → DirectMMIF → mem.s_mm and return the driver."""
    master = MMIFMaster(sim=sim, bitwidth=mem.word_size)
    direct = DirectMMIF(
        sim=sim, clk=clk,
        latency_write=latency_write,
        latency_read=latency_read,
        latency_read_return=latency_read_return,
    )
    direct.bind("master", master)
    direct.bind("slave", mem.s_mm)
    return _Driver(sim=sim, master=master, program=program)


# ---------------------------------------------------------------------------
# Round-trip through the slave path
# ---------------------------------------------------------------------------

def test_round_trip_through_slave():
    sim = Simulation()
    clk = Clock(freq=1.0)
    mem = MemoryMod(name="mem", sim=sim, inline=False, clk=clk)

    data = np.arange(6, dtype=np.uint32) + 100

    def program(drv):
        addr = mem.alloc(6)               # byte address of a 6-word region
        yield from drv.master.write(data, addr)
        got = yield from drv.master.read(6, addr)
        return got

    drv = _wire(sim, mem, clk=clk, program=program)
    sim.run_sim()
    npt.assert_array_equal(drv.result, data)


# ---------------------------------------------------------------------------
# Latency composition (decision 2): bus + access, no double-count
# ---------------------------------------------------------------------------

def test_access_latency_composes_with_bus_latency():
    """Total time = bus latency (interconnect) + access latency (memory).

    Run the same read twice — once with a zero-latency memory, once with a
    latency-bearing one — and assert the *difference* is exactly the modeled
    access delay.  That proves the two latencies compose and the bus time is
    not double-counted.
    """
    N = 4
    L0, Lw = 5.0, 2.0          # memory: init + per-word cycles
    LR, LRR = 2.0, 1.0         # bus: read request + read return cycles
    access_delay = (L0 + N * Lw)   # freq == 1.0 → cycles == seconds

    def make(mem_latency: bool):
        sim = Simulation()
        clk = Clock(freq=1.0)
        mem = MemoryMod(
            name="mem", sim=sim, inline=False, clk=clk,
            latency_init=L0 if mem_latency else 0.0,
            latency_per_word=Lw if mem_latency else 0.0,
        )
        data = np.arange(N, dtype=np.uint32)
        times: dict[str, float] = {}

        def program(drv):
            addr = mem.alloc(N)
            yield from drv.master.write(data, addr)
            t0 = drv.now
            yield from drv.master.read(N, addr)
            times["read"] = drv.now - t0
            return None

        _wire(sim, mem, clk=clk, latency_read=LR, latency_read_return=LRR,
              program=program)
        sim.run_sim()
        return times["read"]

    t_bus_only = make(mem_latency=False)
    t_bus_plus_access = make(mem_latency=True)

    # Bus-only read = LR (request) + 0 (access) + (LRR + N) (return burst)
    assert t_bus_only == LR + (LRR + N)
    # Adding the memory's access latency shifts the total by exactly that delay.
    assert t_bus_plus_access - t_bus_only == access_delay
    # And the absolute total is the explicit bus + access sum.
    assert t_bus_plus_access == LR + access_delay + (LRR + N)


def test_write_latency_is_bus_plus_access():
    N = 3
    L0, Lw = 4.0, 1.0
    LW = 3.0   # DirectMMIF latency_write
    sim = Simulation()
    clk = Clock(freq=1.0)
    mem = MemoryMod(
        name="mem", sim=sim, inline=False, clk=clk,
        latency_init=L0, latency_per_word=Lw,
    )
    data = np.arange(N, dtype=np.uint32)
    times: dict[str, float] = {}

    def program(drv):
        addr = mem.alloc(N)
        t0 = drv.now
        yield from drv.master.write(data, addr)
        times["write"] = drv.now - t0
        return None

    _wire(sim, mem, clk=clk, latency_write=LW, program=program)
    sim.run_sim()
    # write = LW (bus) + (L0 + N*Lw) (access)
    assert times["write"] == LW + (L0 + N * Lw)


# ---------------------------------------------------------------------------
# Zero latency still works
# ---------------------------------------------------------------------------

def test_zero_latency_default():
    """Default knobs (0,0) make the memory contribute no access time.

    With zero bus latency too, a write costs 0 time.  A read still costs
    ``nwords`` on the bus (the DirectMMIF FULL read-return burst) — that is bus
    time, not memory access time — so the memory's contribution being zero is
    shown by subtracting that known bus term.
    """
    N = 3
    sim = Simulation()
    clk = Clock(freq=1.0)
    mem = MemoryMod(name="mem", sim=sim, inline=False, clk=clk)  # defaults: 0,0
    data = np.array([7, 8, 9], dtype=np.uint32)
    times: dict[str, float] = {}

    def program(drv):
        addr = mem.alloc(N)
        t0 = drv.now
        yield from drv.master.write(data, addr)
        times["write"] = drv.now - t0
        t1 = drv.now
        got = yield from drv.master.read(N, addr)
        times["read"] = drv.now - t1
        return got

    drv = _wire(sim, mem, clk=clk, program=program)
    sim.run_sim()
    npt.assert_array_equal(drv.result, data)
    assert times["write"] == 0.0          # zero bus + zero access
    assert times["read"] == N             # bus return burst only; access == 0


# ---------------------------------------------------------------------------
# Typed (schema/array) path through the slave
# ---------------------------------------------------------------------------

def test_write_array_read_array_round_trip():
    from waveflow.hw.dataschema import FloatField
    F32 = FloatField.specialize(bitwidth=32)

    sim = Simulation()
    clk = Clock(freq=1.0)
    mem = MemoryMod(name="mem", sim=sim, inline=False, clk=clk,
                       latency_init=1.0, latency_per_word=1.0)
    values = np.array([1.0, 2.5, -3.25, 4.0], dtype=np.float32)

    def program(drv):
        addr = mem.alloc(len(values))
        yield from drv.master.write_array(values, F32, addr)
        arr = yield from drv.master.read_array(F32, count=len(values), addr=addr)
        return arr

    drv = _wire(sim, mem, clk=clk, program=program)
    sim.run_sim()
    assert isinstance(drv.result, np.ndarray)
    npt.assert_allclose(drv.result, values, atol=1e-6)


def test_write_array_read_array_complex_round_trip():
    """A ``ComplexField`` array round-trips through ``write_array`` / ``read_array`` as a
    vectorized structured numpy array — exercising the open/closed numpy-transfer hook
    (``DataSchema.to_words_numpy`` / ``from_words_numpy``) with NO element-type switch in
    the transport.  Composite types fall back to the canonical recursive serializer, which
    returns the structured ``[('re', …), ('im', …)]`` array rather than a Python list."""
    from waveflow.hw.complexfield import ComplexField
    from waveflow.hw.dataschema import IntField
    from waveflow.utils import complexutils as cx
    from waveflow.utils.fixputils import Format

    CF = ComplexField.specialize(IntField.specialize(8, signed=True))
    sim = Simulation()
    clk = Clock(freq=1.0)
    mem = MemoryMod(name="mem", sim=sim, inline=False, clk=clk,
                       latency_init=1.0, latency_per_word=1.0)
    n = 4
    values = cx.make_complex([1, -2, 3, -4], [5, 6, -7, 8], Format(8, 4, True))

    def program(drv):
        nwpe = CF.nwords_per_inst(mem.word_size)
        addr = mem.alloc(nwpe * n)
        yield from drv.master.write_array(values, CF, addr, word_bw=mem.word_size)
        arr = yield from drv.master.read_array(
            CF, count=n, addr=addr, word_bw=mem.word_size)
        return arr

    drv = _wire(sim, mem, clk=clk, program=program)
    sim.run_sim()
    assert isinstance(drv.result, np.ndarray)          # numpy, not a list of instances
    assert drv.result.dtype.names == ("re", "im")      # structured (vectorizable) view
    npt.assert_array_equal(drv.result["re"], values["re"])
    npt.assert_array_equal(drv.result["im"], values["im"])


# ---------------------------------------------------------------------------
# m_mm direct master stays zero-latency (decision 4)
# ---------------------------------------------------------------------------

def test_inline_direct_master_zero_latency():
    sim = Simulation()
    clk = Clock(freq=1.0)
    mem = MemoryMod(name="mem", sim=sim, inline=True, nwords_tot=8, clk=clk,
                       latency_init=99.0, latency_per_word=99.0)
    times: dict[str, float] = {}

    def program(drv):
        yield from mem.m_mm.write(np.arange(4, dtype=np.uint32), mem._base_addr)
        got = yield from mem.m_mm.read(4, mem._base_addr)
        times["elapsed"] = drv.now
        return got

    # m_mm has no interconnect; drive it from a bare process via run_sim.
    drv = _Driver(sim=sim, master=None, program=program)
    sim.run_sim()
    npt.assert_array_equal(drv.result, np.arange(4))
    assert times["elapsed"] == 0.0   # direct master ignores the latency knobs
