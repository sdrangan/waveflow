"""The multi-antenna matched filter as a program for the SIMD core (``plans/simdcore.md`` Stage 3).

``Y[i, j] = conj(X[i]) · R[i, j]``, ``i`` over a frame's samples, ``j < nrx`` over the antennas -- the
computation of ``examples/vecunit_mf``, in its formats (``X`` Q2.14 rounding, ``R`` Q3.13 rounding,
``Y`` Q3.13 truncating, all saturating), so the result is **bit-exact against that example's golden**:
the exact product rounded once, which is ``wf.vcmulc`` with ``csh`` = 14 + 13 - 13 = 14 and ``vxrm`` =
rdn (floor).

=====================  ==========================================================================
matched filter         SIMD core (records D10: I/O register windows)
=====================  ==========================================================================
a chunk of samples     ``VL <= VLMAX`` elements (``vsetvli`` strip-mines the frame)
``X`` (chunk)          input window ``X``: one register, ``v1`` (``v0`` is the mask register)
``R`` (chunk x nrx)    input window ``R``: ``v8 .. v8+nrx-1``, antenna ``j`` in ``v8+j``
``Y``                  output window ``Y``: ``v16 .. v16+nrx-1``
=====================  ==========================================================================

A chunk is ``nrx`` instructions ``wf.vcmulc.vv v16+j, v1, v8+j`` -- one complex multiply each, ``S`` = 1
an antenna -- between two ``wf.qpop`` and a ``wf.qpush``::

        csrwi   wfsh, 14
        csrwi   vxrm, 2
    chunk:
        vsetvli t0, a0, e32, m1, ta, ma
        wf.qpop 0                       # X
        wf.qpop 1                       # R
        wf.vcmulc.vv v16, v1, v8        # ... one an antenna
        wf.qpush 2                      # Y
        sub     a0, a0, t0
        bnez    a0, chunk
        ecall
"""

from __future__ import annotations

import numpy as np

from waveflow.simdcore.config import SimdCoreConfig, Window
from waveflow.simdcore.model import JobResult, cpack, cunpack, run_job

#: Window indices, in the order of the configuration's ``windows``.
Q_X, Q_R, Q_Y = 0, 1, 2
V_X, V_R, V_Y = 1, 8, 16
CSH = 14  # Q2.14 x Q3.13 -> Q3.13
VXRM_RDN = 2


def mf_config(nrx: int = 8, *, L: int = 2, vlen: int = 2048, **kw) -> SimdCoreConfig:
    """A core for the matched filter at ``nrx`` antennas: chunks of ``vlen / 32`` samples."""
    if not 1 <= nrx <= 8:
        raise ValueError(f"nrx = {nrx}: 1..8")
    return SimdCoreConfig(L=L, vlen=vlen, windows=(
        Window("X", V_X, 1, "in"), Window("R", V_R, nrx, "in"), Window("Y", V_Y, nrx, "out")), **kw)


def mf_text(nrx: int) -> str:
    """The program (one job = one frame; ``a0`` = the frame's samples)."""
    body = "".join(f"    wf.vcmulc.vv v{V_Y + j}, v{V_X}, v{V_R + j}\n" for j in range(nrx))
    return (f"    csrwi wfsh, {CSH}\n    csrwi vxrm, {VXRM_RDN}\n"
            "chunk:\n"
            "    vsetvli t0, a0, e32, m1, ta, ma\n"
            f"    wf.qpop {Q_X}\n    wf.qpop {Q_R}\n" + body +
            f"    wf.qpush {Q_Y}\n"
            "    sub a0, a0, t0\n    bnez a0, chunk\n    ecall\n")


def mf_inputs(cfg: SimdCoreConfig, xr, xi, rr, ri) -> dict[str, list[np.ndarray]]:
    """Per chunk an ``X`` block (``k x 1``) and an ``R`` block (``k x nrx``) of packed complex words."""
    vlmax = cfg.vlmax(32)
    n = len(xr)
    xs, rs = [], []
    for c0 in range(0, n, vlmax):
        sl = slice(c0, min(n, c0 + vlmax))
        xs.append(cpack(np.asarray(xr)[sl], np.asarray(xi)[sl])[:, None])
        rs.append(cpack(np.asarray(rr)[sl], np.asarray(ri)[sl]))
    return {"X": xs, "R": rs}


def run_mf(cfg: SimdCoreConfig, xr, xi, rr, ri) -> tuple[np.ndarray, np.ndarray, JobResult]:
    """One frame through the interpreter: ``(Y re, Y im, the job's result)``."""
    nsamp, nrx = np.shape(rr)
    r = run_job(cfg, mf_text(nrx), inputs=mf_inputs(cfg, xr, xi, rr, ri), args=[nsamp]).check()
    y = np.concatenate(r.outputs["Y"]) if r.outputs["Y"] else np.zeros((0, nrx), np.uint32)
    yr, yi = cunpack(y)
    return yr, yi, r


def input_floor(nsamp: int, nrx: int, word_bits: int = 64) -> int:
    """Input words a frame (``X`` and ``R``, two 32-bit complex values a 64-bit word): its fewest cycles."""
    per = word_bits // 32
    return -(-nsamp // per) + -(-nsamp * nrx // per)


def frame(nsamp: int, nrx: int, seed: int):
    """A frame's ``X`` (``nsamp``) and ``R`` (``nsamp x nrx``): random 16-bit stored integers."""
    rng = np.random.default_rng(seed)
    lo, hi = -(1 << 15), 1 << 15
    return (rng.integers(lo, hi, nsamp), rng.integers(lo, hi, nsamp),
            rng.integers(lo, hi, (nsamp, nrx)), rng.integers(lo, hi, (nsamp, nrx)))


def steady_table(lanes=(1, 2, 4, 8), nframes: int = 4, nsamp: int = 256, nrx: int = 8) -> list[dict]:
    """The cycle model's per-frame interval on ``nframes`` back-to-back frames (one job), by lane count
    (``plans/simdcore_records.md`` Stage 3; the interpreter's prediction, not a measurement)."""
    rows = []
    for L in lanes:
        cfg = mf_config(nrx, L=L)
        xr, xi, rr, ri = frame(nframes * nsamp, nrx, seed=L)
        _, _, r = run_mf(cfg, xr, xi, rr, ri)
        per = r.cycles / nframes
        rows.append({"L": L, "dsp": cfg.multipliers, "cycles_per_frame": per,
                     "vs_floor": per / input_floor(nsamp, nrx), "lane_cycles": r.timing.lane_cycles / nframes})
    return rows


if __name__ == "__main__":
    print(f"input floor: {input_floor(256, 8)} cycles a frame (256 samples x 8 antennas)")
    for row in steady_table():
        print("L={L:2d}  DSP={dsp:3d}  {cycles_per_frame:7.0f} cycles a frame  {vs_floor:4.2f}x floor  "
              "lanes busy {lane_cycles:5.0f}".format(**row))
