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


def _dft4(vr: list, vi: list, f: Format, first: bool) -> tuple[list, list, Format]:
    """One radix-4 butterfly, with the formats CONFIRMED BY TRACE (not derived).

    ``hls_ssr_fft_parallel_fft_kernel.hpp:104-180`` declares three types, so a radix-4 stage
    carries *two* accumulator levels::

        T_productType  = ButterflyTraits<isFirst, mode, T_bflyIn>::T_butterflyComplexRotatedType
        stage1_accum   = ButterflyTraits<isFirst, mode, T_productType>::T_butterflyAccumType
        stage2_accum   = ButterflyTraits<isFirst, mode, stage1_accum>::T_butterflyAccumType   <- output

    Measured by instrumenting a copy of the headers (``cpp/dump_stages.cpp``):

        stage 1 (isFirst):  in (16,2) -> prod (17,3) -> out (19,5)
        stage 2:            in (19,5) -> prod (20,5) -> out (22,7)

    The rotation is a ``complexMultiply`` into ``T_productType``, and ``local_r4_kernel[i][j]``
    is ``W_4^{i*j}`` built from exact ``+-1`` / ``0`` constants.
    """
    fprod = _f(f.W + 1, f.int_bits + (1 if first else 0))
    facc1 = _f(fprod.W + 1, fprod.int_bits + 1)
    facc2 = _f(facc1.W + 1, facc1.int_bits + 1)
    ftw = exp_table_format(18, 2)
    ei = np.arange(16, dtype=np.float64)
    ex_r = fp.quantize_real(np.cos(2.0 * np.pi * ei / R), ftw)
    ex_i = fp.quantize_real(-np.sin(2.0 * np.pi * ei / R), ftw)

    out_r, out_i = [], []
    for i in range(R):
        pr, pi = [], []
        for j in range(R):
            r, m = complex_multiply(vr[j], vi[j], f,
                                    np.array([ex_r[(i * j) % R]]), np.array([ex_i[(i * j) % R]]),
                                    ftw, fprod)
            pr.append(r)
            pi.append(m)
        l1r = [fp._apply_overflow(pr[0] + pr[1], facc1), fp._apply_overflow(pr[2] + pr[3], facc1)]
        l1i = [fp._apply_overflow(pi[0] + pi[1], facc1), fp._apply_overflow(pi[2] + pi[3], facc1)]
        out_r.append(fp._apply_overflow(l1r[0] + l1r[1], facc2))
        out_i.append(fp._apply_overflow(l1i[0] + l1i[1], facc2))
    return out_r, out_i, facc2


def fft16(x_re: np.ndarray, x_im: np.ndarray, in_w: int, in_i: int,
          tw_w: int = 18, tw_i: int = 2) -> tuple[np.ndarray, np.ndarray, Format]:
    """L=16, R=4, NO_SCALING, natural order, forward.  Stored ints in, stored ints out.

    The inter-stage rotation **preserves** the stage-1 output format -- the trace shows stage-2
    ``bfly_in`` at ``(19,5)``, the same as stage-1 ``bfly_out`` -- and the internal ``(22,7)``
    is cast down to the declared output ``(21,7)`` at the end.
    """
    length = 16
    fin = _f(in_w, in_i)
    ftw = exp_table_format(tw_w, tw_i)
    tw_r, tw_i_ = twiddle_stored(length, tw_w, tw_i)

    stage1 = {n2: _dft4([np.array([x_re[n2 + R * n1]]) for n1 in range(R)],
                        [np.array([x_im[n2 + R * n1]]) for n1 in range(R)], fin, True)
              for n2 in range(R)}
    f1 = stage1[0][2]                                   # (19,5)

    rotated = {}
    for n2 in range(R):
        for k1 in range(R):
            rotated[(n2, k1)] = complex_multiply(
                stage1[n2][0][k1], stage1[n2][1][k1], f1,
                np.array([tw_r[(n2 * k1) % length]]), np.array([tw_i_[(n2 * k1) % length]]),
                ftw, f1)

    out_r = np.zeros(length, dtype=np.int64)
    out_i = np.zeros(length, dtype=np.int64)
    fo = f1
    for k1 in range(R):
        orr, oii, fo = _dft4([rotated[(n2, k1)][0] for n2 in range(R)],
                             [rotated[(n2, k1)][1] for n2 in range(R)], f1, False)
        for k2 in range(R):
            out_r[k1 + R * k2] = int(orr[k2][0])
            out_i[k1 + R * k2] = int(oii[k2][0])

    fout = _f(in_w + 4 + 1, in_i + 4 + 1)               # FFTOutputTraits: in + log2(L) + 1
    return fp.quantize(out_r, fo, fout), fp.quantize(out_i, fo, fout), fout
