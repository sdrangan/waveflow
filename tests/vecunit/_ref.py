"""An independent reference for the vector unit's arithmetic: exact rationals, and a quantizer written
from the definitions (floor or round half up, then wrap or saturate), sharing no code with
``fixputils``."""

from __future__ import annotations

import math
from fractions import Fraction

from waveflow.utils.fixputils import Format, OMode, QMode


def value(stored: int, fmt: Format) -> Fraction:
    """The real value a stored integer stands for."""
    return Fraction(int(stored), 1) / Fraction(2) ** fmt.frac_bits


def quantize(x: Fraction, fmt: Format) -> int:
    """``x`` in ``fmt``: the nearest-below (``AP_TRN``) or the nearest, ties up (``AP_RND``), stored
    integer, then wrapped or clipped to ``W`` bits."""
    scaled = x * Fraction(2) ** fmt.frac_bits
    v = math.floor(scaled + Fraction(1, 2)) if fmt.q_mode == QMode.AP_RND else math.floor(scaled)
    lo, hi = -(1 << (fmt.W - 1)), (1 << (fmt.W - 1)) - 1
    if fmt.o_mode == OMode.AP_SAT:
        return min(max(v, lo), hi)
    return ((v - lo) % (1 << fmt.W)) + lo


def divide(a: Fraction, fa: Format, b: Fraction, g_div: int) -> Fraction:
    """``fixputils.div`` on the dividend widened by ``g_div`` fraction bits: the quotient truncated
    toward zero at the dividend's widened fraction bits; 0 for a zero divisor."""
    if b == 0:
        return Fraction(0)
    f = fa.frac_bits + g_div
    q = a / b * Fraction(2) ** f
    t = math.floor(q) if q >= 0 else math.ceil(q)
    return Fraction(t) / Fraction(2) ** f
