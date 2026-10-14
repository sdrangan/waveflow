"""The LTBF engine (plans/mimo_cg/ltbf_cg_dse.md steps 4.1-4.2): pysim and the host-compiled body
against the fixed-point model, word for word."""

from __future__ import annotations

import pytest

from examples.ltbf_cg.hw import engine as eg
from examples.ltbf_cg.hw import host_check as hc


@pytest.mark.parametrize("explicit", [False, True], ids=["factored", "explicit"])
def test_pysim_matches_the_model_joint_and_per_ue(explicit):
    """A sector solved jointly, then UE by UE with H kept, then another sector."""
    s = eg.LtbfSystem(explicit=explicit)
    s.run()  # check() asserts every X word against the model
    assert s.tb.dut.jobs == len(s.jobs()) == 4


@pytest.mark.skipif(not hc.HLS_INCLUDE.is_dir(), reason="needs the Vitis HLS headers")
@pytest.mark.parametrize(
    "knobs",
    [
        {"explicit": False},
        {"explicit": True},
        {"explicit": False, "R": 4, "C": 2},
        {"explicit": False, "R": 1, "C": 4, "W": 12},
        {"explicit": True, "R": 4, "C": 4, "W": 20},
    ],
    ids=[
        "factored",
        "explicit",
        "factored-4x2",
        "factored-1x4-w12",
        "explicit-4x4-w20",
    ],
)
def test_the_hls_body_compiled_on_the_host_is_bit_exact(knobs, tmp_path):
    assert hc.host_check(eg.LtbfSystem(**knobs), tmp_path) == 0


def test_knobs_are_checked():
    with pytest.raises(ValueError):
        eg.LtbfSystem(R=3).run()
    with pytest.raises(ValueError):
        eg.LtbfSystem(explicit=True, R=2, C=4).run()


def test_pysim_at_256_joint_and_per_ue():
    """Step 4.2 at 16 x 16: a 10-UE sector jointly, then each UE alone with H kept."""
    s = eg.LtbfSystem(nmax=256, cmax=240, mmax=80, R=4, C=4)
    s.run()
    assert s.tb.dut.jobs == len(s.jobs()) == 12


def test_the_build_sets_are_valid_and_registered_apart():
    """Step 4.4: every registered build is a valid engine; the held-out builds are not fitted."""
    from examples.ltbf_cg.hw import space as sp

    fit, held, brute = sp.fit_set(), sp.heldout_set(), sp.brute_set()
    assert all(k.valid() for k in fit + held + brute)
    assert not set(fit) & set(held) and len(held) >= 8
    assert {k.nmax for k in fit} == set(sp.NMAX) and any(k.explicit for k in held)
    assert not any(k.explicit and k.nmax > sp.EXPLICIT_NMAX for k in fit + held + brute)


def _flat_overheads():
    from examples.ltbf_cg.hw import models as md

    return md.Overheads(
        a_T=10, a_U=10, a_S=10, a_Q=10, a_rz=5, a_v=20, d_alpha=40, d_beta=40
    )


def test_time_model_counts_jobs_regimes_and_units():
    """Step 5.1: a sector's columns run in jobs of <= mmax (the first loads H), regime 2 solves
    N_rx columns, sectors spread over the units longest-first, the A53's UEs over its cores.
    """
    from examples.ltbf_cg.hw import models as md
    from examples.ltbf_cg.hw import space as sp
    from examples.ltbf_cg.hw import timing as tm

    o = _flat_overheads()
    k = sp.Knobs(False, 64, 240, 16, 2, 2, 16)
    sectors = [(20, 4), (20, 2), (20, 0)]  # 4 and 2 own UEs, one empty sector
    run = tm.Run(t=3, regime=1, units=1, eig="pl")
    want_s0 = md.job_cycles_rtl(k, o, 64, 160, 16, 3, True, 0) + md.job_cycles_rtl(
        k, o, 64, 160, 16, 3, False, 0
    )  # 32 columns = two jobs of 16
    want_s1 = md.job_cycles_rtl(k, o, 64, 160, 16, 3, True, 0)
    pl = 6 * tm.pl_eig_cycles(k, o, 64, 1)
    a53 = 2 * tm.a53_ue_cycles(64, 1, "pl") / tm.A53_HZ  # 6 UEs over 4 cores: 2 rounds
    got = tm.update_time_s(k, o, run, sectors, 0.0)
    assert got == pytest.approx((want_s0 + want_s1 + pl) * 4e-9 + a53, rel=1e-12)
    # three units: the longest sector alone
    got3 = tm.update_time_s(k, o, tm.Run(3, 1, 3, "pl"), sectors, 0.0)
    assert got3 == pytest.approx(
        (want_s0 + 4 * tm.pl_eig_cycles(k, o, 64, 1)) * 4e-9 + a53, rel=1e-12
    )
    # regime 2: N_rx = 64 columns per sector with own UEs = 4 jobs each
    r2 = tm.update_time_s(k, o, tm.Run(3, 2, 1, "a53"), sectors, 0.0)
    jobs = md.job_cycles_rtl(k, o, 64, 160, 16, 3, True, 0) + 3 * md.job_cycles_rtl(
        k, o, 64, 160, 16, 3, False, 0
    )
    a53_full = 2 * tm.a53_ue_cycles(64, 1, "a53") / tm.A53_HZ
    assert r2 == pytest.approx(2 * jobs * 4e-9 + a53_full, rel=1e-12)


def test_time_model_rtl_correction_is_per_tile():
    from examples.ltbf_cg.hw import models as md
    from examples.ltbf_cg.hw import space as sp

    o = _flat_overheads()
    for k in (
        sp.Knobs(False, 64, 64, 16, 2, 2, 16),
        sp.Knobs(True, 64, 64, 16, 2, 2, 16),
    ):
        a = md.job_cycles(k, o, 64, 64, 8, 4, True)
        b = md.job_cycles_rtl(k, o, 64, 64, 8, 4, True, 3.0)
        assert a - b == pytest.approx(3.0 * md.product_tiles(k, 64, 64, 8, 4, True))
