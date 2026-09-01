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

from functools import cache

import numpy as np

from waveflow.utils import fixputils as fp
from waveflow.utils.fixputils import Format, OMode, QMode

from .cxquant import complex_multiply

R = 4

#: The three ``scaling_mode_enum`` values, as the goldens name them.
NO_SCALING = "SSR_FFT_NO_SCALING"
SCALE = "SSR_FFT_SCALE"
GROW_TO_MAX_WIDTH = "SSR_FFT_GROW_TO_MAX_WIDTH"


def _f(w: int, i: int, q: QMode = QMode.AP_TRN, o: OMode = OMode.AP_WRAP) -> Format:
    return Format(W=w, int_bits=i, signed=True, q_mode=q, o_mode=o)


def exp_table_format(tw_w: int, tw_i: int) -> Format:
    """Tables use the ROUNDING cast -- ``AP_RND``/``AP_SAT``.  See ``twiddle.py``."""
    return _f(tw_w, tw_i, QMode.AP_RND, OMode.AP_SAT)


def twiddle_stored(length: int, tw_w: int, tw_i: int) -> tuple[np.ndarray, np.ndarray]:
    """``W_L^i`` as stored integers -- the inter-stage rotation table, **as the hardware reads it**.

    Not a direct quantization of ``cos``/``sin``: past ``L=16`` the design stores only a quarter
    wave (``EXTENDED_TWIDDLE_TALBE_LENGTH = L/4``) and rebuilds the circle with
    ``readQuaterTwiddleTable``, substituting an exact ``-1`` at the two axis indices.  Reading
    the full circle directly agrees at ``L=16`` and diverges by an LSB beyond it -- which is
    what kept ``L=64`` off by 14 values.  See ``twiddle.quarter_twiddles``.
    """
    from .twiddle import quarter_twiddles
    return quarter_twiddles(length, tw_w, tw_i)


def _accumulate(a, b, operand: Format, target: Format):
    """One adder-tree addition: wrap at ``operand`` width, then convert into ``target``."""
    wrapped = fp._apply_overflow(a + b, operand)
    if (operand.W, operand.int_bits) == (target.W, target.int_bits):
        return wrapped
    return fp.quantize(wrapped, operand, target)


def _stage_formats(f: Format, first: bool, mode: str) -> tuple[Format, Format, Format]:
    """``(product, tree-level, tree-base)`` for one radix-4 stage.

    **Measured, not derived** -- ``cpp/dump_modes.cpp`` traces every declared width under all
    three scaling modes.  For ``ap_fixed<16,2>`` in, ``L=16``, ``R=4``::

        NO_SCALING          (16,2) -> (17,3) -> (18,4) -> (19,5)   then (19,5) -> (20,5) -> (21,6) -> (22,7)
        SCALE               (16,2) -> (16,3) -> (16,4) -> (16,5)   then (16,5) -> (16,5) -> (16,6) -> (16,7)
        GROW_TO_MAX_WIDTH   (16,2) -> (17,3) -> (18,4) -> (19,5)   then (19,5) -> (19,5) -> (20,6) -> (21,7)

    The integer part grows identically in all three -- ``+1`` per accumulator level, plus one
    more in the first stage's rotation.  The modes differ only in what happens to the WIDTH:

    * ``NO_SCALING`` widens at every step, so nothing is ever discarded.
    * ``SCALE`` holds the width fixed, so each level drops one fractional bit -- that *is* the
      per-stage right shift, expressed as a format rather than an explicit shift.
    * ``GROW_TO_MAX_WIDTH`` widens except in the rotation of a non-first stage.

    The width rules below are read off that table.  ``GROW_TO_MAX_WIDTH``'s cap (27 bits) is not
    reached at these sizes, so it is not modelled -- see PLAN.md.
    """
    grow_i = 1 if first else 0
    if mode == SCALE:
        fprod = _f(f.W, f.int_bits + grow_i)
        facc1 = _f(f.W, fprod.int_bits + 1)
        facc2 = _f(f.W, facc1.int_bits + 1)
    else:
        prod_w = f.W + (1 if (mode == NO_SCALING or first) else 0)
        fprod = _f(prod_w, f.int_bits + grow_i)
        facc1 = _f(fprod.W + 1, fprod.int_bits + 1)
        facc2 = _f(facc1.W + 1, facc1.int_bits + 1)
    return fprod, facc1, facc2


@cache
def _exp_table(tw_w: int, tw_i: int) -> tuple[Format, tuple, tuple]:
    """``ComplexExpTable`` -- the radix-R DFT constants ``W_R^k``, cached per format.

    ``initComplexExpTable`` (``hls_ssr_fft_complex_exp_table.hpp:70-86``) fills
    ``max(R, 16)`` entries with the same rounding cast the twiddle table uses.  For ``R = 4``
    the values are exactly ``{1, -j, -1, +j}``, so the butterfly multiplies are lossless -- but
    that is a consequence of the format, not a licence to hardcode it, which is what this
    function previously did (it built the table at a fixed ``<18,2>`` and silently ignored the
    caller's twiddle width).
    """
    ftw = exp_table_format(tw_w, tw_i)
    ei = np.arange(16, dtype=np.float64)
    return (ftw,
            tuple(fp.quantize_real(np.cos(2.0 * np.pi * ei / R), ftw)),
            tuple(fp.quantize_real(-np.sin(2.0 * np.pi * ei / R), ftw)))


def _dft4(vr: list, vi: list, f: Format, first: bool, mode: str,
          tw_w: int = 18, tw_i: int = 2) -> tuple[list, list, Format]:
    """One radix-4 butterfly.

    ``hls_ssr_fft_parallel_fft_kernel.hpp:104-180`` declares three types, so a radix-4 stage
    carries *two* accumulator levels; the rotation is a ``complexMultiply`` into the product
    type, and ``local_r4_kernel[i][j]`` is ``W_4^{i*j}`` from exact ``+-1`` / ``0`` constants.

    Tree additions wrap at their OPERAND width and widen on assignment -- see ``fft16``.
    """
    fprod, facc1, facc2 = _stage_formats(f, first, mode)
    ftw, ex_r, ex_i = _exp_table(tw_w, tw_i)

    out_r, out_i = [], []
    for i in range(R):
        pr, pi = [], []
        for j in range(R):
            r, m = complex_multiply(vr[j], vi[j], f,
                                    np.array([ex_r[(i * j) % R]]), np.array([ex_i[(i * j) % R]]),
                                    ftw, fprod)
            pr.append(r)
            pi.append(m)
        # A tree addition wraps at its OPERAND width, then converts into the declared
        # accumulator format.  Both halves matter, for different modes:
        #   * the wrap is what NO_SCALING needs -- measured: p2+p3 = +524288 (two ap_fixed<20,5>)
        #     is stored as -524288, a wrap at 20 bits, though the accumulator is declared (21,6).
        #   * the convert is what SCALE needs -- there the accumulator is the SAME width with one
        #     more integer bit, so converting drops a fractional bit.  That is the per-stage
        #     right shift, and _apply_overflow alone would silently skip it.
        # For NO_SCALING / GROW the convert keeps the fraction and is exact, so one rule serves
        # all three modes.
        l1r = [_accumulate(pr[0], pr[1], fprod, facc1), _accumulate(pr[2], pr[3], fprod, facc1)]
        l1i = [_accumulate(pi[0], pi[1], fprod, facc1), _accumulate(pi[2], pi[3], fprod, facc1)]
        out_r.append(_accumulate(l1r[0], l1r[1], facc1, facc2))
        out_i.append(_accumulate(l1i[0], l1i[1], facc1, facc2))
    return out_r, out_i, facc2


def fft16(x_re: np.ndarray, x_im: np.ndarray, in_w: int, in_i: int,
          tw_w: int = 18, tw_i: int = 2,
          mode: str = NO_SCALING) -> tuple[np.ndarray, np.ndarray, Format]:
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
                        [np.array([x_im[n2 + R * n1]]) for n1 in range(R)], fin, True, mode,
                        tw_w, tw_i)
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
                             [rotated[(n2, k1)][1] for n2 in range(R)], f1, False, mode,
                             tw_w, tw_i)
        for k2 in range(R):
            out_r[k1 + R * k2] = int(orr[k2][0])
            out_i[k1 + R * k2] = int(oii[k2][0])

    # Only NO_SCALING narrows at the end: its internal (22,7) is cast to the declared (21,7).
    # SCALE and GROW_TO_MAX_WIDTH already land on their output format -- measured, see
    # cpp/dump_modes.cpp.
    fout = _f(in_w + 4 + 1, in_i + 4 + 1) if mode == NO_SCALING else fo
    if fout == fo:
        return out_r, out_i, fout
    return fp.quantize(out_r, fo, fout), fp.quantize(out_i, fo, fout), fout


# ---------------------------------------------------------------------------------------------
# General L = R^S
# ---------------------------------------------------------------------------------------------
def _log(n: int, base: int) -> int:
    k, v = 0, 1
    while v < n:
        v *= base
        k += 1
    if v != n:
        raise ValueError(f"{n} is not a power of {base}")
    return k


def stage_formats(in_w: int, in_i: int, n_stages: int,
                  mode: str = NO_SCALING) -> list[tuple[Format, Format]]:
    """``(input, output)`` format per stage, for ``L = R^n_stages``.

    Measured at ``L=16`` (2 stages) and ``L=64`` (3 stages) with ``cpp/dump_stages.cpp``::

        stage 1   (16,2) -> (19,5)        the rotation after it PRESERVES the format
        stage 2   (19,5) -> (22,7)        the rotation after it drops one fractional bit
        stage 3   (21,7) -> (24,9)        and so does the final output cast

    So a stage widens by 3, and every inter-stage rotation except the first narrows by 1.
    Total: ``in_W + 3S - (S-1) = in_W + 2S + 1``, and since ``log2(L) = 2S`` for ``R=4`` that is
    the library's own ``OUTPUT_WL = in_W + log2(L) + 1`` -- which is how this rule was checked
    at ``L=1024`` before any sample was compared.
    """
    out = []
    f = _f(in_w, in_i)
    for s in range(n_stages):
        _, _, g = _stage_formats(f, s == 0, mode)
        out.append((f, g))
        f = g if s == 0 else _f(g.W - 1, g.int_bits)
    return out


def fft_general(x_re: np.ndarray, x_im: np.ndarray, length: int, in_w: int, in_i: int,
                tw_w: int = 18, tw_i: int = 2,
                mode: str = NO_SCALING) -> tuple[np.ndarray, np.ndarray, Format]:
    """Bit-exact model for any ``L = R^S`` (``R = 4``), natural output order, forward.

    The recursive decimation-in-frequency the library implements::

        A_q[m]      = sum_p x[m + p*(L/R)] * W_R^{p q}          # one radix-R butterfly per m
        X[q + R u]  = (L/R)-point DFT over m of ( A_q[m] * W_L^{m q} )

    Written iteratively over stages so each stage's declared formats can be applied in order.
    ``L=16`` is the two-stage case of this and gives identical results to :func:`fft16`.

    **Scope, and why there are two entry points:**

    ==================  ==========================  ==============================
    function            lengths                     scaling modes
    ==================  ==========================  ==============================
    :func:`fft16`       ``L = 16`` only             all three, validated
    :func:`fft_general` any ``L = 4^S``             ``NO_SCALING`` only
    ==================  ==========================  ==============================

    The split is not tidiness: the per-stage narrowing rule this function applies was measured
    for ``NO_SCALING``, and the other two modes demonstrably break it.  Rather than return a
    plausible-looking wrong answer, non-default modes raise.
    """
    if mode != NO_SCALING:
        raise NotImplementedError(
            f"fft_general models {NO_SCALING} only; got {mode}.  The inter-stage and final "
            "narrowing rules were measured for NO_SCALING at L=16/64/1024 and do NOT hold for "
            "the other modes -- SCALE keeps the width fixed, so narrowing by a bit per stage is "
            "wrong, and neither SCALE nor GROW_TO_MAX_WIDTH casts at the output.  Use fft16 for "
            "all three modes at L=16; extending here needs SCALE/GROW goldens at L>16 first "
            "(cpp/dump_modes.cpp handles any configuration).")
    n_stages = _log(length, R)
    fmts = stage_formats(in_w, in_i, n_stages, mode)
    ftw = exp_table_format(tw_w, tw_i)
    tw_r, tw_i_ = twiddle_stored(length, tw_w, tw_i)

    # Values live in a flat array indexed by output position as it is progressively resolved.
    cur_re = [np.array([v]) for v in x_re]
    cur_im = [np.array([v]) for v in x_im]

    # `blocks` are the independent sub-transforms at this level; each is a list of indices into
    # cur_*, in the order the sub-transform sees them.  Stage 1 has one block of the whole array.
    blocks = [list(range(length))]
    for s in range(n_stages):
        f_in, _ = fmts[s]
        sub_len = len(blocks[0])
        m_count = sub_len // R
        # ONE full-L table for every stage, with the index scaled -- `index = n * p_k` in
        # hls_ssr_fft.hpp:107 is an ap_uint<log2(t_L)>, i.e. always the L-length phase space.
        # With a DIRECT table W_L^(mq*L/sub) == W_sub^(mq) and the choice would not matter; with
        # the quarter-wave reconstruction it does, because the two land on different LUT indices
        # and different axis-saturation cases.
        tw_scale = length // sub_len
        new_blocks = []
        for blk in blocks:
            rotated: dict = {}
            for m in range(m_count):
                vr = [cur_re[blk[m + p * m_count]] for p in range(R)]
                vi = [cur_im[blk[m + p * m_count]] for p in range(R)]
                orr, oii, g = _dft4(vr, vi, f_in, s == 0, mode, tw_w, tw_i)
                for q in range(R):
                    if s == n_stages - 1:
                        rotated[(q, m)] = (orr[q], oii[q])
                    else:
                        f_next = fmts[s + 1][0]
                        idx = (m * q * tw_scale) % length
                        # The stage output is narrowed to the NEXT stage's format BEFORE the
                        # rotation, and the multiply then runs with that narrow type as its first
                        # operand.  Narrowing inside the multiply instead -- the natural reading,
                        # since complexMultiply takes a product type -- leaves 14 of 64 stage-3
                        # inputs off by an LSB at L=64.  Measured, not deduced.
                        a, b, op1 = orr[q], oii[q], g
                        if (g.W, g.int_bits) != (f_next.W, f_next.int_bits):
                            a = fp.quantize(a, g, f_next)
                            b = fp.quantize(b, g, f_next)
                            op1 = f_next
                        rotated[(q, m)] = complex_multiply(
                            a, b, op1,
                            np.array([tw_r[idx]]), np.array([tw_i_[idx]]), ftw, f_next)
            # X[q + R*u] -- element q of the butterfly starts the sub-transform for residue q
            for q in range(R):
                idxs = [blk[q + R * u] for u in range(m_count)]
                for m in range(m_count):
                    cur_re[idxs[m]], cur_im[idxs[m]] = rotated[(q, m)]
                new_blocks.append(idxs)
        blocks = new_blocks

    _, g_last = fmts[-1]
    fout = _f(g_last.W - 1, g_last.int_bits) if n_stages >= 2 else g_last
    re = np.array([int(v[0]) for v in cur_re], dtype=np.int64)
    im = np.array([int(v[0]) for v in cur_im], dtype=np.int64)
    if fout == g_last:
        return re, im, fout
    return fp.quantize(re, g_last, fout), fp.quantize(im, g_last, fout), fout
