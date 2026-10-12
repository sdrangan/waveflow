"""Step 6 of plans/sionna_rt/sionna_rt.md: waveforms, power and thermal noise, numpy only."""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.rfenv.noise import (
    BOLTZMANN,
    CHUNK,
    T0,
    ThermalNoise,
    amplitude_gain,
    dbfs,
    dbm_to_w,
    gain_db_for_dbfs,
    stream_tag,
    thermal_noise_power,
    w_to_dbm,
)
from waveflow.rfenv.waveforms import ArrayWaveform, Pn, Tone, ZadoffChu
from waveflow.simulation.rf_tb import write_rf_bundle

FS = 122.88e6


def periodic_autocorr(x: np.ndarray) -> np.ndarray:
    f = np.fft.fft(x)
    return np.fft.ifft(f * np.conj(f))


# --- waveforms --------------------------------------------------------------------------------


def test_zadoff_chu_has_ideal_periodic_autocorrelation():
    zc = ZadoffChu(length=1021, root=25)
    x = zc.period()
    assert np.allclose(np.abs(x), 1.0)
    r = periodic_autocorr(x)
    assert abs(r[0] - 1021) < 1e-9
    assert np.max(np.abs(r[1:])) < 1e-9 * 1021


def test_zadoff_chu_refuses_a_root_sharing_a_factor():
    with pytest.raises(ValueError, match="coprime"):
        ZadoffChu(length=1024, root=2)


@pytest.mark.parametrize("degree", [3, 7, 10])
def test_pn_is_an_m_sequence(degree):
    x = Pn(degree=degree).period()
    n = 2**degree - 1
    assert x.shape == (n,) and set(np.unique(x.real)) == {-1.0, 1.0}
    assert abs(np.sum(x.real)) == 1  # balanced: one more of one sign
    r = periodic_autocorr(x)
    assert abs(r[0] - n) < 1e-9 and np.allclose(r[1:], -1.0)


def test_waveforms_are_indexed_by_absolute_sample():
    for w in (Tone(freq_hz=1.5e6), ZadoffChu(length=139, root=3), Pn(degree=5)):
        whole = w.samples(0, 500, FS)
        parts = np.concatenate([w.samples(n0, 50, FS) for n0 in range(0, 500, 50)])
        np.testing.assert_allclose(parts, whole, rtol=0, atol=1e-12)
        assert abs(np.mean(np.abs(whole) ** 2) - 1.0) < 1e-12  # unit power


def test_a_tone_has_its_frequency():
    x = Tone(freq_hz=-2.0e6).samples(0, 4096, FS)
    k = np.argmax(np.abs(np.fft.fft(x)))
    assert np.isclose(np.fft.fftfreq(4096, 1 / FS)[k], -2.0e6, atol=FS / 4096)


def test_a_user_array_repeats_as_given_on_its_channels():
    data = np.array([[1, 2j, 3], [4, 5, 6j]])
    w = ArrayWaveform(data=data)
    assert w.n_ch == 2 and ArrayWaveform(data=data[0]).n_ch is None
    np.testing.assert_array_equal(w.samples(2, 4, FS), data[:, [2, 0, 1, 2]])
    with pytest.raises(ValueError):
        ArrayWaveform(data=np.zeros((1, 1, 1)))


def test_a_bundle_plays_as_a_waveform(tmp_path):
    rng = np.random.default_rng(0)
    blocks = [
        rng.standard_normal((2, 8)) + 1j * rng.standard_normal((2, 8)) for _ in range(3)
    ]
    write_rf_bundle(blocks, tmp_path / "b", complex_samp=True)
    w = ArrayWaveform.from_bundle(tmp_path / "b", n_ch=2, blksize=8)
    np.testing.assert_array_equal(w.period(), np.concatenate(blocks, axis=1))


# --- power ------------------------------------------------------------------------------------


def test_power_conversions():
    assert dbm_to_w(30.0) == 1.0 and np.isclose(w_to_dbm(1e-3), 0.0)
    assert np.isclose(amplitude_gain(20.0), 10.0)
    x = 0.5 * np.exp(2j * np.pi * np.arange(100) / 7)
    assert np.isclose(dbfs(x, full_scale=1.0), 20 * np.log10(0.5))
    g = gain_db_for_dbfs(power_w=1e-9, target_dbfs=-10.0, full_scale=1.0)
    assert np.isclose(dbfs(np.sqrt(1e-9) * amplitude_gain(g) * x / 0.5, 1.0), -10.0)


def test_thermal_noise_power_is_ktbf():
    assert np.isclose(thermal_noise_power(1.0, 0.0), BOLTZMANN * T0)
    # -174 dBm/Hz at 290 K is the textbook figure
    assert np.isclose(w_to_dbm(thermal_noise_power(1.0, 0.0)), -173.98, atol=0.01)


# --- noise ------------------------------------------------------------------------------------


def test_noise_power_is_within_2_percent():
    p = thermal_noise_power(FS, noise_figure_db=7.0)
    x = ThermalNoise(p, seed=1, tag=stream_tag("rx")).samples(1, 0, 1_000_000)
    assert abs(np.mean(np.abs(x) ** 2) / p - 1.0) < 0.02


def test_noise_does_not_depend_on_blocking():
    p = thermal_noise_power(FS, 5.0)
    n_total = 3 * CHUNK + 100
    whole = ThermalNoise(p, seed=7, tag=1).samples(2, 0, n_total)
    for blk in (1, 1024):
        nz = ThermalNoise(p, seed=7, tag=1)
        parts = np.concatenate(
            [
                nz.samples(2, n0, min(blk, n_total - n0))
                for n0 in range(0, n_total, blk)
            ],
            axis=1,
        )
        np.testing.assert_array_equal(parts, whole)


def test_noise_streams_are_separate():
    p = 1.0
    a = ThermalNoise(p, seed=3, tag=stream_tag("a")).samples(2, 0, 20_000)
    b = ThermalNoise(p, seed=3, tag=stream_tag("b")).samples(1, 0, 20_000)[0]
    for u, v in ((a[0], a[1]), (a[0], b)):
        rho = abs(np.vdot(u, v)) / (np.linalg.norm(u) * np.linalg.norm(v))
        assert rho < 0.05
    # and none collides with the plain spawn of the same seed
    plain = np.random.Generator(np.random.PCG64(np.random.SeedSequence(3).spawn(1)[0]))
    assert not np.allclose(plain.standard_normal(10), a[0, :10].real * np.sqrt(2))


def test_noise_is_reproducible():
    a = ThermalNoise(1.0, seed=11, tag=5).samples(1, 1000, 64)
    b = ThermalNoise(1.0, seed=11, tag=5).samples(1, 1000, 64)
    np.testing.assert_array_equal(a, b)
