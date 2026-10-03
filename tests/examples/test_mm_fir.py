"""pysim rungs of examples/mm_fir — the witness of plans/mm_slave_adaptor.md.

Rung 1: one tap set, bit-exact.  Rung 2: taps switched mid-stream at ``apply_at``, bit-exact, and the
status shows every config received in time.  The negative control is a host that commits the second
config AFTER its switch point: the kernel must count it ``late`` and the output must differ from the
golden -- otherwise the rung-2 pass would not prove the protocol is doing anything.

Every rung runs in all three wirings (plans/mm_adaptor_host_endpoints.md): each view on its own
crossbar slot, all three behind one adaptor front, and **direct** -- the host's endpoints joined straight
to the kernel.  The host class is the same in all three; only the wiring differs.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.mm_fir.mm_fir import MmFirSystem, fir_golden, make_cfg, FirCfg, NTAP_MAX

TAPS_A = [3, -1, 4, 1, -5]
TAPS_B = [2, 7, 1, -8, 2, 8, 1, -8]


#: (link, one_front) -- the three wirings.
WIRINGS = [("mm", False), ("mm", True), ("direct", False)]
WIRING_IDS = ["per_view", "one_front", "direct"]


def _x(n=200, seed=7):
    return np.random.default_rng(seed).integers(-2000, 2000, size=n)


def _sys(link, one_front, **kw):
    return MmFirSystem(link=link, one_front=one_front, **kw)


@pytest.mark.parametrize("link,one_front", WIRINGS, ids=WIRING_IDS)
def test_rung1_fixed_taps_bit_exact(link, one_front):
    x = _x(120)
    plan = [(0, TAPS_A)]
    s = _sys(link, one_front, x=list(x), plan=plan)
    y = s.run()
    assert np.array_equal(y, fir_golden(x, plan))
    st = s.host.final_status
    assert (int(st.nsamp), int(st.ncfg), int(st.late)) == (120, 1, 0)


@pytest.mark.parametrize("link,one_front", WIRINGS, ids=WIRING_IDS)
@pytest.mark.parametrize("switch_at", [16, 96, 101])
def test_rung2_mid_stream_switch_bit_exact(switch_at, link, one_front):
    """101 is not a packet boundary: the host splits the packet there."""
    x = _x()
    plan = [(0, TAPS_A), (switch_at, TAPS_B)]
    s = _sys(link, one_front, x=list(x), plan=plan)
    y = s.run()
    assert np.array_equal(y, fir_golden(x, plan))
    st = s.host.final_status
    assert (int(st.nsamp), int(st.ncfg), int(st.late)) == (200, 2, 0)


@pytest.mark.parametrize("link,one_front", WIRINGS, ids=WIRING_IDS)
def test_late_config_is_detected_not_silently_misapplied(link, one_front):
    x = _x()
    plan = [(0, TAPS_A), (96, TAPS_B)]
    s = _sys(link, one_front, x=list(x), plan=plan, lag=32)
    y = s.run()
    assert int(s.host.final_status.late) == 1
    assert not np.array_equal(y, fir_golden(x, plan)), "a late config must not look on-time"
    # ...and what it did instead is exactly "in force from the sample it arrived at" -- some sample
    # after 96 and no later than the 128 the host had sent by then.  WHICH one depends on how far
    # behind the host the kernel was, and that differs between the bus and a direct connection.
    arrived = [n for n in range(97, 96 + 32 + 1)
               if np.array_equal(y, fir_golden(x, [(0, TAPS_A), (n, TAPS_B)]))]
    assert len(arrived) == 1, arrived


def test_the_host_is_the_same_class_with_the_same_schedule_in_every_wiring():
    """The point of the endpoints: switching wiring changes nothing the host does."""
    x, plan = _x(), [(0, TAPS_A), (101, TAPS_B)]
    systems = [_sys(link, one_front, x=list(x), plan=plan) for link, one_front in WIRINGS]
    ys = [s.run() for s in systems]
    assert all(np.array_equal(y, ys[0]) for y in ys)
    assert len({type(s.host) for s in systems}) == 1
    assert all(s.host.schedule == systems[0].host.schedule for s in systems)
    # The memory-mapped host never names an address: the map does.
    assert systems[0].slave_map["qout"].status_addr == 0x2800
    assert systems[1].slave_map["regs"].commit_addr == 0x0800


def test_one_front_needs_a_memory_mapped_link():
    with pytest.raises(ValueError, match="link='mm'"):
        MmFirSystem(x=[0], plan=[(0, TAPS_A)], link="direct", one_front=True)


def test_make_cfg_bounds():
    with pytest.raises(ValueError):
        make_cfg([], 0)
    with pytest.raises(ValueError):
        make_cfg([1] * (NTAP_MAX + 1), 0)
    assert FirCfg.nwords_per_inst(64) == 5
