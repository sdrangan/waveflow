"""The emitter detection and AoA example: the workload, the golden and the scorer (no toolchain)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from examples.aoa_detect.array import steering
from examples.aoa_detect.golden import AoaGolden, GoldenConfig, chi2_isf, requant
from examples.aoa_detect.score import score
from examples.aoa_detect.workload import (
    Emitter,
    Workload,
    WorkloadConfig,
    input_gain_tradeoff,
    place_emitter,
)


def test_steering_is_unit_modulus_and_follows_the_docs_ordering():
    a = steering(0.3, -0.1, 4, 2)
    assert a.shape == (8,)
    assert np.allclose(np.abs(a), 1.0)
    # element i = n * n_h + m: column m, row n
    m, n = 2, 1
    expect = np.exp(1j * np.pi * (m * math.cos(-0.1) * math.sin(0.3) + n * math.sin(-0.1)))
    assert a[n * 4 + m] == pytest.approx(expect)


def test_chi2_isf_matches_tabulated_values():
    assert chi2_isf(0.05, 2) == pytest.approx(5.9915, abs=1e-3)
    assert chi2_isf(0.01, 10) == pytest.approx(23.209, abs=1e-2)
    # large dof: about mean + z * sqrt(2 dof), z(1e-3) = 3.09
    x = chi2_isf(1e-3, 4096)
    assert (x - 4096) / math.sqrt(2 * 4096) == pytest.approx(3.15, abs=0.05)


def test_input_gain_tradeoff():
    t = input_gain_tradeoff(16, 4.0)
    assert t["quant_db"] == pytest.approx(-19.8, abs=0.1)
    assert t["headroom_db"] == pytest.approx(69.2, abs=0.1)
    # each input bit buys 6 dB of headroom at the same noise level
    assert input_gain_tradeoff(12, 4.0)["headroom_db"] == pytest.approx(45.2, abs=0.1)
    assert input_gain_tradeoff(8, 4.0)["headroom_db"] == pytest.approx(21.0, abs=0.2)


def test_geometric_snr_spread_is_as_documented():
    """workload.md: about -6 to 19 dB per element for 90% of emitters, median about 6 dB."""
    rng = np.random.default_rng(0)
    cfg = WorkloadConfig()
    snr = [place_emitter(rng, cfg)["snr_db"] for _ in range(20_000)]
    p5, p50, p95 = np.percentile(snr, [5, 50, 95])
    assert p5 == pytest.approx(-6.0, abs=1.0)
    assert p50 == pytest.approx(6.0, abs=1.0)
    assert p95 == pytest.approx(19.0, abs=1.0)


def test_workload_is_reproducible_and_in_range():
    cfg = WorkloadConfig(seed=5, C=4, B=64)
    a = list(Workload(cfg).blocks(3))
    b = list(Workload(cfg).blocks(3))
    c = list(Workload(WorkloadConfig(seed=6, C=4, B=64)).blocks(3))
    for (n1, r1, i1), (n2, r2, i2) in zip(a, b):
        assert n1 == n2 and np.array_equal(r1, r2) and np.array_equal(i1, i2)
    assert not np.array_equal(a[0][1], c[0][1])
    re = a[0][1]
    assert re.shape == (4, 64, 16)
    assert re.max() <= 2 ** 15 - 1 and re.min() >= -(2 ** 15)
    # noise at noise_lsb = 4: per real part std 4 / sqrt(2)
    assert re.std() == pytest.approx(4 / math.sqrt(2), rel=0.1)


def test_on_off_process_busy_fraction():
    """A channel is busy p_on / (p_on + p_off) of the time (burst trigger off)."""
    w = Workload(WorkloadConfig(seed=1, p_burst=0.0))
    busy = 0
    n_blocks = 5_000
    for n in range(n_blocks):
        busy += sum(e is not None for e in w._step(n))
    frac = busy / (n_blocks * w.cfg.C)
    cfg = w.cfg
    assert frac == pytest.approx(cfg.p_on / (cfg.p_on + cfg.p_off), abs=0.03)


def test_requant_rounds_saturates_and_wraps():
    v = np.array([5, 6, -6, 1000, -1000])
    assert requant(v, 2, 8, "saturate", True).tolist() == [1, 2, -1, 127, -128]   # round half up
    assert requant(np.array([200]), 0, 8, "wrap", True).tolist() == [200 - 256]
    assert requant(np.array([200]), 0, 8, "saturate", True).tolist() == [127]
    assert requant(np.array([6.0]), 2, 8, "saturate", False).tolist() == [1.5]


def _one_emitter(snr_db=10.0, phi_deg=20.0, eps_deg=-4.0, start=5, end=40, seed=2):
    sc = [Emitter(channel=3, start=start, end=end, phi=math.radians(phi_deg),
                  eps=math.radians(eps_deg), snr_db=snr_db)]
    return Workload(WorkloadConfig(seed=seed), script=sc), sc


def test_one_emitter_is_detected_located_and_released():
    w, truth = _one_emitter()
    g = AoaGolden(GoldenConfig())
    d = g.run(w.blocks(60))
    assert [(x.channel, x.block, x.kind) for x in d] == [(3, 5, "arrival"), (3, 40, "departure")]
    arr = d[0]
    step_phi = 2 * 60.0 / g.cfg.L_phi
    step_eps = 15.0 / g.cfg.L_eps
    assert abs(math.degrees(arr.phi) - 20.0) <= step_phi / 2 + 1.0
    assert abs(math.degrees(arr.eps) + 4.0) <= step_eps
    # the array SNR: n_rx x the per-element SNR
    assert 10 * math.log10(arr.gamma_hat) == pytest.approx(10 + 10 * math.log10(16), abs=1.5)
    s = score(d, truth, 60, n_rx=16)
    assert (s.detected, s.missed, s.false_alarms, s.early_stops) == (1, 0, 0, 0)
    assert s.detect_delay_blocks == [0] and s.depart_delay_blocks == [0]


def test_idle_channels_raise_no_detection():
    w = Workload(WorkloadConfig(seed=4, p_on=0.0, p_burst=0.0))
    g = AoaGolden(GoldenConfig())
    d = g.run(w.blocks(400))
    assert d == []
    # the gate fires at about pfa_gate per channel-block: 400 x 16 x 1e-3 = 6.4 expected
    assert g.counts["gate"] <= 20


def test_fixed_point_matches_float_at_the_proposed_formats():
    """The same decisions, at the same blocks; the direction the same grid cell, or a neighbour when
    two cells are nearly tied (the grid is fine enough that rounding can tip the argmax)."""
    cfg = WorkloadConfig(seed=7, p_on=1 / 20, p_off=1 / 8)
    dfix = AoaGolden(GoldenConfig()).run(Workload(cfg).blocks(300))
    gf = AoaGolden(GoldenConfig(fixed=False))
    dflt = gf.run(Workload(cfg).blocks(300))
    assert len(dfix) > 10
    assert [(x.channel, x.block, x.kind) for x in dfix] == [(x.channel, x.block, x.kind) for x in dflt]
    L_eps = gf.cfg.L_eps
    same = 0
    for a, b in zip(dfix, dflt):
        da, de = abs(a.ell // L_eps - b.ell // L_eps), abs(a.ell % L_eps - b.ell % L_eps)
        assert da <= 1 and de <= 1
        same += a.ell == b.ell
    assert same >= 0.9 * len(dfix)


def _q_stored_max(snr_db, **cfg):
    w, _ = _one_emitter(snr_db=snr_db)
    _, re, im = list(w.blocks(6))[5]
    g = AoaGolden(GoldenConfig(**cfg))
    xr, xi = re[3], im[3]
    Qr = requant(xr.T @ xr + xi.T @ xi, g.q_shift, g.cfg.q_bits, "saturate", True)
    return int(Qr.max()), (1 << (g.cfg.q_bits - 1)) - 1


def test_q_headroom_sets_where_a_strong_emitter_saturates():
    """q_headroom = 10 bits puts the noise about 30 dB below full scale (param.md): a 25 dB emitter
    fits, a 40 dB one (the workload's strong tail) saturates Q̂, and with 2 bits a 25 dB one does too."""
    got, full = _q_stored_max(25.0)
    assert got < full
    got, full = _q_stored_max(40.0)
    assert got == full
    got, full = _q_stored_max(25.0, q_headroom=2)
    assert got == full
