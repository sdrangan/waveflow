"""tests/examples/test_mem_copy_sweep.py — the XSI scenario parameterizes for a timing sweep.

The RTL is scenario-independent (``len`` is a runtime command field), so a timing sweep varies only
the vectors + the harness arena + the ``h.run(N)`` bound.  These check the two knobs (``xsi_jobs`` /
``xsi_run_cycles``) generalize while the defaults reproduce the committed gate scenario exactly — so
the 2908 gate is untouched.  No toolchain needed.
"""
from __future__ import annotations

import pytest

from examples.mem_copy.mem_copy import XSI_JOBS, xsi_jobs, xsi_run_cycles


class TestDefaultsPreserveTheGate:
    def test_default_jobs_are_the_committed_scenario(self):
        j = xsi_jobs()
        assert j == XSI_JOBS
        assert len(j) == 16 and all(x.n_words == 128 for x in j)
        assert [x.src_off for x in j[:3]] == [64, 192, 320]
        assert [x.dst_off for x in j[:3]] == [4096, 4224, 4352]

    def test_default_run_bound_clears_the_gate(self):
        # The gate completes at 2908; the derived bound must sit past it (the committed main still
        # bakes its own 3400 -- this is for sweep points that need more).
        assert xsi_run_cycles() > 2908


class TestSweepPoints:
    @pytest.mark.parametrize("n,k", [(32, 16), (64, 16), (256, 8), (512, 4)])
    def test_regions_do_not_overlap(self, n, k):
        j = xsi_jobs(n_words=n, num_cmds=k)
        assert len(j) == k and all(x.n_words == n for x in j)
        spans = sorted((x.src_off, x.src_off + n) for x in j) + \
                sorted((x.dst_off, x.dst_off + n) for x in j)
        spans.sort()
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            assert a1 <= b0, f"regions overlap at n={n} k={k}: [{a0},{a1}) vs [{b0},{b1})"

    def test_run_bound_scales_with_the_scenario(self):
        # Bigger jobs -> more cycles; the bound is monotone in words moved.
        assert xsi_run_cycles(512, 4) > xsi_run_cycles(128, 4)
        assert xsi_run_cycles(128, 16) > xsi_run_cycles(128, 4)

    def test_larger_scenarios_push_dst_past_src(self):
        """When num_cmds*n_words reaches past 4096, dst_base grows so src and dst stay apart."""
        j = xsi_jobs(n_words=512, num_cmds=16)     # srcs run to 64 + 16*512 = 8256
        assert min(x.dst_off for x in j) >= max(x.src_off + 512 for x in j)
