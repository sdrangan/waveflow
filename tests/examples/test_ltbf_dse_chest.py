"""examples/ltbf_dse/chest.py (plans/ltbf_dse/ltbf_dse.md step 2.2, AC4): the estimators on synthetic
channels whose truth is known, and their op counts against the spec's closed forms (spec §2).
"""

from __future__ import annotations

import numpy as np

from examples.ltbf_dse import chest as ce
from examples.ltbf_dse import signals as sg
from examples.ltbf_dse.opcount import OpLog

N_ANT = 16
G_SRS_DB = 16.14


def _flat_srs(rng, n_ue, noise):
    """Every UE flat in frequency (a random spatial vector): the SRS symbol at the antennas."""
    h = (
        rng.standard_normal((N_ANT, n_ue)) + 1j * rng.standard_normal((N_ANT, n_ue))
    ) / np.sqrt(2)
    y = h @ sg.srs_grid(n_ue)[:, 0, :]
    if noise:
        y = y + (
            rng.standard_normal(y.shape) + 1j * rng.standard_normal(y.shape)
        ) / np.sqrt(2)
    return h, y


def test_srs_ports_separate_on_whole_taps():
    """AC4 (a), leakage: noiseless, every one of 30 UEs flat (delay 0, a whole tap): each UE's 8
    snapshots equal its channel to better than -30 dB."""
    h, y = _flat_srs(np.random.default_rng(1), 30, noise=False)
    blocks = ce.srs_blocks(y, np.arange(30), OpLog())
    for u, b in blocks.items():
        err = np.sum(np.abs(b - h[:, [u]]) ** 2) / np.sum(
            np.abs(h[:, [u]] * np.ones(8)) ** 2
        )
        assert 10 * np.log10(err + 1e-300) < -30


def test_srs_processing_gain():
    """AC4 (a), noise: the estimates' error variance is the per-RE noise less G_SRS = 16.14 dB
    (within 1 dB), the comb's 4x energy times the window's noise gain (spec §2, spike f).
    """
    rng = np.random.default_rng(2)
    errs = []
    for _ in range(20):
        h, y = _flat_srs(rng, 30, noise=True)
        for u, b in ce.srs_blocks(y, np.arange(30), OpLog()).items():
            errs.append(np.mean(np.abs(b - h[:, [u]]) ** 2))
    g_db = -10 * np.log10(np.mean(errs))
    assert abs(g_db - G_SRS_DB) < 1.0


def _dmrs_case(delay_sc, other_port=True):
    """A sector's DMRS through a known effective channel (2 x 792) with a linear phase of
    ``delay_sc`` radians a subcarrier, plus another UE on another port of the same sector.
    """
    rng = np.random.default_rng(3)
    r, port, sector = 2, 4, 1
    f = np.arange(sg.N_SC)
    h = (rng.standard_normal((r, 1)) + 1j * rng.standard_normal((r, 1))) * np.exp(
        -1j * delay_sc * f
    )
    gnb = np.array([sector, sector] if other_port else [sector])
    data = sg.qpsk_data(gnb.size, 5)
    grid = sg.pusch_grid(gnb, data)  # UE 0 on port 0, UE 1 on port 1 of the sector
    port = int(sg.dmrs_ports(gnb)[0])
    z = h[:, None, :] * grid[0][None, list(sg.DMRS_SYMS), :]
    if other_port:
        h2 = (rng.standard_normal((r, 1)) + 1j * rng.standard_normal((r, 1))) * np.ones(
            sg.N_SC
        )
        z = z + h2[:, None, :] * grid[1][None, list(sg.DMRS_SYMS), :]
    return h, z, port, sector


def test_dmrs_exact_when_noiseless_flat_and_delayed():
    """AC4 (b): noiseless, with another UE on an orthogonal port of the same sector. A flat channel
    comes out exact (1e-9). A delayed one comes out scaled by ``cos(delta / 2)`` exactly, ``delta`` its
    phase step a subcarrier: FD-OCC despreading averages two adjacent subcarriers before the delay is
    removed (0.055 dB for a path 300 ns late), the rest of the chain exact."""
    for delay in (0.0, 0.02):
        h, z, port, sector = _dmrs_case(delay)
        got, w = ce.dmrs_weights(z, port, sector, OpLog())
        assert np.max(np.abs(got - h * np.cos(delay / 2))) < 1e-9
        assert np.allclose(w, got / (1 + np.sum(np.abs(got) ** 2, axis=0)), atol=1e-12)


def test_op_counts_match_the_spec():
    """AC4 (c): the logged counts equal spec §2's closed forms (three shapes each)."""
    for n_ue in (8, 17, 30):
        _h, y = _flat_srs(np.random.default_rng(4), n_ue, noise=False)
        log = OpLog()
        ce.srs_blocks(y, np.arange(n_ue), log)
        ports = np.bincount(np.arange(n_ue) % sg.K_TC, minlength=sg.K_TC)
        want = sum(N_ANT * 192 + N_ANT * 192 * 8 * int(p) for p in ports if p)
        assert log.totals("srs")["cmacs"] == want
    for r in (1, 2):
        _h, z, port, sector = _dmrs_case(0.01, other_port=False)
        log = OpLog()
        ce.dmrs_weights(z[:r], port, sector, log)
        nb = sg.N_SC // 6
        t = log.totals("dmrs")
        # despread 4r a block, delay removal r a block, interpolation 2r and the delay restored r a
        # subcarrier, |h|^2 r a subcarrier, the weight r cmacs and one divide a subcarrier
        assert t["cmacs"] == 4 * r * nb + r * nb + (2 * r + r + r + r) * sg.N_SC
        assert t["divides"] == sg.N_SC
