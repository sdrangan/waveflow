"""S5 gates -- the alpha/beta gemv overload against the real Vitis BLAS kernel.

``yr = alpha * (M x) + beta * y`` (``gemv.hpp:67-85``) is a composition of three shipped kernels:
the 5-arg ``gemv`` that S1-S2 already model, then ``scal`` (``beta * y``), then ``axpy``
(``alpha * dot + that``).  The new arithmetic is one line, and it is a fused-multiply-add
candidate -- which is what most of this file is about.

Goldens in ``golden/gemv_ab_*.txt`` come from ``cpp/dump_gemv_ab.cpp``, built with
``-ffp-contract=off``.  Checked in; regenerate with ``tools/regen_golden.sh``.
"""
from __future__ import annotations

import pathlib
import struct

import numpy as np
import pytest

from MatrixVectorMul_bitexact.wf_gemv.gemv import f32_bits, gemv, gemv_ab

ROOT = pathlib.Path(__file__).resolve().parents[1]
CASES = ["M4_N64", "M7_N128"]


def _bits_f32(pattern: int) -> np.float32:
    return np.float32(struct.unpack("<f", struct.pack("<I", int(pattern)))[0])


def _read_input(name: str):
    lines = [ln for ln in (ROOT / "data" / f"input_ab_{name}.txt").read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    n_case, m, n, n_ab = (int(v) for v in lines[0].split())
    vals = [int(v) for v in lines[1:]]
    pos, cases = 0, []
    for _ in range(n_case):
        a = np.array([_bits_f32(v) for v in vals[pos:pos + m * n]],
                     dtype=np.float32).reshape(m, n); pos += m * n
        x = np.array([_bits_f32(v) for v in vals[pos:pos + n]], dtype=np.float32); pos += n
        y = np.array([_bits_f32(v) for v in vals[pos:pos + m]], dtype=np.float32); pos += m
        cases.append((a, x, y))
    ab = []
    for _ in range(n_ab):
        ab.append((_bits_f32(vals[pos]), _bits_f32(vals[pos + 1]))); pos += 2
    assert pos == len(vals), f"input file has {len(vals) - pos} trailing values"
    return m, n, cases, ab


def _read_golden(name: str):
    lines = [ln for ln in (ROOT / "golden" / f"gemv_ab_{name}.txt").read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    return [tuple(int(v) for v in ln.split()) for ln in lines[1:]]


def test_model_is_bit_exact() -> None:
    """Every (alpha, beta), every stream width, every row -- the exact IEEE-754 pattern."""
    total, bad = 0, []
    for name in CASES:
        _m, _n, cases, ab = _read_input(name)
        for t, logp, k, r, want in _read_golden(name):
            alpha, beta = ab[t]
            a, x, y = cases[k]
            got = f32_bits(gemv_ab(a, x, y, alpha, beta, par_entries=1 << logp)[r])
            total += 1
            if got != want:
                bad.append(f"{name} alpha={alpha} beta={beta} logP={logp} case={k} row={r}: "
                           f"0x{got:08x} != 0x{want:08x}")
    assert total, "no golden rows"
    assert not bad, f"{len(bad)}/{total} rows differ:\n" + "\n".join(bad[:12])


# --- the gates --------------------------------------------------------------------------------

def test_an_fma_model_gives_different_bits() -> None:
    """⚠️ The headline S5 risk, and the reason the golden pins ``-ffp-contract=off``.

    ``axpy`` writes ``p_alpha * l_realX + l_realY`` as one expression.  A compiler is free to
    contract that into a fused multiply-add, which keeps the product's full precision and rounds
    once instead of twice.  Building the *same* dumper with ``-O3 -march=native`` (or
    ``-mfma -ffp-contract=fast``) changes 25 of 288 rows on ``M4_N64``.

    So "bit-exact" here is conditional on the multiply and the add being separately rounded.  This
    test reproduces the contracted reading in float64 -- exact for a float32 product -- and
    asserts it disagrees, so the distinction cannot quietly stop mattering.
    """
    differ = total = 0
    for name in CASES:
        _m, _n, cases, ab = _read_input(name)
        for t, logp, k, r, want in _read_golden(name):
            alpha, beta = ab[t]
            a, x, y = cases[k]
            dot_r = gemv(a, x, par_entries=1 << logp)[r]
            scaled = np.float32(np.float32(beta) * y[r])
            fused = np.float32(np.float64(alpha) * np.float64(dot_r) + np.float64(scaled))
            total += 1
            differ += f32_bits(fused) != want
    assert differ > 0, (
        "the fused and unfused readings agree on every row -- this data no longer distinguishes "
        "them, and the -ffp-contract=off pin in tools/regen_golden.sh has become unfalsifiable")


def test_rounding_beta_times_y_before_the_add_matters() -> None:
    """``scal`` rounds ``beta * y`` to float32 before ``axpy`` sees it.

    A model that evaluates ``alpha*dot + beta*y`` in one wider expression and rounds once is the
    natural thing to write, and it is wrong.
    """
    differ = total = 0
    for name in CASES:
        _m, _n, cases, ab = _read_input(name)
        for t, logp, k, r, want in _read_golden(name):
            alpha, beta = ab[t]
            a, x, y = cases[k]
            dot_r = np.float64(gemv(a, x, par_entries=1 << logp)[r])
            once = np.float32(np.float64(alpha) * dot_r + np.float64(beta) * np.float64(y[r]))
            total += 1
            differ += f32_bits(once) != want
    assert differ > 0, "rounding once at the end matched every row -- the gate proves nothing"


def test_alpha_one_beta_zero_reproduces_the_plain_gemv() -> None:
    """A cross-check on the composition itself, against the S1-S2 goldens' own model.

    If the wiring of scal/axpy were wrong, this is the case that would still pass by accident --
    so it is a consistency check, not a gate.  The gates are the two above.
    """
    for name in CASES:
        _m, _n, cases, ab = _read_input(name)
        idx = [i for i, (al, be) in enumerate(ab) if al == 1.0 and be == 0.0]
        assert idx, "no (alpha=1, beta=0) entry in the sweep"
        for t, logp, k, r, want in _read_golden(name):
            if t != idx[0]:
                continue
            a, x, _y = cases[k]
            assert f32_bits(gemv(a, x, par_entries=1 << logp)[r]) == want


def test_alpha_zero_beta_one_returns_y_unchanged() -> None:
    """``0 * dot + 1 * y`` must be exactly ``y`` -- pins that ``scal`` does not perturb it."""
    for name in CASES:
        _m, _n, cases, ab = _read_input(name)
        idx = [i for i, (al, be) in enumerate(ab) if al == 0.0 and be == 1.0]
        assert idx, "no (alpha=0, beta=1) entry in the sweep"
        for t, _logp, k, r, want in _read_golden(name):
            if t == idx[0]:
                assert f32_bits(cases[k][2][r]) == want


@pytest.mark.parametrize("name", CASES)
def test_par_entries_still_changes_the_bits(name: str) -> None:
    """The float path's defining property must survive the alpha/beta wrapper.

    The inner reduction is unchanged, so ``logParEntries`` still sets the tree shape.  If this
    stopped holding, the wrapper would be flattening the very thing S1 established.
    """
    by_key: dict[tuple, set[int]] = {}
    for t, logp, k, r, want in _read_golden(name):
        by_key.setdefault((t, k, r), set()).add(want)
    varying = sum(1 for vals in by_key.values() if len(vals) > 1)
    assert varying > 0, "no result changed with logParEntries across the whole golden"
