"""The agent test's kernels, goldens and scorer (``plans/simdcore_agent_test.md``).

Written from the pre-registration's table by someone other than the agent under test; the agent never
sees this directory. :func:`score` runs a program on the interpreter with the kernel's configuration on
seeds 0-4 and compares every output element (and ``a0`` where the kernel returns a scalar).

Each kernel's configuration is ``SimdCoreConfig(L=2, vlen=2048)`` with its windows in the order the task
text names them (window ``q`` is the ``q``-th); one block of ``N`` rows a window (``N`` = 64 at ``e32``,
128 at ``e16``), ``a0 = N``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from waveflow.simdcore.config import SimdCoreConfig, Window
from waveflow.simdcore.model import cpack, cunpack, run_job

SEEDS = range(5)
M32 = 0xFFFFFFFF


def rnu(p, d: int):
    """Round half up: ``(p + 2^(d-1)) >> d``."""
    p = np.asarray(p, dtype=np.int64)
    return (p + (1 << (d - 1))) >> d if d else p


def sat16(v):
    return np.clip(np.asarray(v, dtype=np.int64), -32768, 32767)


def sat32(v):
    return np.clip(np.asarray(v, dtype=np.int64), -(1 << 31), (1 << 31) - 1)


@dataclass(frozen=True)
class Spec:
    """One kernel: ``windows`` as ``(name, register, direction, sew)``; ``data(rng)`` -> inputs by
    window, the scalar arguments ``a1..`` and anything the golden needs; ``golden(d)`` -> the expected
    outputs by window and/or ``"a0"``."""

    name: str
    windows: tuple
    data: Callable[[np.random.Generator], dict]
    golden: Callable[[dict], dict]
    vdiv: bool = False
    task: str = ""  # the pre-registration's task text (for the record; the agent gets it from there)
    args: Callable[[dict], list] = field(default=lambda d: [])

    def config(self) -> SimdCoreConfig:
        return SimdCoreConfig(L=2, vlen=2048, vdiv=self.vdiv,
                              windows=tuple(Window(n, r, 1, dr, sew) for n, r, dr, sew in self.windows))

    @property
    def n(self) -> int:
        sew = self.windows[0][3]
        return 2048 // sew


def _c16(rng, n, lo=-32768, hi=32767):
    return rng.integers(lo, hi + 1, n), rng.integers(lo, hi + 1, n)


def _cw(rng):
    return int(rng.integers(-32768, 32768)), int(rng.integers(-32768, 32768))


def _cword(re, im) -> int:
    return int(cpack([re], [im])[0])


def _col(v, sew: int) -> np.ndarray:
    """A ``k x 1`` block of ``sew``-bit elements."""
    v = np.asarray(v, dtype=np.int64) & ((1 << sew) - 1)
    return v.astype(np.uint32 if sew == 32 else np.uint16)[:, None]


# --- 1 beamform-2 ------------------------------------------------------------------------------------

def _bf_data(rng):
    x0, x1 = _c16(rng, 64), _c16(rng, 64)
    w0, w1 = _cw(rng), _cw(rng)
    return {"X0": cpack(*x0), "X1": cpack(*x1), "w": (w0, w1), "x": (x0, x1)}


def _bf_golden(d):
    def term(w, x):
        (wr, wi), (xr, xi) = w, x
        re = wr * np.asarray(xr) + wi * np.asarray(xi)  # conj(w) x
        im = wr * np.asarray(xi) - wi * np.asarray(xr)
        return sat16(rnu(re, 15)), sat16(rnu(im, 15))

    (r0, i0), (r1, i1) = term(d["w"][0], d["x"][0]), term(d["w"][1], d["x"][1])
    return {"Y": cpack(sat16(r0 + r1), sat16(i0 + i1))}


# --- 2 power-count -----------------------------------------------------------------------------------

def _pc_data(rng):
    xr, xi = _c16(rng, 64, -32767, 32767)
    e = xr * xr + xi * xi
    t = int(e[int(rng.integers(0, 64))])  # a threshold some elements equal: '>' is strict
    return {"X": cpack(xr, xi), "T": t, "e": e}


def _pc_golden(d):
    return {"a0": int(np.sum(d["e"] > d["T"]))}


# --- 3 rotate ----------------------------------------------------------------------------------------

def _rot_data(rng):
    x, r = _c16(rng, 64), _c16(rng, 64)
    return {"X": cpack(*x), "R": cpack(*r), "x": x, "r": r}


def _rot_golden(d):
    (xr, xi), (rr, ri) = d["x"], d["r"]
    re = sat16(rnu(xr * rr - xi * ri, 15))
    im = sat16(rnu(xr * ri + xi * rr, 15))
    return {"Y": cpack(rnu(re, 1), rnu(im, 1))}


# --- 4 normalize-peak --------------------------------------------------------------------------------

def _np_data(rng):
    x = rng.integers(-(1 << 20), 1 << 20, 64)
    if rng.integers(0, 2):
        x[int(rng.integers(0, 64))] = -(1 << 20)  # the most negative value: |x| = 2^20
    return {"X": x.astype(np.int64)}


def _np_golden(d):
    x = d["X"]
    m = max(1, int(np.max(np.abs(x))))
    num = x << 10
    q = np.sign(num) * (np.abs(num) // m)  # truncating toward zero
    return {"Y": q}


# --- 5 horner-4 --------------------------------------------------------------------------------------

def _vsmul16(a, b):
    return sat16(rnu(np.asarray(a, np.int64) * np.asarray(b, np.int64), 15))


def _ho_data(rng):
    return {"X": rng.integers(-32768, 32768, 128), "c": [int(v) for v in rng.integers(-32768, 32768, 5)]}


def _ho_golden(d):
    x, c = d["X"], d["c"]
    s = np.full(x.shape, c[4], dtype=np.int64)
    for k in (3, 2, 1, 0):
        s = sat16(c[k] + _vsmul16(x, s))
    return {"Y": s}


# --- 6 argmin ----------------------------------------------------------------------------------------

def _am_data(rng):
    x = rng.integers(-(1 << 31), 1 << 31, 64)
    if rng.integers(0, 2):  # a repeated minimum: the first index counts
        i, j = sorted(rng.choice(64, 2, replace=False))
        x[j] = x[i] = int(x.min()) - 1 if x.min() > -(1 << 31) else x.min()
    return {"X": x.astype(np.int64)}


def _am_golden(d):
    return {"a0": int(np.argmin(d["X"]))}


# --- 7 soft-limit ------------------------------------------------------------------------------------

def _sl_data(rng):
    return {"X": rng.integers(-32767, 32768, 128), "A": int(rng.integers(1, 1 << 14))}


def _sl_golden(d):
    x, a = np.asarray(d["X"], np.int64), d["A"]
    ax = np.abs(x)
    return {"Y": np.where(ax <= a, x, np.sign(x) * (a + ((ax - a) >> 2)))}


# --- 8 complex-energy-sum ----------------------------------------------------------------------------

def _es_data(rng):
    xr, xi = _c16(rng, 64)
    return {"X": cpack(xr, xi), "x": (xr, xi)}


def _es_golden(d):
    xr, xi = d["x"]
    return {"a0": int(np.sum(rnu(xr * xr + xi * xi, 8))) & M32}


C, E16, E32 = 32, 16, 32
KERNELS: dict[str, Spec] = {s.name: s for s in (
    Spec("beamform-2", (("X0", 1, "in", C), ("X1", 2, "in", C), ("Y", 3, "out", C)), _bf_data, _bf_golden,
         args=lambda d: [_cword(*d["w"][0]), _cword(*d["w"][1])]),
    Spec("power-count", (("X", 1, "in", C),), _pc_data, _pc_golden, args=lambda d: [d["T"]]),
    Spec("rotate", (("X", 1, "in", C), ("R", 2, "in", C), ("Y", 3, "out", C)), _rot_data, _rot_golden),
    Spec("normalize-peak", (("X", 1, "in", E32), ("Y", 3, "out", E32)), _np_data, _np_golden, vdiv=True),
    Spec("horner-4", (("X", 1, "in", E16), ("Y", 3, "out", E16)), _ho_data, _ho_golden,
         args=lambda d: d["c"]),
    Spec("argmin", (("X", 1, "in", E32),), _am_data, _am_golden),
    Spec("soft-limit", (("X", 1, "in", E16), ("Y", 3, "out", E16)), _sl_data, _sl_golden,
         args=lambda d: [d["A"]]),
    Spec("complex-energy-sum", (("X", 1, "in", C),), _es_data, _es_golden),
)}


def run(spec: Spec, program: str, seed: int):
    """``(result, golden)`` for one seed."""
    rng = np.random.default_rng(seed)
    d = spec.data(rng)
    cfg = spec.config()
    inputs = {n: [_col(d[n], sew)] for n, _r, dr, sew in spec.windows if dr == "in"}
    args = [spec.n] + [a & M32 for a in spec.args(d)]
    return run_job(cfg, program, inputs=inputs, args=args), spec.golden(d)


def score(name: str, program: str) -> tuple[bool, str]:
    """``(passed, message)``: the message is what the agent gets back on a failure -- the assembler's or
    interpreter's error, or the first mismatching element."""
    spec = KERNELS[name]
    for seed in SEEDS:
        try:
            r, g = run(spec, program, seed)
        except Exception as e:  # the assembler's error
            return False, f"assembly failed: {e}"
        if r.status != "OK":
            return False, f"status {r.status}: {r.error}"
        for key, exp in g.items():
            if key == "a0":
                got = r.state.x[10]
                if got != (exp & M32):
                    return False, f"a0 mismatch (seed {seed}): got {got}, expected {exp & M32}"
                continue
            sew = next(s for n, _r, _d, s in spec.windows if n == key)
            outs = r.outputs.get(key, [])
            if len(outs) != 1:
                return False, f"window {key}: {len(outs)} blocks pushed, expected 1"
            got = np.asarray(outs[0], np.int64).reshape(-1)
            want = np.asarray(exp, np.int64).reshape(-1) & ((1 << sew) - 1)
            if got.shape != want.shape:
                return False, f"window {key}: {got.size} rows pushed, expected {want.size}"
            bad = np.flatnonzero(got != want)
            if bad.size:
                return False, f"output mismatch at element {int(bad[0])} (seed {seed})"
    return True, "pass"


__all__ = ["KERNELS", "SEEDS", "Spec", "cunpack", "run", "score"]
