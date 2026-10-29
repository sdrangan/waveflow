"""examples/ltbf_dse signals and front end (plans/ltbf_dse/ltbf_dse.md step 2.1, AC3).

The drops are local (``examples/ltbf_dse/data/drops``, copied in step 0.4); without them the tests skip.
"""

from __future__ import annotations

import numpy as np
import pytest

from examples.ltbf_dse import frontend as fe
from examples.ltbf_dse import ltbf_link as lk
from examples.ltbf_dse import scenario as sc
from examples.ltbf_dse import signals as sg
from waveflow.rfenv.apply import reference_apply

needs_drops = pytest.mark.skipif(
    not (sc.DROPS_DIR / "drop_00001.npz").is_file(),
    reason="the LTBF drops are local data (examples/ltbf_dse/data/drops; plan step 0.4)",
)


def _qpsk(rng, shape):
    return (
        rng.choice([-1.0, 1.0], shape) + 1j * rng.choice([-1.0, 1.0], shape)
    ) / np.sqrt(2)


def _setup(n=2, snapshot=0, sector=0, drop_id=1):
    drop = sc.load_drop(drop_id)
    pc = lk.power_control(lk.channel(drop, snapshot, 8, "100MHz"), lk.TARGETS["-6_14"])
    ta = sg.timing_advance(drop, snapshot, pc.gnb_idx)
    return sg.sector_paths(drop, snapshot, sector, n, pc, ta)


@needs_drops
def test_render_equals_rfenv_reference():
    """AC3 (a): the factored rendering is rfenv's channel, path by path, to rounding."""
    paths = _setup(n=2)
    x = _qpsk(np.random.default_rng(1), (paths.a.shape[1], 3000))
    ref = reference_apply(sg.pathset(paths), x, sg.FS)
    got = sg.render(paths, x)
    assert np.linalg.norm(got - ref) <= 1e-9 * np.linalg.norm(ref)


@needs_drops
def test_grid_is_the_frequency_response_on_a_periodic_symbol():
    """AC3 (b): rendering, timing advance, FFT window and :func:`signals.frequency_response` agree.

    One OFDM symbol repeated, so every path's delay is a cyclic shift of it: the front end's grid is
    exactly ``sum_u H_u[f] X_u[f]`` up to the interpolator's in-band error (about -67 dB).
    """
    paths = _setup(n=4)
    u = paths.a.shape[1]
    grid = _qpsk(np.random.default_rng(2), (u, 1, sg.N_SC))
    sym = sg.ofdm_modulate(grid)[:, sg.CP :]  # one period, no CP
    x = np.tile(sym, (1, 4))
    y = sg.render(paths, x)
    # The third period's window, past every path's delay and the interpolator's reach.
    win = y[:, 2 * sg.N_FFT + sg.GROUP_DELAY : 3 * sg.N_FFT + sg.GROUP_DELAY]
    got = (np.fft.fft(win, axis=1) / np.sqrt(sg.N_FFT))[:, sg.SC_BINS]
    want = np.einsum("nuf,uf->nf", sg.frequency_response(paths), grid[:, 0, :])
    assert np.linalg.norm(got - want) <= 1e-3 * np.linalg.norm(want)


@needs_drops
def test_rendering_is_deterministic_and_noise_is_unit_per_re():
    """AC3 (c) and the noise normalization: the same drop renders to the same bytes; the noise is
    unit variance a resource element after the unitary FFT."""
    paths = _setup(n=2)
    x = _qpsk(np.random.default_rng(3), (paths.a.shape[1], 2 * sg.SYM_LEN + 200))
    y1 = sg.render(paths, x) + sg.noise(4, x.shape[1], drop=1, snapshot=0, sector=0)
    y2 = sg.render(paths, x) + sg.noise(4, x.shape[1], drop=1, snapshot=0, sector=0)
    assert y1.tobytes() == y2.tobytes()
    w = fe.ofdm_demodulate(sg.noise(64, 14 * sg.SYM_LEN + 64, 7, 1, 2), 14)
    assert abs(np.mean(np.abs(w) ** 2) - 1.0) < 0.01


def test_ofdm_round_trip():
    """The modulator and the front end are inverses on an ideal channel (no delay, no noise)."""
    grid = _qpsk(np.random.default_rng(4), (2, 3, sg.N_SC))
    x = sg.ofdm_modulate(grid)
    y = np.concatenate([np.zeros((2, sg.GROUP_DELAY)), x], axis=1)
    back = fe.ofdm_demodulate(y, 3)
    assert np.allclose(back, grid, atol=1e-12)
