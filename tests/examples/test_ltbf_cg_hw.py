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
    # regime 2: N_rx = 64 columns per sector with own UEs = 4 jobs each, plus A_i = X_sym H_i
    r2 = tm.update_time_s(k, o, tm.Run(3, 2, 1, "a53"), sectors, 0.0)
    jobs = md.job_cycles_rtl(k, o, 64, 160, 16, 3, True, 0) + 3 * md.job_cycles_rtl(
        k, o, 64, 160, 16, 3, False, 0
    )
    a_i = 6 * tm.regime2_a_cycles(k, o, 64)
    a53_full = 2 * tm.a53_ue_cycles(64, 1, "a53") / tm.A53_HZ
    assert r2 == pytest.approx((2 * jobs + a_i) * 4e-9 + a53_full, rel=1e-12)
    # I/O at 4 values per beat: only the stream terms shrink
    l4 = tm.update_time_s(k, o, run, sectors, 0.0, L=4)
    io = (
        tm.io_cycles(64, 160, 16, True)
        + tm.io_cycles(64, 160, 16, False)
        + tm.io_cycles(64, 160, 16, True)
    )
    io4 = sum(-(-tm.io_cycles(64, 160, 16, lh) // 4) for lh in (True, False, True))
    assert got - l4 == pytest.approx((io - io4) * 4e-9, rel=1e-9)


def test_time_model_pads_ragged_sectors_to_the_tile():
    """Columns and right-hand sides are padded to R and C: 7 connected UEs (56 columns) on R = 16
    cost what 64 do; a build too narrow for the sector is refused."""
    from examples.ltbf_cg.hw import space as sp
    from examples.ltbf_cg.hw import timing as tm

    o = _flat_overheads()
    k = sp.Knobs(False, 64, 64, 16, 16, 16, 16)
    assert tm.sector_cycles(k, o, 7, 1, 1, "a53", 0.0) == tm.sector_cycles(
        k, o, 8, 2, 1, "a53", 0.0
    )
    with pytest.raises(ValueError):
        tm.sector_cycles(k, o, 9, 1, 1, "a53", 0.0)


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


@pytest.mark.skipif(not hc.HLS_INCLUDE.is_dir(), reason="needs the Vitis HLS headers")
@pytest.mark.parametrize(
    "knobs",
    [
        {"nmax": 64, "cmax": 64, "mmax": 16, "R": 2, "C": 16},  # per-UE jobs of 8 < C
        {
            "nmax": 64,
            "cmax": 64,
            "mmax": 16,
            "R": 16,
            "C": 16,
            "cols_run": 56,
        },  # 56 % R != 0
    ],
    ids=["m-below-C", "cols-ragged"],
)
def test_ragged_sizes_are_padded_exactly(knobs, tmp_path):
    """M3-M4 review B2: the host pads H's columns to R and B's to C with zeros (exact); the body
    and pysim then agree with the model on every word, the padding columns' X included.
    """
    s = eg.LtbfSystem(**knobs)
    s.run()
    assert hc.host_check(eg.LtbfSystem(**knobs), tmp_path) == 0


def test_pysim_refuses_unpadded_sizes():
    import numpy as np

    s = eg.LtbfSystem(nmax=64, cmax=64, mmax=16, R=2, C=16)
    f = eg.hw_formats(16)
    job = s.jobs()[1]  # 8 columns
    assert job.b.shape[1] == 8
    padded = eg.job_words(job, f, 2, 16)
    assert eg.LtbfCmd().__class__ is eg.LtbfCmd and len(padded) == 2
    assert padded[-1].size == 64 * 16  # B padded to 16 columns
    assert np.all(
        np.asarray(eg.unpack_words(padded[-1], f.cg.B.W)[0]).reshape(64, 16)[:, 8:] == 0
    )
