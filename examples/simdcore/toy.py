"""Toy kernels for the SIMD core (``plans/simdcore.md`` Stage 3): each a short program, its windows and a
NumPy golden written from the math (integers, not from the interpreter).

=========  =====================================================================  ======  ===
kernel     computes                                                               ``e``   S
=========  =====================================================================  ======  ===
``cmul``   ``y = rs(a · b)``, complex Q15                                         32      1
``gain``   ``y = rs(g · a)``, ``g`` a complex scalar (``.vx``)                    32      1
``abs2``   ``y = |a|²``, a 32-bit real (``wf.vcabs2``)                            32      1
``clip``   ``y = min(max(x, lo), hi)``, 16-bit real                               16      2
``poly``   ``y = c0 + x(c1 + x(c2 + x·c3))``, Q15 Horner (``vsmul`` + ``vsadd``)  16      7
``pwl``    ``y = x < t ? s0·x : s1·x + c``, both pieces then a select            16      6
``lut``    ``y = B[i] + D[i]·f``, ``i`` = ``x``'s top 6 bits, ``f`` its fraction  16      7
``argmax`` ``a0 = argmax(x)`` (``vredmax`` -> ``vmseq`` -> ``vfirst``)             32      3
``sum``    ``a0 = Σ x`` (``vredsum``)                                             32      1
=========  =====================================================================  ======  ===

``rs(p)`` rounds half up (``vxrm`` 0) by ``csh`` = 15 and saturates each part to 16 bits.  One job is one
block of ``N <= VLMAX`` elements in window ``A`` (and ``B``), the result in window ``Y`` or in ``a0``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from waveflow.simdcore.config import SimdCoreConfig, Window
from waveflow.simdcore.model import JobResult, cpack, run_job

V_A, V_B, V_Y = 1, 2, 3


def config(L: int = 2, vlen: int = 2048, sew: int = 32, two_in: bool = False, **kw) -> SimdCoreConfig:
    w = [Window("A", V_A, 1, "in", sew)]
    if two_in:
        w.append(Window("B", V_B, 1, "in", sew))
    w.append(Window("Y", V_Y, 1, "out", sew))
    return SimdCoreConfig(L=L, vlen=vlen, windows=tuple(w), **kw)


def _rnu(p, d):
    return (np.asarray(p, dtype=np.int64) + (1 << (d - 1))) >> d


def _s16(v):
    return np.clip(v, -32768, 32767)


def _q15mul(a, b):  # vsmul at e16, vxrm 0
    return _s16(_rnu(np.asarray(a, np.int64) * np.asarray(b, np.int64), 15))


@dataclass(frozen=True)
class Kernel:
    name: str
    sew: int
    two_in: bool
    text: Callable[[], str]
    data: Callable[[np.random.Generator, int], dict]  # -> {"A": ..., "B": ..., "args": ..., ...}
    golden: Callable[[dict], np.ndarray | int]
    scalar_out: bool = False
    lut: Callable[[], np.ndarray] | None = None


def _cdata(rng, n):
    return {"A": (rng.integers(-32768, 32768, n), rng.integers(-32768, 32768, n)),
            "B": (rng.integers(-32768, 32768, n), rng.integers(-32768, 32768, n))}


def _cmul_g(d):
    (ar, ai), (br, bi) = d["A"], d["B"]
    return cpack(_s16(_rnu(ar * br - ai * bi, 15)), _s16(_rnu(ar * bi + ai * br, 15)))


def _gain_g(d):
    ar, ai = d["A"]
    gr, gi = d["g"]
    return cpack(_s16(_rnu(ar * gr - ai * gi, 15)), _s16(_rnu(ar * gi + ai * gr, 15)))


def _abs2_g(d):
    ar, ai = d["A"]
    return np.clip(ar * ar + ai * ai, -(2**31), 2**31 - 1).astype(np.int64).astype(np.uint32)


def _clip_g(d):
    return np.clip(d["A"], d["lo"], d["hi"])


def _poly_g(d):
    x, c = np.asarray(d["A"], np.int64), d["c"]
    acc = np.full_like(x, c[3])
    for k in (2, 1, 0):
        acc = _s16(_q15mul(acc, x) + c[k])
    return acc


def _pwl_g(d):
    x = np.asarray(d["A"], np.int64)
    lo = _q15mul(x, d["s0"])
    hi = _s16(_q15mul(x, d["s1"]) + d["c"])
    return np.where(x < d["t"], lo, hi)


LUT_N = 64


def _lut_tables():
    """``B[i]`` and ``D[i]`` of sin(π/2 · u), u ∈ [0, 1), Q15, 64 segments (a piecewise-linear table)."""
    u = np.arange(LUT_N + 1) / LUT_N
    v = np.round(np.sin(np.pi / 2 * u) * 32767).astype(np.int64)
    base = v[:-1]
    slope = np.clip(v[1:] - v[:-1], -32768, 32767) * 32  # per unit fraction (frac is Q15 of 1/64)
    return np.clip(base, -32768, 32767), np.clip(slope, -32768, 32767)


def _lut_g(d):
    x = np.asarray(d["A"], np.int64) & 0xFFFF  # unsigned u in [0, 1) as Q16
    i = x >> 10
    f = (x & 1023) << 5  # Q15
    base, slope = _lut_tables()
    return _s16(base[i] + _q15mul(slope[i], f))


def _lut_image():
    """The LUT region: ``B`` (64 halfwords) then ``D`` (64 halfwords)."""
    base, slope = _lut_tables()
    return np.concatenate([base, slope]).astype(np.int16).view(np.uint16)


KERNELS: dict[str, Kernel] = {}


def _k(k: Kernel):
    KERNELS[k.name] = k


_PRE32 = "    vsetvli t0, a0, e32, m1, ta, ma\n    wf.qpop 0\n"
_PRE32B = _PRE32 + "    wf.qpop 1\n"
_PRE16 = "    vsetvli t0, a0, e16, m1, ta, ma\n    wf.qpop 0\n"
_POST = "    wf.qpush {q}\n    ecall\n"

_k(Kernel("cmul", 32, True, lambda: _PRE32B + "    wf.vcmul.vv v3, v1, v2\n" + _POST.format(q=2), _cdata, _cmul_g))
_k(Kernel("gain", 32, False, lambda: _PRE32 + "    wf.vcmul.vx v3, v1, a1\n" + _POST.format(q=1),
          lambda rng, n: {"A": _cdata(rng, n)["A"], "g": (int(rng.integers(-32768, 32768)),
                                                          int(rng.integers(-32768, 32768)))}, _gain_g))
_k(Kernel("abs2", 32, False, lambda: "    csrwi wfsh, 15\n" + _PRE32 + "    wf.vcabs2.v v3, v1\n" + _POST.format(q=1),
          lambda rng, n: {"A": _cdata(rng, n)["A"]}, _abs2_g))
_k(Kernel("clip", 16, False, lambda: _PRE16 + "    vmax.vx v3, v1, a1\n    vmin.vx v3, v3, a2\n" + _POST.format(q=1),
          lambda rng, n: {"A": rng.integers(-32768, 32768, n), "lo": -12000, "hi": 9000}, _clip_g))
_k(Kernel("poly", 16, False, lambda: _PRE16 + "    vmv.v.x v3, a4\n"
          "    vsmul.vv v3, v3, v1\n    vsadd.vx v3, v3, a3\n"
          "    vsmul.vv v3, v3, v1\n    vsadd.vx v3, v3, a2\n"
          "    vsmul.vv v3, v3, v1\n    vsadd.vx v3, v3, a1\n" + _POST.format(q=1),
          lambda rng, n: {"A": rng.integers(-32768, 32768, n), "c": [3000, -16000, 9000, 20000]}, _poly_g))
_k(Kernel("pwl", 16, False, lambda: _PRE16 +
          "    vsmul.vx v4, v1, a2\n"          # lo piece: s0 x
          "    vsmul.vx v5, v1, a3\n    vsadd.vx v5, v5, a4\n"  # hi piece: s1 x + c
          "    vmslt.vx v0, v1, a1\n"          # mask: x < t
          "    vmerge.vvm v3, v5, v4, v0\n" + _POST.format(q=1),
          lambda rng, n: {"A": rng.integers(-32768, 32768, n), "t": 4000, "s0": 26000, "s1": 9000, "c": 2500},
          _pwl_g))
_k(Kernel("lut", 16, False, lambda: _PRE16 +
          "    vsrl.vi v4, v1, 10\n    vsll.vi v4, v4, 1\n"       # byte offset of B[i]
          "    vand.vx v5, v1, a2\n    vsll.vi v5, v5, 5\n"       # f, Q15
          "    vluxei16.v v6, (a1), v4\n"                         # B[i]
          "    addi t1, a1, 128\n    vluxei16.v v7, (t1), v4\n"   # D[i]
          "    vsmul.vv v7, v7, v5\n    vsadd.vv v3, v6, v7\n" + _POST.format(q=1),
          lambda rng, n: {"A": rng.integers(0, 65536, n)}, _lut_g, lut=_lut_image))
_k(Kernel("argmax", 32, False, lambda: _PRE32 +
          "    vmv.s.x v4, a1\n    vredmax.vs v4, v1, v4\n    vmv.x.s t1, v4\n"
          "    vmseq.vx v0, v1, t1\n    vfirst.m a0, v0\n    ecall\n",
          lambda rng, n: {"A": rng.integers(-(2**31), 2**31, n)}, lambda d: int(np.argmax(d["A"])), scalar_out=True))
_k(Kernel("sum", 32, False, lambda: _PRE32 + "    vmv.s.x v4, zero\n    vredsum.vs v4, v1, v4\n    vmv.x.s a0, v4\n"
          "    ecall\n", lambda rng, n: {"A": rng.integers(-(2**20), 2**20, n)},
          lambda d: int(np.sum(d["A"])) & 0xFFFFFFFF, scalar_out=True))


def _block(k: Kernel, v) -> np.ndarray:
    if isinstance(v, tuple):
        return cpack(*v)[:, None]
    return (np.asarray(v, np.int64) & ((1 << k.sew) - 1)).astype(np.uint32 if k.sew == 32 else np.uint16)[:, None]


def run_kernel(name: str, n: int, *, L: int = 2, seed: int = 0, vlen: int = 2048) -> tuple[JobResult, object, object]:
    """``(result, the core's output, the golden)`` for ``n`` random elements."""
    k = KERNELS[name]
    rng = np.random.default_rng(seed)
    d = k.data(rng, n)
    cfg = config(L, vlen, k.sew, k.two_in)
    inputs = {"A": [_block(k, d["A"])]}
    if k.two_in:
        inputs["B"] = [_block(k, d["B"])]
    args = {10: n}
    if name == "gain":
        args[11] = int(cpack([d["g"][0]], [d["g"][1]])[0])
    elif name == "clip":
        args.update({11: d["lo"] & 0xFFFFFFFF, 12: d["hi"] & 0xFFFFFFFF})
    elif name == "poly":
        args.update({11 + i: c & 0xFFFFFFFF for i, c in enumerate(d["c"])})
    elif name == "pwl":
        args.update({11: d["t"], 12: d["s0"], 13: d["s1"], 14: d["c"]})
    elif name == "lut":
        args.update({11: cfg.lut_base, 12: 1023})
    elif name == "argmax":
        args[11] = 0x80000000  # the smallest e32 value: the reduction's start
    dm = [(cfg.lut_base, k.lut())] if k.lut else None
    r = run_job(cfg, k.text(), inputs=inputs, args=args, dm_init=dm).check()
    gold = k.golden(d)
    if k.scalar_out:
        return r, r.state.x[10], gold
    out = r.outputs["Y"][0][:, 0]
    if k.sew == 16:
        out = out.astype(np.uint16).view(np.int16).astype(np.int64)
        gold = np.asarray(gold, np.int64)
    else:
        gold = np.asarray(gold).astype(np.uint32)
    return r, out, gold
