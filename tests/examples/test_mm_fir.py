"""pysim rungs of examples/mm_fir — the witness of plans/mm_slave_adaptor.md.

Rung 1: one tap set, bit-exact.  Rung 2: taps switched mid-stream, bit-exact, and every packet's
response echoes its tx_id and the config it was meant to use.  Order between the config and the samples
is carried by the header's ``cfg_seq`` (plans/mm_fir_cfg_seq.md): a config committed LATE is waited for
(output still exact); the negative control is a host that tags its packets with the wrong config, which
the responses must expose -- otherwise the rung-2 pass would not prove the echo checks anything.

Every rung runs in all three wirings (plans/mm_adaptor_host_endpoints.md): each view on its own
crossbar slot, all four behind one adaptor front, and **direct** -- the host's endpoints joined straight
to the kernel.  The host class is the same in all three; only the wiring differs.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.mm_fir.mm_fir import (
    NTAP_MAX,
    FirCfg,
    FirCmdHdr,
    FirRespHdr,
    MmFirSystem,
    fir_golden,
    host_schedule,
    make_cfg,
)

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
    assert (int(st.nsamp), int(st.ncfg)) == (120, 1)
    assert s.host.mismatches == []


@pytest.mark.parametrize("link,one_front", WIRINGS, ids=WIRING_IDS)
@pytest.mark.parametrize("switch_at", [16, 96, 101])
def test_rung2_mid_stream_switch_bit_exact(switch_at, link, one_front):
    """101 is not a packet boundary: the host cuts the packet there."""
    x = _x()
    plan = [(0, TAPS_A), (switch_at, TAPS_B)]
    s = _sys(link, one_front, x=list(x), plan=plan)
    y = s.run()
    assert np.array_equal(y, fir_golden(x, plan))
    st = s.host.final_status
    assert (int(st.nsamp), int(st.ncfg)) == (200, 2)
    assert s.host.mismatches == []
    npkt = sum(1 for it in s.host.schedule if it[0] == "pkt")
    assert [t for t, _ in s.host.responses] == list(range(npkt))


@pytest.mark.parametrize("link,one_front", WIRINGS, ids=WIRING_IDS)
def test_a_config_committed_late_is_waited_for(link, one_front):
    """The second config is committed 32 samples AFTER the packets that need it went out.  They wait in
    queue in until it arrives -- the header's cfg_seq makes the kernel wait -- so the output is still
    exact.  (Under the old apply_at protocol this was the negative control: the switch landed late.)"""
    x = _x()
    plan = [(0, TAPS_A), (96, TAPS_B)]
    s = _sys(link, one_front, x=list(x), plan=plan, lag=32)
    sched = s.host.schedule
    first_b = next(i for i, it in enumerate(sched) if it[0] == "pkt" and it[3] == 2)
    commit_b = [i for i, it in enumerate(sched) if it[0] == "cfg"][1]
    assert commit_b > first_b, "the scenario must really send packets before their config"
    y = s.run()
    assert np.array_equal(y, fir_golden(x, plan))
    assert s.host.mismatches == []


@pytest.mark.parametrize("link,one_front", WIRINGS, ids=WIRING_IDS)
def test_negative_control_a_wrong_tag_is_exposed_by_the_responses(link, one_front):
    """The host tags every packet with config 1 while meaning config 2 after the switch.  The kernel
    obeys the tag -- it never takes config 2 -- and every response after the switch echoes cfg_seq 1
    where the host meant 2.  Without this run, an empty mismatch list could mean the echo works or that
    it checks nothing."""
    x = _x()
    plan = [(0, TAPS_A), (96, TAPS_B)]
    s = _sys(link, one_front, x=list(x), plan=plan, stale_tag=True)
    y = s.run()
    assert not np.array_equal(y, fir_golden(x, plan))
    assert np.array_equal(y, fir_golden(x, [(0, TAPS_A)])), "taps A throughout, as tagged"
    assert int(s.host.final_status.ncfg) == 1
    after = [i for i, it in enumerate(it for it in s.host.schedule if it[0] == "pkt") if it[4] == 2]
    assert [(t, f, e, g) for t, f, e, g in s.host.mismatches] == [(t, "cfg_seq", 2, 1) for t in after]


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
    assert systems[1].slave_map["qresp"].base == 0x3000


def test_schedule_cuts_at_switches_and_tags_each_packet():
    sched = host_schedule(40, [(0, TAPS_A), (21, TAPS_B)], pkt=16)
    assert sched == [("cfg", TAPS_A), ("pkt", 0, 16, 1, 1), ("pkt", 16, 21, 1, 1),
                     ("cfg", TAPS_B), ("pkt", 21, 37, 2, 2), ("pkt", 37, 40, 2, 2)]


def test_one_front_needs_a_memory_mapped_link():
    with pytest.raises(ValueError, match="link='mm'"):
        MmFirSystem(x=[0], plan=[(0, TAPS_A)], link="direct", one_front=True)


def test_message_sizes():
    with pytest.raises(ValueError):
        make_cfg([])
    with pytest.raises(ValueError):
        make_cfg([1] * (NTAP_MAX + 1))
    assert (FirCfg.nwords_per_inst(64), FirCmdHdr.nwords_per_inst(64),
            FirRespHdr.nwords_per_inst(64)) == (5, 1, 1)
