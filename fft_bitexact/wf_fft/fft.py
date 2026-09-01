"""Bit-exact sequential model of the Vitis L1 SSR FFT -- L=16, R=4, SSR_FFT_NO_SCALING.

Models the **arithmetic, not the parallelism**.  SSR is a throughput/layout property: the same
butterflies happen in the same order whatever the SSR factor, so a sequential model is
bit-identical to the hardware.  None of the data commutors, barrel shifters or super-sample
containers in the C++ affect a single bit.

Decomposition, for ``L = R^2`` with ``n = n2 + R*n1`` and ``k = k1 + R*k2``::

    X[k1 + R k2] = sum_n2 W_R^{n2 k2} * ( W_L^{n2 k1} * ( sum_n1 x[n2 + R n1] W_R^{n1 k1} ) )
                   \\_____ stage 2 _____/   \\_ rotate _/   \\________ stage 1 ________/

Where the bits are decided -- all three read out of the library, not guessed:

* **The radix-4 DFT is exact.**  ``ComplexExpTable`` holds ``W_R^k``, and for ``R = 4`` those
  are exactly ``{1, -j, -1, +j}`` (stored ``+-65536`` in ``ap_fixed<18,2>``), so the butterfly
  multiplies are sign flips and swaps.  No rounding.
* **The adder tree is exact.**  ``m_outputData[n] = p_data[2n] + p_data[2n+1]`` accumulates into
  ``T_butterflyAccumType = ap_fixed<W+1, I+1>`` (``hls_ssr_fft_butterfly_traits.hpp:34-37``),
  which is exactly ``add_format``.  Growth, not loss.  Two levels per radix-4 stage.
* **The rotation is the only lossy step** -- one ``complexMultiply`` per sample per stage
  boundary, with its partial products truncated into ``T_op1``.  See ``cxquant.complex_multiply``.

So the whole transform loses precision in exactly one place, which is why ``L=16`` lands on
``ap_fixed<21,7>``: ``16,2`` + 2 adder levels -> ``18,4``, + first-stage rotation growth ->
``19,5``, + 2 more adder levels -> ``21,7``.  That equals
``OUTPUT_WL = in_W + log2(L) + 1`` (``hls_ssr_fft_output_traits.hpp:147-151``).
"""
from __future__ import annotations

import numpy as np

from waveflow.utils import fixputils as fp
from waveflow.utils.fixputils import Format, OMode, QMode

from .cxquant import complex_multiply

R = 4


def _f(w: int, i: int, q: QMode = QMode.AP_TRN, o: OMode = OMode.AP_WRAP) -> Format:
    return Format(W=w, int_bits=i, signed=True, q_mode=q, o_mode=o)


def exp_table_format(tw_w: int, tw_i: int) -> Format:
    """Tables use the ROUNDING cast -- ``AP_RND``/``AP_SAT``.  See ``twiddle.py``."""
    return _f(tw_w, tw_i, QMode.AP_RND, OMode.AP_SAT)


def twiddle_stored(length: int, tw_w: int, tw_i: int) -> tuple[np.ndarray, np.ndarray]:
    """``W_L^i`` as stored integers -- the inter-stage rotation table."""
    ft = exp_table_format(tw_w, tw_i)
    i = np.arange(length, dtype=np.float64)
    return (fp.quantize_real(np.cos(2.0 * np.pi * i / length), ft),
            fp.quantize_real(-np.sin(2.0 * np.pi * i / length), ft))


def _dft4(vr: list, vi: list, f: Format) -> tuple[list, list, Format]:
    """Radix-4 DFT: exact, because ``W_4^k`` is ``{1, -j, -1, +j}``.

    Two binary adder-tree levels, each ``(W+1, I+1)``.  Overflow is applied at the accumulator
    format, matching the C++ storing each level into its declared type.
    """
    def rot(k: int, re, im):
        """Multiply by ``W_4^k`` in ``{1, -j, -1, +j}``.

        Kept in exact (unwrapped) integers: in the C++ this is an ``ap_fixed`` multiply whose
        product type is wider than the operand, so negating the most-negative operand does NOT
        wrap.  Wrapping is applied where the value is actually stored -- the accumulators below.
        """
        re = np.asarray(re, dtype=object)
        im = np.asarray(im, dtype=object)
        return [(re, im), (im, -re), (-re, -im), (-im, re)][k % 4]

    f1 = _f(f.W + 1, f.int_bits + 1)      # level 1 accumulator
    fo = _f(f.W + 2, f.int_bits + 2)      # level 2 accumulator
    out_r, out_i = [], []
    for k in range(R):
        pr, pi = [], []
        for n in range(R):
            a, b = rot((n * k) % R, vr[n], vi[n])
            pr.append(a)
            pi.append(b)
        # Each tree level is STORED into its own accumulator type, so overflow is applied per
        # level -- not once at the end.  Only extreme inputs distinguish the two, which is why
        # the golden carries an alternating-extremes vector.
        lr = [fp._apply_overflow(pr[0] + pr[1], f1), fp._apply_overflow(pr[2] + pr[3], f1)]
        li = [fp._apply_overflow(pi[0] + pi[1], f1), fp._apply_overflow(pi[2] + pi[3], f1)]
        out_r.append(fp._apply_overflow(lr[0] + lr[1], fo))
        out_i.append(fp._apply_overflow(li[0] + li[1], fo))
    return out_r, out_i, fo


def fft16(x_re: np.ndarray, x_im: np.ndarray, in_w: int, in_i: int,
          tw_w: int = 18, tw_i: int = 2) -> tuple[np.ndarray, np.ndarray, Format]:
    """L=16, R=4, NO_SCALING, natural order, forward.  Stored ints in, stored ints out."""
    length = 16
    fin = _f(in_w, in_i)
    ftw = exp_table_format(tw_w, tw_i)
    tw_r, tw_i_ = twiddle_stored(length, tw_w, tw_i)

    stage1 = {}
    for n2 in range(R):
        vr = [np.array([x_re[n2 + R * n1]]) for n1 in range(R)]
        vi = [np.array([x_im[n2 + R * n1]]) for n1 in range(R)]
        stage1[n2] = _dft4(vr, vi, fin)
    f1 = stage1[0][2]

    # First stage carries COMPLEX_ROTATED_BIT_GROWTH = 1 (butterfly_traits.hpp:37).
    frot = _f(f1.W + 1, f1.int_bits + 1)

    rotated = {}
    for n2 in range(R):
        for k1 in range(R):
            idx = (n2 * k1) % length
            rotated[(n2, k1)] = complex_multiply(
                stage1[n2][0][k1], stage1[n2][1][k1], f1,
                np.array([tw_r[idx]]), np.array([tw_i_[idx]]), ftw, frot)

    out_r = np.zeros(length, dtype=np.int64)
    out_i = np.zeros(length, dtype=np.int64)
    fo = frot
    for k1 in range(R):
        vr = [rotated[(n2, k1)][0] for n2 in range(R)]
        vi = [rotated[(n2, k1)][1] for n2 in range(R)]
        orr, oii, fo = _dft4(vr, vi, frot)
        for k2 in range(R):
            out_r[k1 + R * k2] = int(orr[k2][0])
            out_i[k1 + R * k2] = int(oii[k2][0])
    return out_r, out_i, fo
