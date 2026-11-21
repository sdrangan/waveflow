"""The PRACH searcher's gates in Python: the golden, each stage alone in its harness, then the composite.

    python -m pytest tests -q

Every check compares bursts, word for word, and asserts their counts: a run that stopped early (a
deadlock looks like one) has fewer bursts and fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from waveflow.hw.harness import harness  # noqa: E402

from prach import golden as g  # noqa: E402
from prach import nr  # noqa: E402
from prach.prach import (W, Correlate, Detect, Extract, Front, PrachSearcher, Status,  # noqa: E402
                         StatusCode)
from prach.scenario import B4, KINDS, scenario, worked_cmd  # noqa: E402


def recorded(sysm, port) -> list[np.ndarray]:
    return [np.asarray(b, dtype=np.uint64).reshape(len(b), -1) for b in sysm.stand_ins[port].words]


def same(got: list[np.ndarray], want: list[np.ndarray], what: str) -> None:
    assert len(got) == len(want), f"{what}: {len(got)} bursts, expected {len(want)}"
    for k, (a, b) in enumerate(zip(got, want)):
        b = np.asarray(b, dtype=np.uint64).reshape(len(b), -1)
        assert a.shape == b.shape, f"{what}: burst {k} has {a.shape[0]} words, expected {b.shape[0]}"
        np.testing.assert_array_equal(a, b, err_msg=f"{what}: burst {k}")


# -- the golden ---------------------------------------------------------------------------------
def test_golden_worked_example():
    """Three UEs, each found as its own preamble, at its own delay (within 0.26 us, one TA step),
    and nothing else; the fixed-point model agrees with the float one on every detection."""
    c = worked_cmd()
    tr = g.search_fixed(c.wr, c.wi, c.params())
    found = {d.preamble: d for d in tr.dets}
    assert set(found) == {17, 40, 63}
    for pre, tau in ((17, 2.0e-6), (40, 0.3e-6), (63, 4.5e-6)):
        cv = B4.preamble(pre)[1]
        assert abs(g.delay_estimate_s(found[pre].n_peak, cv) - tau) < 0.26e-6
    w = c.wr + 1j * c.wi
    fl = {p: (n, m) for p, n, m in g.search_float(w, c.params())}
    assert set(fl) == set(found)
    for p, d in found.items():
        assert fl[p][0] == d.n_peak
        ratio = d.peak / (d.noise / d.n_noise)
        assert abs(ratio - fl[p][1]) / fl[p][1] < 0.01        # the fixed-point loss: under 1 %


def _bit_exact(cfg, ues, seed):
    from prach.thresholds import thr_q8
    rng = np.random.default_rng(seed)
    w = nr.windows(nr.occasion(cfg, ues, 4, rng), cfg)
    wr, wi = nr.quantize_adc(w)
    prm = g.Params(4, cfg.n_sym, cfg.k_start, cfg.n_cs, cfg.n_pre, g.roots_for(cfg.roots),
                   thr_q8(4, cfg.n_cs))
    return {d.preamble for d in g.search_fixed(wr, wi, prm).dets}


def test_threshold_table():
    """Decision 18: a threshold for every antenna count 1..4 and every N_CS of Table 6.3.3.1-7; the
    table agrees with the full-chain calibration at N_CS 23 (results/perf_float.json) within 2 %;
    an N_CS outside the table is refused."""
    import pytest
    from prach.thresholds import thr_q8
    for n_ant in (1, 2, 3, 4):
        for n_cs in nr.NCS_TABLE_139:
            assert 256 < thr_q8(n_ant, n_cs) < 1 << 16           # fits PrachCmd.thr_q8 (U16)
    assert abs(thr_q8(4, 23) - 1531) / 1531 < 0.02 and abs(thr_q8(2, 23) - 2465) / 2465 < 0.02
    with pytest.raises(ValueError):
        thr_q8(4, 24)


def test_near_far():
    """Decision 17: a UE 12 dB under another on the same root is found (the noise excludes the strong
    UE's peak), and nothing else is reported."""
    for seed in range(5):
        ues = [nr.Ue(13, 1.0e-6 + 0.5e-6 * seed, -4.0), nr.Ue(16, 3.0e-6 - 0.4e-6 * seed, -16.0)]
        assert _bit_exact(B4, ues, seed) == {13, 16}


def test_strong_ue_no_ghost():
    """Decision 17: a strong UE at zero delay and at the usable zone's end lights no other preamble
    (WIN_MARGIN 4 holds its early sidelobe, the relative rule its late one)."""
    for seed, (pre, dl) in enumerate(((7, 0.0), (8, 0.0), (20, B4.max_delay_s() - 1.0e-6),
                                      (45, B4.max_delay_s() - 1.0e-6))):
        assert _bit_exact(B4, [nr.Ue(pre, dl, 20.0)], 100 + seed) == {pre}


def test_window_table():
    """The windows of one root tile [0, 256) without overlap; each starts where C_v's zero delay is."""
    for n_cs in nr.NCS_TABLE_139[1:]:
        cover = np.zeros(g.N_CORR, int)
        for v in range(nr.shifts_per_root(n_cs)):
            n0, ln = g.window(v * n_cs, n_cs)
            cover[(n0 + np.arange(ln)) % g.N_CORR] += 1
        assert cover.max() == 1


def test_reference_is_the_root_dft():
    """R_u(k) (the ROM, the phase index) is the root's DFT up to one constant phase."""
    c, s = g.phase_rom()
    for u in (1, 2, 70, 138):
        p = g.ref_phase_index(u, pow(u, -1, nr.L_RA))
        ref = (c[p] + 1j * s[p]) / 2 ** 15
        y = np.fft.fft(nr.zc(u)) / np.sqrt(nr.L_RA)
        rot = y / ref
        assert np.allclose(np.abs(rot), 1, atol=1e-4) and np.ptp(np.angle(rot)) < 1e-3


def test_errors_are_fatal():
    for kind, code in (("test", StatusCode.BAD_PARAM), ("early", StatusCode.TLAST_EARLY),
                       ("badroot", StatusCode.BAD_ROOT), ("late", StatusCode.TLAST_LATE),
                       ("lateroots", StatusCode.TLAST_LATE), ("latecmd", StatusCode.TLAST_LATE)):
        sc = scenario(kind)
        _, status = sc.expected()
        codes = [Status().deserialize(s, word_bw=W).status for s in status]
        assert codes[-1] == code and all(c == StatusCode.OK for c in codes[:-1])
        assert len(status) == len(sc.cmds) - 1                    # the command after is never answered


# -- each stage alone ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_front_alone(kind):
    sysm = harness(Front, workload=kind)
    sysm.run()
    sc = scenario(kind)
    same(recorded(sysm, "m_cmd"), sc.link_cmd_words(), "front.m_cmd")
    same(recorded(sysm, "m_fft"), sc.link_fft1_in_words(), "front.m_fft")


@pytest.mark.parametrize("kind", KINDS)
def test_extract_alone(kind):
    sysm = harness(Extract, workload=kind)
    sysm.run()
    same(recorded(sysm, "m_y"), scenario(kind).link_y_words(), "extract.m_y")


@pytest.mark.parametrize("kind", KINDS)
def test_correlate_alone(kind):
    sysm = harness(Correlate, workload=kind)
    sysm.run()
    sc = scenario(kind)
    same(recorded(sysm, "m_cmd"), sc.link_cor_words(), "correlate.m_cmd")
    same(recorded(sysm, "m_fft"), sc.link_fft2_in_words(), "correlate.m_fft")


@pytest.mark.parametrize("kind", KINDS)
def test_detect_alone(kind):
    sysm = harness(Detect, workload=kind)
    sysm.run()
    out, status = scenario(kind).expected()
    same(recorded(sysm, "m_out"), out, "detect.m_out")
    same(recorded(sysm, "m_status"), status, "detect.m_status")


# -- code generation (no Vitis needed) ------------------------------------------------------------
def test_codegen(tmp_path):
    """The headers and the five tops generate; the composite's top instantiates both FFTs' tasks
    and the four hand-written bodies, every internal link at its stated depth."""
    import prach_build
    tops = prach_build.generate(tmp_path)
    assert tops == ["prach_searcher", "prach_front", "prach_extract", "prach_correlate", "prach_detect"]
    top = (tmp_path / "gen" / "prach_searcher.cpp").read_text()
    assert top.count("hls::task t") == 34               # 4 stages + 17 (L=1024) + 13 (L=256) FFT tasks
    for body in ("front", "extract", "correlate", "detect"):
        assert f"prach_{body}_task<128" in top
        assert (tmp_path / "include" / f"prach_{body}_task.h").exists()
    for name, depth in (("cmd", 16), ("win", 4), ("bins", 4), ("y", 32), ("cor", 4), ("zc", 4), ("pdp", 4)):
        assert f"variable={name} depth={depth}" in top
    assert (tmp_path / "include" / "prach_rom.h").exists()


# -- the composite --------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_composite(kind):
    sysm = harness(PrachSearcher, workload=kind)
    sysm.run()
    out, status = scenario(kind).expected()
    same(recorded(sysm, "m_out"), out, "m_out")
    same(recorded(sysm, "m_status"), status, "m_status")
