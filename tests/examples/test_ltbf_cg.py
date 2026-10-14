"""examples/ltbf_cg (plans/mimo_cg/ltbf_cg_dse.md): the LTBF CG design-space exploration.

The scene tests trace the repo-local Denver scene copy (``examples/ltbf_cg/data/``, local, never
committed), so they skip where it is missing or Sionna is not installed.  They are deliberately not
``sionna``-marked: a ``-m sionna`` session fails on any skip, and every other checkout lacks the
copy.  The rest run on synthetic channels.
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from examples.ltbf_cg import ltbf_link as lk
from examples.ltbf_cg import scenario as sc

SCENE_MISSING = not sc.SCENE_XML.is_file() or importlib.util.find_spec("sionna") is None
needs_scene = pytest.mark.skipif(
    SCENE_MISSING,
    reason="needs the local Denver scene copy (plan step 0.3) and Sionna RT",
)


def _random_channel(rng, n_sect=3, n_ant=16, n_ue=30, n_f=12):
    """Unscaled channels spanning outage, the target range and above it."""
    h = rng.standard_normal((n_sect, n_ant, n_ue, n_f)) + 1j * rng.standard_normal(
        (n_sect, n_ant, n_ue, n_f)
    )
    gain_db = rng.uniform(-150.0, -60.0, size=n_ue)  # path gain per UE
    return h * np.sqrt(10.0 ** (0.1 * gain_db) / n_ant)[None, None, :, None]


# --- the link model, no Sionna ------------------------------------------------------------------


def test_link_power_control_lands_connected_ues_on_target():
    rng = np.random.default_rng(1)
    h = _random_channel(rng)
    lo, hi = lk.TARGETS["-6_14"]
    pc = lk.power_control(h, (lo, hi))
    hs = lk.scaled(h, pc)
    gain = (np.abs(hs) ** 2).mean(axis=3).sum(axis=1)  # (S, U)
    snr_max = (
        lk.PTX_MAX_DBM
        + 10 * np.log10((np.abs(h) ** 2).mean(axis=3).sum(axis=1).max(axis=0))
        - lk.NOISE_FLOOR_DBM
    )
    assert pc.iconn.size > 0 and pc.iconn.size < h.shape[2]  # both kinds present
    for u in range(h.shape[2]):
        if u in pc.iconn:
            got = 10 * np.log10(gain[pc.gnb_idx[u], u])
            assert got == pytest.approx(min(snr_max[u], hi), abs=1e-9)
            assert got >= lo - 1e-9
        else:
            assert pc.gnb_idx[u] == -1 and pc.scale[u] == 0.0


def test_link_long_term_inputs_have_the_papers_structure():
    """Noiseless SRS: every UE's block of H_alpha is sqrt(1/N_SRS) times its channel at 8 distinct
    frequency samples, the same samples in every sector; a sector's right-hand sides are its own
    UEs' unscaled blocks; Q = I + sum_j H_j H_j^H / N_SRS over the connected UEs."""
    rng = np.random.default_rng(2)
    h = _random_channel(rng)
    pc = lk.power_control(h, lk.TARGETS["-6_14"])
    hs = lk.scaled(h, pc)
    lt = lk.long_term(hs, pc, np.random.default_rng(3), g_srs_db=None)
    idx: dict[int, list[int]] = {}
    for s in range(3):
        ha = lt.h_alpha[s]
        assert ha.shape == (h.shape[1], lk.N_SRS * pc.iconn.size)
        want = np.eye(h.shape[1], dtype=complex)
        for k, u in enumerate(pc.iconn):
            blk = ha[:, k * lk.N_SRS : (k + 1) * lk.N_SRS] * np.sqrt(lk.N_SRS)
            # which frequency sample each column is: it must equal one exactly
            d = np.abs(hs[s, :, u, :, None] - blk[:, None, :]).max(axis=0)  # (F, 8)
            cols = d.argmin(axis=0)
            assert np.all(d[cols, np.arange(lk.N_SRS)] < 1e-15)
            assert len(set(cols.tolist())) == lk.N_SRS
            assert (
                idx.setdefault(int(u), cols.tolist()) == cols.tolist()
            )  # same in every sector
            want += hs[s, :, u, cols].T @ hs[s, :, u, cols].conj() / lk.N_SRS
        np.testing.assert_allclose(lk.q_hat(ha), want, rtol=1e-12, atol=1e-12)
        own = lt.own[s]
        assert set(own.tolist()) == {u for u in pc.iconn if pc.gnb_idx[u] == s}
        assert lt.rhs[s].shape == (h.shape[1], lk.N_SRS * own.size)
        for k, u in enumerate(own):
            blk = lt.rhs[s][:, k * lk.N_SRS : (k + 1) * lk.N_SRS]
            np.testing.assert_array_equal(blk, lt.srs_own[int(u)])
            np.testing.assert_array_equal(blk, hs[s, :, u, idx[int(u)]].T)
    # with SRS noise: the same shapes, and Q >= I
    ltn = lk.long_term(hs, pc, np.random.default_rng(3), g_srs_db=lk.G_SRS_DB)
    for s in range(3):
        assert np.linalg.eigvalsh(lk.q_hat(ltn.h_alpha[s])).min() >= 1.0 - 1e-9


def test_link_srs_noise_is_at_the_processing_gain():
    h = np.zeros(
        (3, 64, 30, 69), dtype=complex
    )  # no signal: the SRS is pure estimation noise
    pc = lk.PowerControl(np.zeros(30, int), np.arange(30), np.ones(30))
    lt = lk.long_term(h, pc, np.random.default_rng(5), g_srs_db=10.0)
    noise = np.concatenate([lt.h_alpha[s].ravel() for s in range(3)]) * np.sqrt(
        lk.N_SRS
    )
    assert np.mean(np.abs(noise) ** 2) == pytest.approx(
        0.1, rel=0.02
    )  # 138,240 samples
    lt0 = lk.long_term(h, pc, np.random.default_rng(5), g_srs_db=None)
    assert not np.any(lt0.h_alpha[0])


# --- the scenario and the trace, Sionna ---------------------------------------------------------


@needs_scene
def test_scenario_drop_follows_the_papers_geometry():
    grid = sc.coverage_grid()
    d = sc.make_drop(7, grid)
    assert d.ue_pos.shape == (sc.N_SECT * sc.N_UE, 3)
    horiz = np.hypot(d.ue_pos[:, 0] - d.gnb_pos[0], d.ue_pos[:, 1] - d.gnb_pos[1])
    assert horiz.max() < sc.DIST_RANGE[1]
    speed = np.hypot(d.ue_vel[:, 0], d.ue_vel[:, 1])
    assert speed.max() <= sc.SPEED_RANGE[1] and not np.any(d.ue_vel[:, 2])
    ang = np.arctan2(d.ue_pos[:, 1] - d.gnb_pos[1], d.ue_pos[:, 0] - d.gnb_pos[0])
    for s in range(sc.N_SECT):
        off = np.angle(np.exp(1j * (ang[d.ue_sector == s] - d.yaws[s])))
        assert np.all(np.abs(off) < sc.RX_BEAMWIDTH / 2)
    d2 = sc.make_drop(7, grid)
    np.testing.assert_array_equal(d.ue_pos, d2.ue_pos)


@needs_scene
def test_synthetic_expansion_matches_sionna():
    """One element per sector plus lk.channel's array = Sionna's own synthetic 4x4 array."""
    from waveflow.rfenv.sionna_backend import ArraySpec, trace_with_paths

    d = sc.make_drop(1, sc.coverage_grid())
    rec = sc.trace_snapshot(d, 0)
    drop = {
        "yaws": d.yaws,
        "a0": rec["a"],
        "tau0": rec["tau"],
        "valid0": rec["valid"],
        "k_rx0": rec["k_rx"],
    }
    arr = ArraySpec(num_rows=4, num_cols=4, pattern="tr38901", polarization="V")
    ps4, _ = trace_with_paths(sc.scene_spec(d, 0, rx_array=arr))
    f = lk.band_frequencies("1.44MHz")
    a4 = np.where(ps4.valid, ps4.a, 0)
    t4 = np.where(ps4.valid, ps4.tau, 0)
    h4 = np.einsum("rtp,rtpf->rtf", a4, np.exp(-2j * np.pi * t4[..., None] * f))
    h4 = h4.reshape(3, 16, 30, -1)
    mine = lk.channel(drop, 0, 4, "1.44MHz")
    assert np.linalg.norm(mine - h4) / np.linalg.norm(h4) < 1e-6


@needs_scene
def test_drops_retrace_bit_for_bit(tmp_path):
    """AC1: a drop traced again from its seed is identical, and so is the local committed-run file."""
    grid = sc.coverage_grid()
    a = sc.trace_drop(3, grid, out_dir=tmp_path / "a")
    b = sc.trace_drop(3, grid, out_dir=tmp_path / "b")
    with np.load(a) as fa, np.load(b) as fb:
        assert fa.files == fb.files
        for k in fa.files:
            np.testing.assert_array_equal(fa[k], fb[k])
    local = sc.drop_files(3)[0]
    if local.is_file():
        with np.load(a) as fa, np.load(local) as fl:
            for k in fa.files:
                np.testing.assert_array_equal(fa[k], fl[k])


# --- the float reference (AC2), no Sionna ---------------------------------------------------------


def _system(rng, n=32, cols=64, rhs=16, scale=1.0):
    ha = (
        scale
        * (rng.standard_normal((n, cols)) + 1j * rng.standard_normal((n, cols)))
        / 4
    )
    b = rng.standard_normal((n, rhs)) + 1j * rng.standard_normal((n, rhs))
    return ha, b


@pytest.mark.parametrize("form", ["factored", "explicit"])
@pytest.mark.parametrize("regime", [1, 2])
def test_float_cg_converges_to_the_direct_solve(form, regime):
    """AC2 (a): run long enough, both forms and both right-hand sides reach the direct solve."""
    from examples.ltbf_cg import ltbf as lt

    rng = np.random.default_rng(11)
    ha, b = _system(rng)
    if regime == 2:
        b = np.eye(ha.shape[0], dtype=complex)
    apply = lt.factored_s(ha) if form == "factored" else lt.explicit_s(ha)
    q = lk.q_hat(ha)
    # the recursion itself, without the paper's stop (which ends a column at r^H r < 1e-10 by
    # design; test_float_cg_stops_where_the_paper_does covers it)
    xs = lt.cg_iterates(apply, b, 4 * ha.shape[0], stop=0.0)
    x = xs[-1]
    res = np.linalg.norm(b - q @ x, axis=0) / np.linalg.norm(b, axis=0)
    assert res.max() < 1e-12
    want = np.linalg.solve(q, b)
    assert np.linalg.norm(x - want) / np.linalg.norm(want) <= 1e-8


def test_float_cg_stops_where_the_paper_does():
    """cg_solve_complex leaves a column once r^H r < 1e-10: from then on its iterate is frozen,
    while the other columns keep moving."""
    from examples.ltbf_cg import ltbf as lt

    rng = np.random.default_rng(20)
    ha, b = _system(rng, n=16, cols=24, rhs=4)
    b[:, 0] *= 1e-6  # converges below the threshold within a few iterations
    xs = lt.cg_iterates(lt.factored_s(ha), b, 30)
    q = lk.q_hat(ha)
    res = [np.linalg.norm(b - q @ x, axis=0) ** 2 for x in xs]
    first = next(t for t, rr in enumerate(res) if rr[0] < lt.STOP_RS)
    assert first < 10
    for t in range(first + 1, len(xs)):
        np.testing.assert_array_equal(xs[t][:, 0], xs[first][:, 0])
    assert np.any(xs[first + 1][:, 1] != xs[first][:, 1])


def test_float_explicit_and_factored_products_agree():
    """AC2 (b): for the same D, S = Q D and S = D + H_alpha (H_alpha^H D) agree to 1e-12."""
    from examples.ltbf_cg import ltbf as lt

    rng = np.random.default_rng(12)
    ha, d = _system(rng, n=64, cols=240, rhs=80)
    se, sf = lt.explicit_s(ha)(d), lt.factored_s(ha)(d)
    assert np.linalg.norm(se - sf) / np.linalg.norm(se) <= 1e-12


def test_float_exact_projection_spans_the_top_eigenvectors():
    """The canonical G_i spans the top-r eigenvectors of Q^-1 Q_i (paper eq. (Gicanon_exact))."""
    from examples.ltbf_cg import ltbf as lt

    rng = np.random.default_rng(13)
    ha, _ = _system(rng, n=32, cols=48)
    h_i = ha[:, :8] * np.sqrt(lk.N_SRS)
    q = lk.q_hat(ha)
    a_i = np.linalg.solve(q, h_i)
    g = lt.projection(a_i, h_i, 2)
    w, v = np.linalg.eig(np.linalg.solve(q, h_i @ h_i.conj().T))
    top = v[:, np.argsort(-w.real)[:2]]
    # the row space of G equals the span of the two principal eigenvectors
    p_top = top @ np.linalg.pinv(top)
    gh = g.conj().T
    assert np.linalg.norm(p_top @ gh - gh) / np.linalg.norm(gh) < 1e-8


# --- the fixed-point solve (AC3), no Sionna ------------------------------------------------------


def _states(ha, b, f, form, t_max=10):
    from examples.ltbf_cg import ltbf_fixed as lf

    out = []
    lf.solve_fixed(
        ha, b, f, t_max, form, on_iteration=lambda n, st, sc: out.append((st.xr, st.xi))
    )
    return out


@pytest.mark.parametrize("form", ["factored", "explicit"])
def test_fixed_per_ue_and_joint_solves_are_bit_identical(form):
    """Per-column omega and nu with static formats: solving the own UEs jointly or one at a time
    gives the same stored X, so grouping is a hardware axis only (plan §9 b)."""
    from examples.ltbf_cg import ltbf as lt
    from examples.ltbf_cg import ltbf_fixed as lf

    rng = np.random.default_rng(14)
    ha, _ = _system(rng, n=16, cols=48)
    b = ha[:, :24] * np.sqrt(lk.N_SRS)  # three own UEs
    f = lf.formats_for(12, lf.profile_ranges(ha, b, 10))
    joint = _states(ha, b, f, form)
    xs_joint = lt.cg_iterates(lt.factored_s(ha), b, 10)
    for k in range(3):
        cols = slice(k * lk.N_SRS, (k + 1) * lk.N_SRS)
        per = _states(ha, b[:, cols], f, form)
        for (jr, ji), (pr, pi) in zip(joint, per, strict=True):
            np.testing.assert_array_equal(jr[:, cols], pr)
            np.testing.assert_array_equal(ji[:, cols], pi)
        xs_per = lt.cg_iterates(lt.factored_s(ha), b[:, cols], 10)
        for xj, xp in zip(xs_joint, xs_per, strict=True):
            np.testing.assert_allclose(xj[:, cols], xp, rtol=1e-12, atol=1e-12)


@pytest.mark.parametrize("form", ["factored", "explicit"])
def test_fixed_zero_column_stays_zero(form):
    """A zero right-hand side takes both zero guards (rz = ps = 0): its X stays exactly zero."""
    from examples.ltbf_cg import ltbf_fixed as lf

    rng = np.random.default_rng(15)
    ha, b = _system(rng, n=16, cols=48, rhs=8)
    b[:, 2] = 0
    f = lf.formats_for(12, lf.profile_ranges(ha, b, 10))
    for xr, xi in _states(ha, b, f, form):
        assert not np.any(xr[:, 2]) and not np.any(xi[:, 2])
        assert np.any(xr[:, 3])


def test_fixed_24_bit_formats_fit_64_bits_at_1024_antennas():
    """AC3's 24-bit check is buildable at the largest array: every exact intermediate <= 64 bits."""
    from examples.ltbf_cg import ltbf_fixed as lf

    rng = np.random.default_rng(16)
    ha, b = _system(rng, n=32, cols=96, rhs=16, scale=2.0)
    lf.check_widths(lf.formats_for(24, lf.profile_ranges(ha, b, 10)), 1024, 240)


def test_fixed_24_bit_sinr_equals_the_float_pipeline():
    """AC3: at W = 24 the fixed-point LTBF SINR equals the float pipeline's within 0.01 dB, for
    every t <= 10, rank, regime and Q form."""
    from examples.ltbf_cg import ltbf as lt
    from examples.ltbf_cg import ltbf_fixed as lf

    rng = np.random.default_rng(17)
    h0 = _random_channel(rng, n_ant=16, n_f=12)
    h1 = h0 + 0.1 * _random_channel(rng, n_ant=16, n_f=12)
    target = lk.TARGETS["-6_14"]
    pc0 = lk.power_control(h0, target)
    ltu = lk.long_term(lk.scaled(h0, pc0), pc0, np.random.default_rng(18), lk.G_SRS_DB)
    f = lf.formats_for(24, lf.profile_link(ltu, 10))
    solves = tuple(
        lt.Solve(form, reg) for form in ("factored", "explicit") for reg in (1, 2)
    )
    ev = lt.evaluate_channels(
        h0, h1, target, lk.G_SRS_DB, np.random.default_rng(18),
        solves=solves, fixed={"W24": f}, fixed_solves=solves,
    )  # fmt: skip
    assert ev.served.size >= 10
    worst = 0.0
    for (_, form, reg, t, r), per_ue in ev.fixed.items():
        ref = ev.approx[(form, reg, t, r)]
        assert per_ue.keys() == ref.keys()
        worst = max(worst, max(abs(per_ue[u] - ref[u]) for u in ref))
    assert worst <= 0.01, worst


@pytest.mark.parametrize("adjoint", [False, True])
@pytest.mark.parametrize(
    ("wa", "wb", "k"), [(10, 12, 48), (16, 16, 256), (24, 24, 1024), (24, 28, 240)]
)
def test_fixed_blas_product_equals_matmul_exact(adjoint, wa, wb, k):
    """The sweep's float64-BLAS product is waveflow.linalg's matmul_exact bit for bit, including
    the extreme stored values (the most negative one is where a negation would overflow).
    """
    from examples.ltbf_cg import ltbf_fixed as lf
    from waveflow.linalg.matmul import matmul_exact

    rng = np.random.default_rng(wa * 1000 + wb + k)
    fa, fb = lf.reg(wa, 3), lf.reg(wb, 5)
    lo_a, hi_a = -(1 << (wa - 1)), (1 << (wa - 1)) - 1
    lo_b, hi_b = -(1 << (wb - 1)), (1 << (wb - 1)) - 1
    shape_a = (k, 12) if adjoint else (12, k)
    ar, ai = (rng.integers(lo_a, hi_a + 1, size=shape_a) for _ in range(2))
    br, bi = (rng.integers(lo_b, hi_b + 1, size=(k, 7)) for _ in range(2))
    ar[0, :], ai[0, :], br[:, 0], bi[:, 0] = lo_a, lo_a, lo_b, lo_b  # the extremes
    ai[1, :], bi[:, 1] = hi_a, hi_b
    want = matmul_exact(ar, ai, fa, br, bi, fb, adjoint=adjoint)
    got = lf.mm_exact(ar, ai, fa, br, bi, fb, adjoint=adjoint)
    np.testing.assert_array_equal(got[0], want[0])
    np.testing.assert_array_equal(got[1], want[1])
    assert got[2] == want[2]


def test_fixed_degenerate_projection_is_floored_not_an_error():
    """A fixed-point A_i of all zeros gives G_i = 0: the UE receives nothing (the SINR floor)."""
    from examples.ltbf_cg import ltbf as lt

    rng = np.random.default_rng(19)
    h = _random_channel(rng, n_ant=16, n_f=12)
    pc = lk.power_control(h, lk.TARGETS["-6_14"])
    hs = lk.scaled(h, pc)
    u0, u1 = (int(u) for u in pc.iconn[:2])
    g = {u0: np.zeros((2, 16), complex), u1: rng.standard_normal((2, 16)) + 0j}
    g[u1][1] = g[u1][0]  # rank 1 with r = 2
    out = lt.sinr_lt_db(g, hs, pc, np.array([u0, u1]))
    assert out[u0] == pytest.approx(10 * np.log10(lt.SINR_FLOOR))
    assert np.isfinite(out[u1]) and out[u1] > 10 * np.log10(lt.SINR_FLOOR)
