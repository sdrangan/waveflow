"""AC9 of ``plans/cpu_model.md`` (regime part, step 6): the cache-regime features and flag.

The code-bytes part (a kernel's symbol size from the static binary) is gem5-marked and arrives with
the runner in step 9 (``test_footprint_binary.py``).
"""

from __future__ import annotations

import pytest

from waveflow.cpu import CpuConfig, SwFunction, cache_regime, regime_features

CFG = CpuConfig(l1d_bytes=32 * 1024, l2_bytes=1024 * 1024)
L1, L2 = CFG.l1d_bytes, CFG.l2_bytes


@pytest.mark.parametrize(
    "ws, over_l1, over_l2, regime",
    [
        (0, 0, 0, "l1"),
        (L1 - 1, 0, 0, "l1"),
        (L1, 0, 0, "l1"),  # a working set exactly the cache's size fits
        (L1 + 1, 1, 0, "l2"),
        (L2, L2 - L1, 0, "l2"),
        (L2 + 1, L2 + 1 - L1, 1, "dram"),
        (4 * L2, 4 * L2 - L1, 3 * L2, "dram"),
    ],
)
def test_regime_features_bend_at_each_boundary(ws, over_l1, over_l2, regime):
    feats = regime_features(ws, CFG)
    assert feats == {"ws": ws, "ws_over_l1": over_l1, "ws_over_l2": over_l2}
    assert cache_regime(ws, CFG) == regime


def test_a_function_with_a_working_set_gets_regime_features():
    func = SwFunction(
        name="g",
        fn=lambda: (None, {"n": 3}),
        cycles=1,
        working_set=lambda c: c["n"] * 20_000,
    )
    feats = func.features({"n": 3}, CFG)
    assert feats == {
        "n": 3,
        "ws": 60_000.0,
        "ws_over_l1": 60_000.0 - L1,
        "ws_over_l2": 0.0,
    }


def test_the_report_carries_the_footprint(make_run):
    func = SwFunction(
        name="g",
        fn=lambda n: (None, {"n": n}),
        cycles=1,
        working_set=lambda c: c["n"] * 1024,
        code_bytes=312,
    )
    run = make_run(l1d_bytes=32 * 1024, l2_bytes=1024 * 1024)

    def proc():
        for n in (4, 40, 8):
            yield from run.cpu.execute(func, n)

    run.sim.env.process(proc())
    run.run()
    st = run.cpu.report().functions["g"]
    assert (st.code_bytes, st.ws_max, st.regime) == (312, 40 * 1024, "l2")
