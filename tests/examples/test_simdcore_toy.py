"""The SIMD core's toy kernels against their NumPy goldens, and the cycle model on them
(``plans/simdcore.md`` Stage 3)."""

from __future__ import annotations

import numpy as np
import pytest

from examples.simdcore.toy import KERNELS, run_kernel


@pytest.mark.parametrize("name", sorted(KERNELS))
@pytest.mark.parametrize("n,L", [(64, 2), (17, 4), (1, 1), (128, 8)])
def test_kernel_vs_golden(name, n, L):
    if KERNELS[name].sew == 32 and n > 64:
        n = 64
    for seed in range(3):
        r, out, gold = run_kernel(name, n, L=L, seed=seed)
        if KERNELS[name].scalar_out:
            assert out == gold, (name, n, L, seed)
        else:
            assert np.array_equal(out, gold), (name, n, L, seed)


def test_cycle_model_on_kernels():
    """Each kernel's vector work is its instruction count times N/L (plus memory)."""
    for name in ("cmul", "poly", "pwl"):
        r1, _, _ = run_kernel(name, 64, L=1)
        r4, _, _ = run_kernel(name, 64, L=4)
        assert r4.timing.lane_cycles * 3.5 <= r1.timing.lane_cycles
