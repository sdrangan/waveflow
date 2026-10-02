"""pysim rungs of examples/mm_fir — the witness of plans/mm_slave_adaptor.md.

Rung 1: one tap set, bit-exact.  Rung 2: taps switched mid-stream at ``apply_at``, bit-exact, and the
status shows every config received in time.  The negative control is a host that commits the second
config AFTER its switch point: the kernel must count it ``late`` and the output must differ from the
golden -- otherwise the rung-2 pass would not prove the protocol is doing anything.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.mm_fir.mm_fir import MmFirSystem, fir_golden, make_cfg, FirCfg, NTAP_MAX

TAPS_A = [3, -1, 4, 1, -5]
TAPS_B = [2, 7, 1, -8, 2, 8, 1, -8]


def _x(n=200, seed=7):
    return np.random.default_rng(seed).integers(-2000, 2000, size=n)


def test_rung1_fixed_taps_bit_exact():
    x = _x(120)
    plan = [(0, TAPS_A)]
    s = MmFirSystem(x=list(x), plan=plan)
    y = s.run()
    assert np.array_equal(y, fir_golden(x, plan))
    st = s.host.final_status
    assert (int(st.nsamp), int(st.ncfg), int(st.late)) == (120, 1, 0)


@pytest.mark.parametrize("switch_at", [16, 96, 101])
def test_rung2_mid_stream_switch_bit_exact(switch_at):
    """101 is not a packet boundary: the host splits the packet there."""
    x = _x()
    plan = [(0, TAPS_A), (switch_at, TAPS_B)]
    s = MmFirSystem(x=list(x), plan=plan)
    y = s.run()
    assert np.array_equal(y, fir_golden(x, plan))
    st = s.host.final_status
    assert (int(st.nsamp), int(st.ncfg), int(st.late)) == (200, 2, 0)


def test_late_config_is_detected_not_silently_misapplied():
    x = _x()
    plan = [(0, TAPS_A), (96, TAPS_B)]
    s = MmFirSystem(x=list(x), plan=plan, lag=32)
    y = s.run()
    assert int(s.host.final_status.late) == 1
    assert not np.array_equal(y, fir_golden(x, plan)), "a late config must not look on-time"
    # ...and what it did instead is exactly "in force from the sample it arrived at".
    assert np.array_equal(y, fir_golden(x, [(0, TAPS_A), (96 + 32, TAPS_B)]))


def test_make_cfg_bounds():
    with pytest.raises(ValueError):
        make_cfg([], 0)
    with pytest.raises(ValueError):
        make_cfg([1] * (NTAP_MAX + 1), 0)
    assert FirCfg.nwords_per_inst(64) == 5
