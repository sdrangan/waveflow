"""Runs the three memory toys, so ``docs/guide/memory/`` cannot quote code that no longer works.

One toy per object — ``MemMgr`` (allocation and addressing), ``HwState`` (storage inside a module),
``MemoryMod`` (storage across a bus).  Each demo asserts its own claims internally; these tests run
them and additionally pin the facts the guide states in prose, so a doc claim and a test claim fail
together rather than drifting apart.
"""
from __future__ import annotations

import pytest

from examples.memory import hwstate_demo, memmgr_demo


def test_memmgr_demo_runs():
    memmgr_demo.main()


def test_hwstate_demo_runs():
    hwstate_demo.main()


# --- the claims the guide makes in prose -------------------------------------------------------


def test_the_total_carries_across_firings():
    """docs/guide/memory/hwstate.md: state persists across firings."""
    assert hwstate_demo.show_pysim() == [[1, 10], [2, 20], [3, 30]]


def test_state_emits_a_static_in_the_task_body():
    """hwstate.md quotes this declaration shape for the free-running flow."""
    src = hwstate_demo.show_codegen()
    assert "static ap_uint<32> total[2];" in src
    # ...and the pragma follows the declaration it names, which is the reason the specs live on
    # the HwState rather than on the schema.
    lines = [ln.strip() for ln in src.splitlines()]
    i = lines.index("static ap_uint<32> total[2];   // access=RW")
    assert lines[i + 1] == "#pragma HLS ARRAY_PARTITION variable=total complete dim=1"


def test_addressing_conventions_match_the_guide():
    """memmgr.md: 4 words is 16 bytes under byte addressing, index 4 under word addressing."""
    from waveflow.hw.memory import AddrUnit, MemMgr

    assert MemMgr(word_size=32, addr_unit=AddrUnit.byte).index_to_addr(4) == 16
    assert MemMgr(word_size=32, addr_unit=AddrUnit.word).index_to_addr(4) == 4


def test_first_fit_reuses_a_freed_gap():
    """memmgr.md: handing the manager live occupancy means a free is immediately visible."""
    from waveflow.hw.memory import Memory

    mem = Memory(word_size=32, nwords_tot=64)
    a = mem.alloc(4)
    mem.alloc(4)
    mem.free(a)
    assert mem.alloc(4) == a


def test_memorymod_access_latency_is_charged():
    """memorymod.md: an access on the s_mm path costs (init + nwords*per_word)/freq seconds.

    Checked against a bare MemoryMod (no interconnect), so the number isolates the memory's own
    access latency from any bus latency it would otherwise compose with.
    """
    from waveflow.hw.clock import Clock
    from waveflow.hw.memory import MemoryMod
    from waveflow.simulation.simulation import Simulation

    sim = Simulation()
    clk = Clock(freq=100e6)
    mem = MemoryMod(name="m", sim=sim, clk=clk, inline=False, word_size=32,
                    nwords_tot=256, latency_init=10, latency_per_word=1)
    assert mem._access_delay(4) == pytest.approx((10 + 4 * 1) / clk.freq)
    # It really is per-word, not a flat cost — the guide's formula, checked at two sizes.
    assert mem._access_delay(8) == pytest.approx((10 + 8 * 1) / clk.freq)
    # And allocation goes through the wrapped Memory, hence through its MemMgr: one policy.
    from waveflow.hw.memory import MemMgr

    assert isinstance(mem._mem.mgr, MemMgr)
    assert mem.alloc(8) == 0
