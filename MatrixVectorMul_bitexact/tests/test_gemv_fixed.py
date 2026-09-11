"""S4 gates -- the ap_fixed gemv model against the real Vitis BLAS kernel.

Two goldens per input, both produced by ``cpp/dump_gemv_fixed.cpp`` from the library's own code:

``*_shipped.txt``   the untouched library -- and it is **wrong**, see the defect note below
``*_patched.txt``   the same source built against ``cpp/vendor_patched/dotHelper_patched.hpp``,
                    a copy of one vendor header differing by one line

Checked in, so these tests need neither Vitis nor a compiler.  Regenerate with
``tools/regen_golden.sh``.
"""
from __future__ import annotations

import pathlib

import numpy as np
import pytest

from MatrixVectorMul_bitexact.wf_gemv.fixed import (
    OMode,
    QMode,
    as_shipped,
    fixed_format,
    gemv_fixed,
    gemv_fixed_as_shipped,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
CASES = ["W16_M3_N32", "W24_M4_N16"]
MODES = {
    "trn_wrap": (QMode.AP_TRN, OMode.AP_WRAP),
    "rnd_wrap": (QMode.AP_RND, OMode.AP_WRAP),
    "trn_sat": (QMode.AP_TRN, OMode.AP_SAT),
    "rnd_sat": (QMode.AP_RND, OMode.AP_SAT),
}


def _read_input(name: str):
    lines = [ln for ln in (ROOT / "data" / f"input_fixed_{name}.txt").read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    w, i, n_case, m, n = (int(v) for v in lines[0].split())
    body, cases = lines[1:], []
    for k in range(n_case):
        block = body[k * (m + 1):(k + 1) * (m + 1)]
        mat = np.array([[int(v) for v in row.split()] for row in block[:m]], dtype=np.int64)
        vec = np.array([int(v) for v in block[m].split()], dtype=np.int64)
        assert mat.shape == (m, n) and vec.shape == (n,)
        cases.append((mat, vec))
    return w, i, cases


def _read_golden(path: pathlib.Path):
    lines = [ln for ln in path.read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    rows = []
    for ln in lines[1:]:
        cfg, logp, k, r, y = ln.split()
        rows.append((cfg, int(logp), int(k), int(r), int(y, 16)))
    return rows


def _signed(bits: int, w: int) -> int:
    return bits - (1 << w) if bits >> (w - 1) else bits


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("build", ["shipped", "patched"])
def test_model_is_bit_exact(name: str, build: str) -> None:
    """Every row, every (Q, O), every stream width -- the exact stored field the kernel wrote."""
    w, i, cases = _read_input(name)
    rows = _read_golden(ROOT / "golden" / f"gemv_fixed_{name}_{build}.txt")
    assert rows, "empty golden"
    model = gemv_fixed_as_shipped if build == "shipped" else gemv_fixed
    bad = []
    for cfg, logp, k, r, y_bits in rows:
        fmt = fixed_format(w, i, *MODES[cfg])
        mat, vec = cases[k]
        got = int(model(mat, vec, fmt)[r]) & ((1 << w) - 1)
        if got != y_bits:
            bad.append(f"{cfg} logP={logp} case={k} row={r}: "
                       f"model 0x{got:x} != golden 0x{y_bits:x}")
    assert not bad, f"{len(bad)}/{len(rows)} rows differ:\n" + "\n".join(bad[:12])


# --- the gates: each property below must be able to fail --------------------------------------

@pytest.mark.parametrize("name", CASES)
def test_the_shipped_kernel_is_wrong(name: str) -> None:
    """The defect is real and this data exercises it.

    If AMD fixes ``dotHelper.hpp:98``, a regenerated ``_shipped`` golden equals the ``_patched``
    one and this fails -- which is the point.  It is the signal to retire ``as_shipped``.
    """
    shipped = _read_golden(ROOT / "golden" / f"gemv_fixed_{name}_shipped.txt")
    patched = _read_golden(ROOT / "golden" / f"gemv_fixed_{name}_patched.txt")
    assert [r[:4] for r in shipped] == [r[:4] for r in patched], "goldens are not row-aligned"
    differ = sum(1 for a, b in zip(shipped, patched) if a[4] != b[4])
    assert differ > 0.5 * len(shipped), (
        f"only {differ}/{len(shipped)} rows differ between the shipped and corrected kernel; "
        f"either the data has no fractional content or the library has been fixed")


def test_the_defect_is_a_truncating_value_conversion() -> None:
    """Pin the *shape* of the defect, not just that it exists.

    ``ap_fixed -> ap_uint<W>`` truncates toward zero (not floor), then wraps into W bits.  The
    negative cases are the ones that separate the two, and they were measured against the
    library rather than reasoned about.
    """
    fmt = fixed_format(16, 8)                    # F = 8
    assert as_shipped(round(27.75 * 256), fmt) == 27
    assert as_shipped(round(-2.75 * 256), fmt) == -2      # toward zero; floor would be -3
    assert as_shipped(round(-0.75 * 256), fmt) == 0       # floor would be -1
    assert as_shipped(round(0.75 * 256), fmt) == 0


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("build", ["shipped", "patched"])
def test_par_entries_does_not_change_the_result(name: str, build: str) -> None:
    """Measured, not assumed -- and it is the opposite of the float path.

    ``dot_dsp`` accumulates in index order at any stream width, so unlike ``dot_tree`` (where
    ``logParEntries`` sets the tree shape and the five widths give up to five distinct answers) it is
    not part of the numerical contract here.
    """
    rows = _read_golden(ROOT / "golden" / f"gemv_fixed_{name}_{build}.txt")
    by_key: dict[tuple, set[int]] = {}
    for cfg, logp, k, r, y in rows:
        by_key.setdefault((cfg, k, r), set()).add(y)
    widths = {logp for _, logp, _, _, _ in rows}
    assert len(widths) > 1, "only one stream width in the golden -- nothing to compare"
    varying = {key for key, vals in by_key.items() if len(vals) > 1}
    assert not varying, f"{len(varying)} results changed with logParEntries: {sorted(varying)[:5]}"


@pytest.mark.parametrize("name", CASES)
def test_rounding_and_overflow_modes_both_bite(name: str) -> None:
    """The Q and O sweeps carry information.

    Both are applied once **per element** (the accumulator is the element format), so a suite in
    which every mode agrees would be blessing a model that ignored them entirely.  Overflow must
    also not be universal, or the saturating configs would say only "clipped".
    """
    rows = _read_golden(ROOT / "golden" / f"gemv_fixed_{name}_patched.txt")
    vals = {(cfg, logp, k, r): y for cfg, logp, k, r, y in rows}
    keys = {(logp, k, r) for _, logp, k, r, _ in rows}
    q_diff = sum(1 for key in keys if vals[("trn_wrap", *key)] != vals[("rnd_wrap", *key)])
    o_diff = sum(1 for key in keys if vals[("trn_wrap", *key)] != vals[("trn_sat", *key)])
    assert q_diff > 0, "AP_TRN and AP_RND agree everywhere -- the Q sweep proves nothing"
    assert 0 < o_diff < len(keys), (
        f"AP_WRAP vs AP_SAT differ on {o_diff}/{len(keys)} rows; the data must overflow the "
        f"accumulator on some rows and not others")


def test_the_model_refuses_widths_it_cannot_carry() -> None:
    """``fixputils`` is int64-backed and the exact ``acc + product`` intermediate is 2W+1 bits.

    Fail at construction, not at the first quietly wrapped answer.
    """
    fixed_format(31, 16)
    with pytest.raises(NotImplementedError, match="ceiling"):
        fixed_format(32, 16)


@pytest.mark.parametrize("name", CASES)
def test_quantizing_once_at_the_end_is_wrong(name: str) -> None:
    """The obvious model -- accumulate exactly, round once -- fails, and by a lot.

    It is what anyone would write from the docs, and it is what a DSP-style MAC with a wide
    accumulator would do.  ``dot_dsp`` cannot do that, because S3 showed ``t_MacDataType`` will
    not compile as anything but the element type: the narrowing happens on *every* ``+=``.  This
    is the S4 analogue of the float path's sequential-vs-tree gate.
    """
    from waveflow.utils import fixputils as fx

    w, i, cases = _read_input(name)
    rows = _read_golden(ROOT / "golden" / f"gemv_fixed_{name}_patched.txt")
    wrong = 0
    for cfg, _logp, k, r, y_bits in rows:
        fmt = fixed_format(w, i, *MODES[cfg])
        mat, vec = cases[k]
        prod, prod_fmt = fx.mult(mat[r], fmt, vec, fmt)
        total, total_fmt = fx.fixed_sum(prod, prod_fmt)
        got = int(fx.quantize(total, total_fmt, fmt)) & ((1 << w) - 1)
        wrong += got != y_bits
    assert wrong > 0.5 * len(rows), (
        f"quantize-once-at-the-end matched on {len(rows) - wrong}/{len(rows)} rows; the data no "
        f"longer distinguishes per-element narrowing from a wide accumulator")


@pytest.mark.parametrize("name", CASES)
def test_ignoring_the_rounding_mode_is_wrong(name: str) -> None:
    """``AP_RND`` is not decoration: forcing ``AP_TRN`` everywhere misses every ``AP_RND`` row."""
    w, i, cases = _read_input(name)
    rows = _read_golden(ROOT / "golden" / f"gemv_fixed_{name}_patched.txt")
    rnd_rows = [r for r in rows if MODES[r[0]][0] is QMode.AP_RND]
    assert rnd_rows, "no AP_RND configs in the golden"
    wrong = 0
    for cfg, _logp, k, r, y_bits in rnd_rows:
        forced = fixed_format(w, i, QMode.AP_TRN, MODES[cfg][1])
        mat, vec = cases[k]
        wrong += (int(gemv_fixed(mat, vec, forced)[r]) & ((1 << w) - 1)) != y_bits
    assert wrong > 0.5 * len(rnd_rows), (
        f"AP_TRN reproduced {len(rnd_rows) - wrong}/{len(rnd_rows)} AP_RND rows -- the data has "
        f"too few values sitting on a rounding boundary")
