"""matmul.py — the bit-exact model of the complex fixed-point matrix multiply.

``C = q_c(A·B)`` or ``C = q_c(Aᴴ·B)``: every product and every sum is exact, and the result is
rounded and saturated once, to the format of ``C`` (:func:`~waveflow.utils.fixputils.quantize`).
Integer sums are associative, so any order of the products, and either multiply form, gives the
same values; a hardware implementation is bit-exact iff it keeps every partial sum exact and
rounds once.

The values are the **stored integers** of their formats, as ``(re, im)`` pairs of ``int64``
arrays.  ``A`` has shape ``(..., M, K)`` (``(..., K, M)`` for ``Aᴴ``), ``B`` has shape
``(..., K, N)``, and the leading dimensions broadcast as in :func:`numpy.matmul`.

The multiply forms
------------------
``form=4`` sums ``ar·br − ai·bi`` and ``ar·bi + ai·br``.  ``form=3`` sums the three-multiply
(Gauss) terms of :func:`~waveflow.utils.complexutils.cmult3`: ``k1 = br(ar + ai)``,
``k2 = ar(bi − br)``, ``k3 = ai(br + bi)``, ``re = k1 − k3``, ``im = k1 + k2``.  The values are
equal; ``form`` exists so a model of a datapath can run the order the hardware runs.

``Aᴴ`` negates the imaginary part of ``A`` exactly: the conjugate of ``−2^(W−1)`` is
``+2^(W−1)``, which ``W`` bits cannot hold, so a datapath must widen before it negates.
"""

from __future__ import annotations

import numpy as np

from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format

FORMS = (3, 4)


def acc_format(a: Format, b: Format, K: int) -> Format:
    """The exact format of a sum of ``K`` complex products of ``a`` and ``b`` values.

    It holds ``A·B`` and ``Aᴴ·B`` alike.  Raises ``NotImplementedError`` beyond 64 bits.
    """
    return fx.sum_format(cx.cmult_format(a, b), int(K))


def _check_operand(re, im, fmt: Format, name: str) -> tuple[np.ndarray, np.ndarray]:
    if not fmt.signed:
        raise NotImplementedError(f"{name}: complex multiply needs a signed format")
    re, im = np.asarray(re, dtype=np.int64), np.asarray(im, dtype=np.int64)
    if re.shape != im.shape or re.ndim < 2:
        raise ValueError(
            f"{name}: re and im must be matrices of one shape, got {re.shape} and {im.shape}"
        )
    lo, hi = -(1 << (fmt.W - 1)), (1 << (fmt.W - 1)) - 1
    for part in (re, im):
        if part.size and (part.min() < lo or part.max() > hi):
            raise ValueError(f"{name}: stored values outside the {fmt.W}-bit format")
    return re, im


def matmul_exact(
    ar,
    ai,
    a: Format,
    br,
    bi,
    b: Format,
    *,
    adjoint: bool = False,
    form: int = 4,
) -> tuple[np.ndarray, np.ndarray, Format]:
    """The exact ``A·B`` (or ``Aᴴ·B``) and its format, :func:`acc_format`."""
    if form not in FORMS:
        raise ValueError(f"form must be one of {FORMS}, got {form}")
    ar, ai = _check_operand(ar, ai, a, "A")
    br, bi = _check_operand(br, bi, b, "B")
    if adjoint:
        ar, ai = np.swapaxes(ar, -1, -2), -np.swapaxes(ai, -1, -2)
    K = ar.shape[-1]
    if br.shape[-2] != K:
        raise ValueError(
            f"inner dimensions differ: A gives K = {K}, B has {br.shape[-2]} rows"
        )
    fmt = acc_format(a, b, K)
    # Every partial sum below fits fmt, which is at most 64 bits, so int64 arithmetic is exact:
    # a Gauss term such as br (ar + ai) is as wide as a plain pair ar·br − ai·bi.
    if form == 4:
        re = ar @ br - ai @ bi
        im = ar @ bi + ai @ br
    else:
        k1 = (ar + ai) @ br
        re = k1 - ai @ (br + bi)
        im = k1 + ar @ (bi - br)
    return re, im, fmt


def matmul(
    ar,
    ai,
    a: Format,
    br,
    bi,
    b: Format,
    c: Format,
    *,
    adjoint: bool = False,
    form: int = 4,
) -> tuple[np.ndarray, np.ndarray]:
    """``C = q_c(A·B)``, or ``q_c(Aᴴ·B)`` with ``adjoint``: the stored integers of ``C``."""
    re, im, fmt = matmul_exact(ar, ai, a, br, bi, b, adjoint=adjoint, form=form)
    return fx.quantize(re, fmt, c), fx.quantize(im, fmt, c)
