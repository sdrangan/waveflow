"""examples/ltbf_cg (plans/mimo_cg/ltbf_cg_dse.md): the LTBF CG design-space exploration.

The ``sionna``-marked tests trace the repo-local Denver scene copy (``examples/ltbf_cg/data/``,
local, not committed); the rest run on synthetic channels.
"""

from __future__ import annotations

import numpy as np
import pytest

from examples.ltbf_cg import ltbf_link as lk
from examples.ltbf_cg import scenario as sc

SCENE_MISSING = not sc.SCENE_XML.is_file()


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
    rng = np.random.default_rng(2)
    h = _random_channel(rng)
    pc = lk.power_control(h, lk.TARGETS["-6_14"])
    hs = lk.scaled(h, pc)
    lt = lk.long_term(hs, pc, np.random.default_rng(3), g_srs_db=lk.G_SRS_DB)
    for s in range(3):
        ha = lt.h_alpha[s]
        assert ha.shape == (h.shape[1], lk.N_SRS * pc.iconn.size)
        q = lk.q_hat(ha)
        # Q = I + sum_j Qhat_j, Qhat_j = Hj Hj^H / N_SRS, over every connected UE
        want = np.eye(h.shape[1], dtype=complex)
        for k, u in enumerate(pc.iconn):
            hj = ha[:, k * lk.N_SRS : (k + 1) * lk.N_SRS] * np.sqrt(lk.N_SRS)
            want += hj @ hj.conj().T / lk.N_SRS
        np.testing.assert_allclose(q, want, rtol=1e-12, atol=1e-12)
        assert np.linalg.eigvalsh(q).min() >= 1.0 - 1e-9
        assert set(lt.own[s].tolist()) == {u for u in pc.iconn if pc.gnb_idx[u] == s}
        assert lt.rhs[s].shape == (h.shape[1], lk.N_SRS * lt.own[s].size)


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


@pytest.mark.sionna
@pytest.mark.skipif(
    SCENE_MISSING, reason="the Denver scene copy is local (plan step 0.3)"
)
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


@pytest.mark.sionna
@pytest.mark.skipif(
    SCENE_MISSING, reason="the Denver scene copy is local (plan step 0.3)"
)
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


@pytest.mark.sionna
@pytest.mark.skipif(
    SCENE_MISSING, reason="the Denver scene copy is local (plan step 0.3)"
)
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
    xs = lt.cg_iterates(apply, b, 4 * ha.shape[0])
    x = xs[-1]
    res = np.linalg.norm(b - q @ x, axis=0) / np.linalg.norm(b, axis=0)
    assert res.max() < 1e-12
    want = np.linalg.solve(q, b)
    assert np.linalg.norm(x - want) / np.linalg.norm(want) <= 1e-8


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
