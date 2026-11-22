"""``SsrFft(direction="runtime")``: the direction chosen per frame (``plans/shared_kernel.md`` Stage 3).

The toolchain-free half; the RTL gate is ``test_xsi.py::test_runtime_*`` (``-m xsi``).

* the identity the hardware uses: the inverse tables are the forward ones conjugated, so an inverse
  stage is the forward stage with butterfly outputs 1 and 3 swapped and the twiddle conjugated --
  computed here exactly as ``ssr_butterfly_dir`` does, and equal to the fixed inverse stage;
* conjugating the frame at both ends instead would NOT be bit-exact (why the hardware does not);
* the composite's pysim, both reorders and both pysim modes: every frame is the fixed build's of
  its direction, bit for bit, at the fixed build's interval;
* its structure: the ``s_dir`` port, a direction FIFO per stage, its own configuration key, and the
  option surviving the harness's fresh copy (``build_fields``).
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.dsp.ssr_fft import hls
from waveflow.dsp.ssr_fft import model as m
from waveflow.dsp.ssr_fft.hw import SsrFft
from waveflow.dsp.ssr_fft.testbench import golden, pysim_frame_cycles, pysim_output, run_pysim
from waveflow.hw.hw_module import build_params
from waveflow.simulation.simulation import Simulation
from waveflow.utils import fixputils as fp
from waveflow.vitis_l1.cxquant import complex_multiply
from waveflow.vitis_l1.fft import _dft4, _exp_table, exp_table_format, twiddle_stored

DIRS = [0, 1, 1, 0, 1]


@pytest.mark.parametrize("tw", [(18, 2), (16, 2), (24, 2), (12, 2)])
@pytest.mark.parametrize("length", (16, 64, 256, 1024, 4096))
def test_inverse_tables_are_the_forward_ones_conjugated(length, tw):
    fr, fi = twiddle_stored(length, *tw, False)
    ir, ii = twiddle_stored(length, *tw, True)
    assert np.array_equal(fr, ir) and np.array_equal(np.asarray(fi), -np.asarray(ii))
    _, er, ei = _exp_table(*tw, False)
    _, xr, xi = _exp_table(*tw, True)
    assert er == xr and tuple(-v for v in ei) == xi


def _stage_as_hardware(geo: m.Geometry, s: int, re, im, inverse: bool):
    """Stage ``s`` as ``ssr_butterfly_dir`` computes it: the FORWARD tables; for an inverse frame,
    outputs 1 and 3 swapped and the twiddle's imaginary part negated."""
    fwd = geo.direction(False)
    f_in, _ = fwd.stage_fmts[s]
    orr, oii, g = _dft4(re.T, im.T, f_in, s == 0, fwd.mode, fwd.tw_w, fwd.tw_i, False)
    if inverse:
        orr, oii = orr[[0, 3, 2, 1]], oii[[0, 3, 2, 1]]
    if s < fwd.S - 1:
        f_next = fwd.stage_fmts[s + 1][0]
        op1 = g
        if (g.W, g.int_bits) != (f_next.W, f_next.int_bits):
            orr, oii = fp.quantize(orr, g, f_next), fp.quantize(oii, g, f_next)
            op1 = f_next
        tw_r, tw_i = (np.asarray(t) for t in fwd.twiddles)
        idx = fwd.twiddle_index(s).T
        ti = -tw_i[idx] if inverse else tw_i[idx]
        orr, oii = complex_multiply(orr, oii, op1, tw_r[idx], ti,
                                    exp_table_format(fwd.tw_w, fwd.tw_i), f_next)
    else:
        fout = fwd.raw_out_fmt
        if (g.W, g.int_bits) != (fout.W, fout.int_bits):
            orr, oii = fp.quantize(orr, g, fout), fp.quantize(oii, g, fout)
    return np.asarray(orr, dtype=np.int64).T, np.asarray(oii, dtype=np.int64).T


def _frame(length: int, seed: int = 3):
    rng = np.random.default_rng(seed)
    lo, hi = -(1 << 15), 1 << 15
    re = rng.integers(lo, hi, size=(length // 4, 4))
    im = rng.integers(lo, hi, size=(length // 4, 4))
    re[0, :2] = lo                  # the most negative value, where a negation overflows
    im[1, :2] = lo
    return re, im


@pytest.mark.parametrize("inverse", (False, True))
@pytest.mark.parametrize("length", (16, 64, 256, 1024))
def test_the_hardware_identity_is_the_fixed_stage(length, inverse):
    geo = m.Geometry(length, runtime=True)
    ref = geo.direction(inverse)
    re, im = _frame(length)
    a_re, a_im = m.transpose_in(ref, re), m.transpose_in(ref, im)
    b_re, b_im = a_re, a_im
    for s in range(geo.S):
        a_re, a_im = m.stage(ref, s, a_re, a_im)
        b_re, b_im = _stage_as_hardware(geo, s, b_re, b_im, inverse)
        assert np.array_equal(a_re, b_re) and np.array_equal(a_im, b_im), f"stage {s}"
        if s < geo.S - 1:
            d = geo.stage_d(s)
            a_re, a_im = m.commute(a_re, d), m.commute(a_im, d)
            b_re, b_im = a_re, a_im


def test_conjugating_at_the_ends_is_not_bit_exact():
    """``conj(FFT(conj(x)))`` is the inverse in exact arithmetic, not in these bits: truncation does
    not commute with negation (and ``-(-2^15)`` does not fit the input).  Hence the swap."""
    geo = m.Geometry(64)
    re, im = _frame(64)
    want = m.pipeline(geo.direction(True), re, im)
    neg = np.where(im == -(1 << 15), (1 << 15) - 1, -im)          # saturate the one that overflows
    f_re, f_im = m.pipeline(geo, re, neg)
    assert not (np.array_equal(f_re, want[0]) and np.array_equal(-f_im, want[1]))


@pytest.mark.parametrize("reorder,pysim,interval", (("pingpong", "tasks", 16),
                                                     ("pingpong", "flat", 16),
                                                     ("sob", "tasks", 21)))
@pytest.mark.parametrize("length", (16, 64))
def test_runtime_pysim_is_each_directions_fixed_build(tmp_path, length, reorder, pysim, interval):
    n = 8
    tb = run_pysim(tmp_path, length=length, n_frames=n, reorder=reorder, pysim=pysim,
                   direction="runtime", dirs=DIRS)
    out = pysim_output(tb)
    assert len(out) == n
    for k, (g, w) in enumerate(zip(out, golden(n, length, dirs=DIRS))):
        assert np.array_equal(g[0], w[0]) and np.array_equal(g[1], w[1]), f"frame {k}"
    if length == 64:
        assert np.allclose(np.diff(pysim_frame_cycles(tb)), interval)


def test_runtime_frames_equal_the_fixed_builds_pysim(tmp_path):
    """Frame for frame, against the two fixed builds run on the same input."""
    n = 6
    rt = pysim_output(run_pysim(tmp_path / "rt", length=64, n_frames=n, reorder="pingpong",
                                direction="runtime", dirs=DIRS))
    fwd = pysim_output(run_pysim(tmp_path / "f", length=64, n_frames=n, reorder="pingpong"))
    inv = pysim_output(run_pysim(tmp_path / "i", length=64, n_frames=n, reorder="pingpong",
                                 inverse=True))
    for k in range(n):
        ref = inv[k] if DIRS[k % len(DIRS)] else fwd[k]
        assert np.array_equal(rt[k][0], ref[0]) and np.array_equal(rt[k][1], ref[1]), f"frame {k}"


def test_runtime_structure():
    fft = SsrFft(name="f", sim=Simulation(), L=64, reorder="pingpong", direction="runtime")
    assert [name for name, _ in fft.boundary] == ["s_in", "s_dir", "m_out"]
    assert hls.config_key(fft.geo) == hls.config_key(m.Geometry(64)) + "_rt"
    assert fft.out_fmt == m.Geometry(64).out_fmt
    assert fft.inv_out_fmt == m.Geometry(64, inverse=True).out_fmt
    kinds = [t.task.kind for t in fft.tasks]
    assert kinds[0] == "dir_in"
    dir_ifs = [i for n, i in fft.interfaces.items() if "_dir" in n]
    assert len(dir_ifs) == fft.geo.S
    assert all(i.depth >= 3 for i in dir_ifs)
    # The fresh copy the harness and the system top make keeps the direction.
    assert build_params(fft)["direction"] == "runtime"
    src = hls.render_wrappers(fft.geo, reorder="pingpong")
    assert "ssr_fft_dir_in_task" in src and src.count("ssr_fft_stage_dir_task") == fft.geo.S
    # The fixed build is unchanged: no direction tasks.
    fixed = hls.render_wrappers(m.Geometry(64), reorder="pingpong")
    assert "dir" not in fixed


@pytest.mark.parametrize("kw", ({"inverse": True}, {"lanes": True}, {"direction": "sideways"}))
def test_runtime_refuses_what_it_does_not_do(kw):
    with pytest.raises(ValueError):
        SsrFft(name="f", sim=Simulation(), L=64, **({"direction": "runtime"} | kw))
