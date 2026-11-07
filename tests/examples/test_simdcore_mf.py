"""The matched filter on the SIMD core's interpreter, bit for bit against ``examples/vecunit_mf``'s
golden (``plans/simdcore.md`` Stage 3)."""

from __future__ import annotations

import numpy as np
import pytest

from examples.simdcore_mf.mf_program import frame, input_floor, mf_config, run_mf, steady_table
from examples.vecunit_mf.mf_golden import mf_golden
from examples.vecunit_mf.mf_unit import FR, FX, FY


@pytest.mark.parametrize("nsamp,nrx,L", [(64, 8, 2), (256, 8, 2), (100, 3, 2), (1, 1, 1), (130, 5, 4),
                                         (64, 8, 8), (200, 7, 1)])
def test_bitexact_vs_vecunit_golden(nsamp, nrx, L):
    xr, xi, rr, ri = frame(nsamp, nrx, seed=nsamp + nrx)
    yr, yi, r = run_mf(mf_config(nrx, L=L), xr, xi, rr, ri)
    gr, gi = mf_golden(xr, xi, FX, rr, ri, FR, FY)
    assert np.array_equal(yr, gr) and np.array_equal(yi, gi)


def test_saturation_corner():
    """-1 x -1 in every part: the golden and the core both saturate."""
    n, nrx = 8, 2
    xr = xi = np.full(n, -32768)
    rr = ri = np.full((n, nrx), -32768)
    yr, yi, r = run_mf(mf_config(nrx), xr, xi, rr, ri)
    gr, gi = mf_golden(xr, xi, FX, rr, ri, FR, FY)
    assert np.array_equal(yr, gr) and np.array_equal(yi, gi)
    assert r.state.vxsat == 1


def test_steady_interval_is_load_bound_from_two_lanes():
    """The cycle model: one lane is compute-bound (8 multiplies x 256 samples a frame), two lanes and
    up sit within 15 % of the input floor (D9's pre-registered target, here as a prediction)."""
    rows = {r["L"]: r for r in steady_table(lanes=(1, 2, 4))}
    assert rows[1]["cycles_per_frame"] > 2048
    assert rows[2]["vs_floor"] <= 1.15
    assert rows[4]["cycles_per_frame"] <= rows[2]["cycles_per_frame"]
    assert input_floor(256, 8) == 1152
