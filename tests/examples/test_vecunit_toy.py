"""The toy kernels (``examples/vecunit``): each program passes the assembler and equals its NumPy golden
on the interpreter, bit for bit, in both pass orders; the toy unit in pysim equals the interpreter."""

from __future__ import annotations

import numpy as np
import pytest

from examples.vecunit import toy as T
from waveflow.vecunit.asm import check


def same(got, want) -> bool:
    return np.array_equal(got[0], want[0]) and np.array_equal(got[1], want[1])


@pytest.mark.parametrize("mode", ["pass", "serial"])
@pytest.mark.parametrize("k, n", [(16, 4), (5, 1), (9, 3)])
def test_double(k, n, mode):
    cfg = T.toy_config()
    xr, xi = T.random_matrix(k, n, seed=k + n, scale=3.0)
    p = T.double_programs(k, n)
    assert check(cfg, p) == []
    zr, zi, r = T.run(cfg, p, [(xr, xi)], mode=mode)
    assert r.status == 0 and same((zr, zi), T.double_golden(xr, xi))
    assert (np.abs(zr) == (1 << 15) - 1).any()  # 2x saturates somewhere at scale 3


@pytest.mark.parametrize("mode", ["pass", "serial"])
@pytest.mark.parametrize("nrows, n", [(16, 4), (40, 3), (7, 1), (64, 4)])
def test_cmul(nrows, n, mode):
    cfg = T.toy_config()
    xr, xi = T.random_matrix(nrows, n, seed=nrows, scale=2.0)
    yr, yi = T.random_matrix(nrows, n, seed=nrows + 1, scale=2.0)
    p = T.cmul_programs(cfg, nrows, n)
    assert check(cfg, p) == []
    assert len(p.store) == len(T.chunks(cfg, nrows)) == -(-nrows // cfg.VLMAX)
    zr, zi, r = T.run(cfg, T.cmul_programs(cfg, nrows, n), T.cmul_inputs(cfg, xr, xi, yr, yi),
                      mode=mode)
    assert r.status == 0 and same((zr, zi), T.cmul_golden(xr, xi, yr, yi))
    assert [o[0]["tag"] for o in r.outputs[T.PORT]] == list(range(len(p.store)))


@pytest.mark.parametrize("mode", ["pass", "serial"])
@pytest.mark.parametrize("k, n", [(16, 4), (3, 2)])
def test_gain(k, n, mode):
    cfg = T.toy_config()
    xr, xi = T.random_matrix(k, n, seed=k)
    yr, yi = T.random_matrix(k, n, seed=k + 1, scale=0.5)
    yr, yi = yr + xr // 2, yi + xi // 2
    p = T.gain_programs(k, n)
    assert check(cfg, p) == []
    zr, zi, r = T.run(cfg, p, [(xr, xi), (yr, yi)], mode=mode)
    assert r.status == 0 and same((zr, zi), T.gain_golden(xr, xi, yr, yi))
    # what it means: the residual is orthogonal to x (in Re(x^H r)) to within the rounding
    x = (xr + 1j * xi) / 2 ** T.FV.frac_bits
    res = (zr + 1j * zi) / 2 ** T.FV.frac_bits
    assert np.all(np.abs(np.sum(np.real(np.conj(x) * res), axis=0)) < 1e-2)


def test_gain_guards_a_zero_x():
    cfg = T.toy_config()
    z = np.zeros((8, 4), np.int64)
    yr, yi = T.random_matrix(8, 4, seed=3)
    zr, zi, r = T.run(cfg, T.gain_programs(8, 4), [(z, z), (yr, yi)])
    assert r.status == 0 and np.array_equal(zr, yr) and np.array_equal(zi, yi)  # a = 0


def test_a_fourth_slot_is_refused():
    """``vlse``, ``vlse``, ``vmul``, ``vsse`` chained is four slots: the assembler refuses it."""
    from waveflow.vecunit import isa as I
    from waveflow.vecunit.asm import Programs

    cfg = T.toy_config()
    p = Programs(load=[I.ld(T.Q_X, 0, 4, 4), I.ld(T.Q_Y, 0, 4, 4)],
                 execute=[I.setvl(4), I.vlse(0, T.Q_X, 0, 1).c, I.vlse(1, T.Q_Y, 0, 1).c,
                          I.vmul_vv(2, 0, 1, conj=True).c, I.vsse(T.Q_Z, 2, 0, 1),
                          I.qpop(T.Q_X), I.qpop(T.Q_Y), I.qpush(T.Q_Z)],
                 store=[I.st(0, T.Q_Z, 0, 4, 4)])
    assert any("runs past S = 3 instructions" in s for s in check(cfg, p))


@pytest.mark.parametrize("word_bits", [64, 128])
@pytest.mark.parametrize("kind", ["test", "stream"])
def test_pysim_unit_equals_the_interpreter(kind, word_bits, tmp_path):
    from examples.vecunit.harness import unit
    from waveflow.hw.harness import harness

    u = unit(word_bits=word_bits)
    sysm = harness(u, workload=kind, n_cycles=10**6)
    sysm.write_scenario(tmp_path)
    sysm.run()
    gold = u.golden(kind)
    assert len(gold["m_status"]) == 4
    for port, bursts in gold.items():
        got = sysm.stand_ins[port].words
        assert len(got) == len(bursts)
        assert all(np.array_equal(np.asarray(a, np.uint64).reshape(-1),
                                  np.asarray(b, np.uint64).reshape(-1))
                   for a, b in zip(got, bursts))
