"""pysim tests for MemSlaveAdaptor (plans/mm_slave_adaptor.md Stage 4): several views, one bus port."""
from __future__ import annotations

import numpy as np
import pytest

from examples.mm_fir.mm_fir import MmFirSystem, fir_golden
from waveflow.hw.mm_adaptor import MemSlaveAdaptor
from waveflow.hw.mm_queue import MemSlaveRStream, MemSlaveWStream
from waveflow.simulation.simulation import Simulation


def test_mm_fir_one_front_is_bit_exact_and_matches_per_view_timing():
    x = np.random.default_rng(3).integers(-2000, 2000, size=150)
    plan = [(0, [1, 2, 3]), (77, [4, -5, 6, -7])]
    a = MmFirSystem(x=list(x), plan=plan)
    b = MmFirSystem(x=list(x), plan=plan, one_front=True)
    ya, yb = a.run(), b.run()
    assert np.array_equal(yb, fir_golden(x, plan))
    assert np.array_equal(ya, yb)
    assert b.adaptor.errors == []
    # One host transaction at a time either way, so one port changes nothing in pysim.
    assert a.sim.env.now == b.sim.env.now


def test_span_and_offsets():
    sim = Simulation()
    qs = [MemSlaveWStream(name=f"q{k}", sim=sim) for k in range(3)]
    ad = MemSlaveAdaptor(name="ad", sim=sim, views=qs)
    assert ad.span() == 0x4000 and ad.offset_of(qs[2]) == 0x2000
    assert ad.s_mem.half_duplex, "the front serves reads and writes one at a time"


def test_unmapped_window_reads_zero_and_records():
    sim = Simulation()
    qs = [MemSlaveRStream(name="q0", sim=sim), MemSlaveRStream(name="q1", sim=sim),
          MemSlaveRStream(name="q2", sim=sim)]
    ad = MemSlaveAdaptor(name="ad", sim=sim, views=qs)
    assert list(ad.s_mem.peek_read(2, 0x3000)) == [0, 0]


def test_width_mismatch_refused():
    sim = Simulation()
    with pytest.raises(ValueError, match="bits wide"):
        MemSlaveAdaptor(name="ad", sim=sim, mem_dwidth=64,
                        views=[MemSlaveWStream(name="q", sim=sim, mem_dwidth=32)])
