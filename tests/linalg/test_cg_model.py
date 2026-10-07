"""Step 8.1b: the bit-exact model of fixed-point CG (``waveflow.linalg.cg``).

Two checks: the golden vectors frozen from the example's ``cg_init`` and ``vec_step`` at
``6a2cdca`` before the model moved (``data/cg_golden.npz``, written by
``examples/mimo_cg/tools/freeze_cg_golden.py``), and the reference solve: it reproduces the frozen
explicit-residual runs iteration by iteration, and with wide registers it converges to the
floating-point solution.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from waveflow.linalg import cg
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format, OMode, QMode

DATA = Path(__file__).parent / "data" / "cg_golden.npz"
REGS = ("A", "B", "P", "R", "S", "X", "ps", "rz", "alpha", "beta")
STATE = ("xr", "xi", "rr", "ri", "pr", "pi", "rz")


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


@pytest.fixture(scope="module")
def golden():
    with np.load(DATA, allow_pickle=False) as z:
        arrays = {k: z[k].astype(np.int64) for k in z.files if k != "meta"}
        meta = json.loads(str(z["meta"]))
    return meta, arrays


def _formats(spec: dict) -> cg.CgFormats:
    def fmt(s):
        W, I, signed, q, o = s
        return Format(W, I, bool(signed), QMode(q), OMode(o))

    return cg.CgFormats(**{r: fmt(spec[r]) for r in REGS}, g_div=spec["g_div"])


def _state(arrays: dict, prefix: str, j: int) -> cg.CgState:
    x, r, p = (arrays[f"{prefix}_{k}"][j] for k in "xrp")
    return cg.CgState(x[0], x[1], r[0], r[1], p[0], p[1], arrays[f"{prefix}_rz"][j])


def _same(a: cg.CgState, b: cg.CgState) -> bool:
    return all(np.array_equal(getattr(a, k), getattr(b, k)) for k in STATE)


# --- golden vectors ------------------------------------------------------------------------------


def test_golden_provenance(golden):
    meta, _ = golden
    assert meta["source_commit"].startswith("6a2cdca") and not meta["skipped"]
    assert len(meta["sets"]) == 17 and len(meta["cases"]) == 228


def test_model_replays_every_golden_case(golden):
    meta, arrays = golden
    n = 0
    for group, names in meta["group_sets"].items():
        kind = group.split("_", 1)[1]
        for j, name in enumerate(names):
            f = _formats(meta["sets"][name])
            if kind == "init":
                b = arrays[f"{group}_b"][j]
                got = cg.cg_init(b[0], b[1], f)
                assert _same(got, _state(arrays, f"{group}_out", j)), (group, name)
                n += 1
                continue
            hook = None
            if kind in ("x1", "xK"):
                a, b = arrays[f"{group}_a"][j], arrays[f"{group}_b"][j]
                hook = cg.residual_hook(a[0], a[1], b[0], b[1], f)
            s = arrays[f"{group}_s"][j]
            got, scalars = cg.vec_step(
                _state(arrays, f"{group}_in", j), s[0], s[1], f, residual=hook
            )
            assert _same(got, _state(arrays, f"{group}_out", j)), (group, name)
            for k in ("ps", "alpha", "beta"):
                assert np.array_equal(scalars[k], arrays[f"{group}_{k}"][j]), (group, k)
            n += 1
    assert n == len(meta["cases"])


# --- the reference solve -------------------------------------------------------------------------


def test_solve_reproduces_the_frozen_explicit_runs(golden):
    """``cg_solve`` from the frozen ``A`` and ``B``: its first and last iterations are the frozen
    ones (both the ``vec_step`` input and output)."""
    meta, arrays = golden
    for K in (4, 8, 16):
        names = meta["group_sets"][f"k{K}_x1"]
        assert names == meta["group_sets"][f"k{K}_xK"]
        for j, name in enumerate(names):
            f = _formats(meta["sets"][name])
            a, b = arrays[f"k{K}_x1_a"][j], arrays[f"k{K}_x1_b"][j]
            seen = {}
            cg.cg_solve(
                a[0], a[1], b[0], b[1], K, f, explicit=True,
                on_iteration=lambda n, st, _sc, seen=seen: seen.setdefault(n, st),
            )  # fmt: skip
            assert _same(seen[1], _state(arrays, f"k{K}_x1_out", j)), (K, name)
            assert _same(seen[K - 1], _state(arrays, f"k{K}_xK_in", j)), (K, name)
            assert _same(seen[K], _state(arrays, f"k{K}_xK_out", j)), (K, name)


#: The bound on the largest absolute error against ``numpy.linalg.solve``.  The test's system
#: measured 1.0e-4 (a first guess of 1e-5 was too tight); the bound rests on the wide registers'
#: documented accuracy against floating-point CG (a different metric: up to 7.4e-4 at M/K = 2, the
#: example's M2 review), and 1e-3 still separates a solve from a non-solve (errors of order 0.1).
WIDE_TOL = 1e-3


def wide() -> cg.CgFormats:
    return cg.CgFormats(
        A=reg(28, 3), B=reg(28, 4), P=reg(28, 4), R=reg(28, 4), S=reg(29, 5),
        X=reg(28, 3), ps=reg(34, 10), rz=reg(33, 9), alpha=reg(29, 5), beta=reg(27, 3),
    )  # fmt: skip


def test_solve_converges_with_wide_registers():
    """24 fraction bits everywhere: K iterations solve ``A X = B`` (:data:`WIDE_TOL`)."""
    f = wide()
    rng = np.random.default_rng(8)
    M, K, N = 64, 8, 4
    H = (rng.standard_normal((M, K)) + 1j * rng.standard_normal((M, K))) / np.sqrt(2)
    Y = (rng.standard_normal((M, N)) + 1j * rng.standard_normal((M, N))) / np.sqrt(2)
    A = (H.conj().T @ H + 0.1 * np.eye(K)) / M
    B = H.conj().T @ Y / M

    def stored(Z, fmt):
        return fx.quantize_real(Z.real, fmt), fx.quantize_real(Z.imag, fmt)

    (ar, ai), (br, bi) = stored(A, f.A), stored(B, f.B)
    for explicit in (False, True):
        st = cg.cg_solve(ar, ai, br, bi, K, f, explicit=explicit)
        X = fx.to_float(st.xr, f.X) + 1j * fx.to_float(st.xi, f.X)
        assert np.max(np.abs(X - np.linalg.solve(A, B))) < WIDE_TOL


def test_solve_refuses_formats_past_the_64_bit_cap():
    f = cg.CgFormats(
        A=reg(40, 3), B=reg(40, 4), P=reg(40, 4), R=reg(40, 4), S=reg(40, 5),
        X=reg(40, 3), ps=reg(40, 10), rz=reg(40, 9), alpha=reg(40, 5), beta=reg(40, 3),
    )  # fmt: skip
    z = np.zeros((4, 4), np.int64)
    with pytest.raises(NotImplementedError):
        cg.cg_solve(z, z, z, z, 1, f)
    keys = {"mm_ap", "mm_ax", "dot_ps", "dot_rz", "rzw"}
    assert set(cg.accumulator_formats(wide(), 4)) == keys
