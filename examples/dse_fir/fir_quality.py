"""fir_quality.py — designing a *quantized* FIR, and scoring it.

Phase M0 of ``plans/mcp_fir.md``.  This file supplies the thing the DSE surface optimizes, because
nothing in the tree computes it today: ``examples/fir_block/fir_block_sim.py`` fabricates
coefficients (``_tap_set``) and asserts the RTL matches its *own* quantized golden.  That is a
conformance test — a design that is bit-exactly a **bad filter** passes it.

Lives in ``examples/dse_fir/`` rather than in ``examples/fir_block/``: the DSE work is a *consumer*
of that design, not part of it.  ``fir_block`` stays a hardware example with its own gates and its own
committed resource corpus, and everything this arc needs from it arrives by import.

This module is the design half:

1. :func:`design_float` — a real-valued prototype meeting a passband/stopband spec at ``ntap`` taps.
2. :func:`design_quantized` — the scale search that maps it into ``ap_fixed<W, I>``.

Optimal filter design is not the point and no claim is made that these are the best taps for the
spec.  The point is a **deterministic, differentiable-in-spirit objective** the agent can move.

The scale search
----------------
Given real taps ``h`` and a target format, the free variable is a gain ``s``: the hardware stores
``Q(s·h)``, and ``s`` decides how much of the format's range the coefficients actually use.  The
objective is scale-invariant misalignment,

.. math::

    J(s) = \\min_a \\frac{\\| a h - Q(s h) \\|^2}{\\| a h \\|^2} = \\frac{1 - R^2}{R^2} = \\tan^2\\theta

where :math:`R^2 = \\langle h, \\hat h\\rangle^2 / (\\|h\\|^2 \\|\\hat h\\|^2)`.  Minimizing ``J`` is
maximizing ``R^2``, which is what :func:`design_quantized` does.

**Why the ``min_a`` and not the simpler** ``\\|s h - Q(s h)\\|^2 / \\|s h\\|^2``: because a pure gain
error is not a design error.  The taps carry an arbitrary gain that the host absorbs when it reads
the output; what a wrong gain must not do is masquerade as a wrong *shape*.  Projecting it out is
what makes the number comparable across scales at all.

**The shape of J, and why a coarse grid suffices.**  At a fixed format the step ``Δ`` is fixed, so the
quantization error is about ``T·Δ²/12`` *regardless of ``s``*, while the signal energy is ``s²‖h‖²``.
So ``J`` falls like ``1/s²`` until taps begin to clip, and then falls off a cliff.  The optimum sits
at the clipping knee.  The grid therefore spans ``s_max/8 … 4·s_max`` about
``s_max = max_repr / max|h|`` and deliberately searches **past** the no-clipping point: one outlier
tap saturating is often cheaper than losing resolution on all ``T``.

Rounding and overflow — a real trap
-----------------------------------
:class:`~waveflow.hw.fixpoint.FixedField` defaults to ``AP_TRN`` / ``AP_WRAP``, Vitis's defaults, and
:func:`~examples.fir_block.fir_block.samp_type` takes them.  Both are wrong *here*:

* ``AP_TRN`` floors, putting a systematic ``-½ LSB`` bias on every coefficient.  A **coherent** tap
  error does not average out across frequency the way white error does, so it costs far more stopband
  than the ``Δ²/12`` analysis above would suggest.
* ``AP_WRAP`` makes "allow a little clipping" catastrophic instead of graceful — a tap just over range
  flips sign.

So :data:`TAP_QMODE` / :data:`TAP_OMODE` override both, and that override is **free of hardware
consequence**: this module emits *already-representable* stored integers, which the hardware's
``AP_TRN`` passes through unchanged.  The rounding mode is a property of how the taps were chosen,
not of the datapath that later multiplies by them.  The datapath's own ``AP_TRN``/``AP_WRAP``
(``FirCompute.filter_block``'s ``quantize``) is untouched and is the evaluation half's problem.

What ``scale`` absorbs
----------------------
A continuous ``scale`` is a **fractional** ``samp_i``: scaling ``h`` by ``2^k`` and moving ``I`` by
``k`` are the same operation, and ``scale`` is not restricted to powers of two.  So the search here
subsumes the coefficient side of ``samp_i`` entirely, and ``samp_i`` survives as a DSE knob only for
the **data path** — input headroom and output overflow.  Do not sweep ``samp_i`` for the taps' sake;
it is already optimized over, continuously.

Linear phase survives
---------------------
``firwin`` and ``remez`` both return a symmetric ``h``, and quantization is element-wise, so equal
coefficients quantize equally and ``Q(s·h)`` is **exactly** symmetric too.  The quantized filter has
exactly linear phase; the only degradation is in magnitude.  That is why the quality metrics may
score the magnitude response alone without waving it through.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from waveflow.hw.fixpoint import FixedField, from_real, to_real
from waveflow.utils.fixputils import OMode, QMode

#: Rounding for the *offline* tap choice.  See the module docstring: not the datapath's mode, and
#: free to differ from it precisely because what leaves here is already representable.
TAP_QMODE = QMode.AP_RND
TAP_OMODE = OMode.AP_SAT

#: Scale grid: decades either side of the no-clipping scale, log-spaced.  200 points is far more than
#: the ``1/s²``-then-cliff shape needs, and the whole search is a few hundred microseconds.
SCALE_LO, SCALE_HI, SCALE_N = 0.125, 4.0, 201


@dataclass(frozen=True)
class FirSpec:
    """A lowpass specification in cycles/sample (``fs = 1``), so nothing carries a sample rate.

    The spec is deliberately *not* an attenuation target.  Attenuation is what ``ntap`` buys and what
    the DSE maximizes — declaring it as an input would fix the answer.
    """

    f_pass: float = 0.20
    f_stop: float = 0.28

    def __post_init__(self) -> None:
        if not 0.0 < self.f_pass < self.f_stop < 0.5:
            raise ValueError(f"require 0 < f_pass < f_stop < 0.5, got {self.f_pass}, {self.f_stop}")

    @property
    def trans_width(self) -> float:
        """Transition width in cycles/sample — the quantity Kaiser's order formula is stated in."""
        return self.f_stop - self.f_pass


@dataclass(frozen=True)
class QuantizedFir:
    """The design step's product: what to load, and how good it is *as a set of numbers*.

    ``r2`` scores the coefficient vector, not the filter.  It is the scale search's objective and a
    useful diagnostic, but it is **not** the DSE metric: tap-domain :math:`L^2` is blind to where in
    frequency the error lands, and stopband attenuation is exactly what tap noise floors.  The
    response-domain metrics are the evaluation half's job.
    """

    h: np.ndarray                 #: the real-valued prototype
    stored: np.ndarray            #: quantized taps as stored integers — what ``LOAD_TAPS`` carries
    taps_real: np.ndarray         #: the same taps as reals, ``stored · 2^-F``
    scale: float                  #: the chosen gain
    r2: float                     #: alignment of ``taps_real`` with ``h``, in [0, 1]
    n_clipped: int                #: coefficients the search let saturate (often the right trade)
    n_zeroed: int                 #: coefficients that quantized to zero — the small-``W`` failure
    method: str                   #: which prototype designer ran

    @property
    def misalignment(self) -> float:
        """``J = (1 - R²)/R²`` — the objective the scale search minimized.  ``inf`` if degenerate."""
        return float("inf") if self.r2 <= 0.0 else (1.0 - self.r2) / self.r2


def tap_format(samp_w: int, samp_i: int) -> type[FixedField]:
    """The format used to *choose* taps: the design's ``(W, I)`` with honest rounding.

    Same width and same binary point as :func:`~examples.fir_block.fir_block.samp_type`, so the
    stored integers are interchangeable — only ``q_mode``/``o_mode`` differ, and those govern the
    offline choice alone.
    """
    return FixedField.specialize(W=int(samp_w), I=int(samp_i), signed=True,
                                 q_mode=TAP_QMODE, o_mode=TAP_OMODE)


def max_representable(samp_w: int, samp_i: int) -> float:
    """The largest real a signed ``ap_fixed<W, I>`` holds: ``(2^(W-1) - 1) · 2^-(W-I)``."""
    return float((2 ** (int(samp_w) - 1) - 1) * 2.0 ** (-(int(samp_w) - int(samp_i))))


# --- 1. the real-valued prototype ---------------------------------------------------------------


def design_float(spec: FirSpec, ntap: int,
                 method: Literal["kaiser", "remez"] = "kaiser") -> np.ndarray:
    """Real taps meeting *spec* at ``ntap`` coefficients.

    **Kaiser is the default, and equiripple is not**, even though ``remez`` gives strictly better
    attenuation for the same order.  Two reasons, both about the DSE loop rather than about filters:
    ``remez`` fails to converge on awkward ``(ntap, spec)`` combinations, and a design step that
    intermittently fails injects non-determinism into the objective — which poisons a benchmark whose
    whole purpose is to be scored.  Kaiser always converges and its attenuation is *smooth and
    monotone* in ``ntap``, which is the response surface the agent is trying to learn.

    The Kaiser ``beta`` is derived from ``ntap`` by inverting the standard order estimate
    ``A ≈ 2.285·Δω·(N-1) + 7.95``, so attenuation rises with ``ntap`` instead of being declared.
    That is the whole DSE story in one line: **``ntap`` buys stopband until the coefficient noise
    floor set by ``samp_w`` caps it**, and the interesting design point is the knee.
    """
    from scipy import signal

    ntap = int(ntap)
    if ntap < 2:
        raise ValueError(f"ntap must be >= 2, got {ntap}")

    if method == "remez":
        # bands in cycles/sample; a converged equiripple design or an exception, never a silent
        # fallback -- a caller asking for remez wants to know it did not happen.
        return np.asarray(signal.remez(ntap, [0.0, spec.f_pass, spec.f_stop, 0.5], [1.0, 0.0],
                                       fs=1.0), dtype=np.float64)

    atten_db = 2.285 * (2.0 * np.pi * spec.trans_width) * (ntap - 1) + 7.95
    beta = float(signal.kaiser_beta(max(atten_db, 0.0)))
    cutoff = 0.5 * (spec.f_pass + spec.f_stop)
    return np.asarray(signal.firwin(ntap, cutoff, window=("kaiser", beta), fs=1.0),
                      dtype=np.float64)


# --- 2. the scale search ------------------------------------------------------------------------


def _r2(h: np.ndarray, hq: np.ndarray) -> float:
    """``⟨h,ĥ⟩² / (‖h‖²‖ĥ‖²)`` — ``cos²`` of the angle between the prototype and its quantization.

    Zero when ``ĥ`` collapses (every tap quantized away at small ``W``) or points the wrong way.
    That is a **result**, not an error: it is what "this width cannot hold this filter" looks like,
    and the DSE surface records it as a row rather than raising.
    """
    den = float(h @ h) * float(hq @ hq)
    if den <= 0.0:
        return 0.0
    num = float(h @ hq)
    return 0.0 if num <= 0.0 else (num * num) / den


def quantize_taps(h: np.ndarray, scale: float, samp_w: int, samp_i: int) -> np.ndarray:
    """``Q(scale·h)`` as **stored integers** — the payload a ``LOAD_TAPS`` command carries.

    Routed through :func:`~waveflow.hw.fixpoint.from_real`, which is the framework quantizer and the
    twin of what ``ap_fixed`` does.  Hand-rolling ``round(x/Δ)`` here would drift from the hardware
    the first time a mode or a width changed, which is the same failure the array serializers exist
    to prevent.
    """
    cls = tap_format(samp_w, samp_i)
    return np.asarray(from_real(np.asarray(h, dtype=np.float64) * float(scale), cls),
                      dtype=np.int64)


def design_quantized(spec: FirSpec, ntap: int, samp_w: int, samp_i: int,
                     method: Literal["kaiser", "remez"] = "kaiser",
                     h: np.ndarray | None = None) -> QuantizedFir:
    """Design at ``ntap`` taps and map into ``ap_fixed<samp_w, samp_i>`` at the best gain.

    *h* may be supplied to skip the prototype design when sweeping widths against one filter — the
    prototype depends only on ``(spec, ntap, method)``, so it is worth caching across ``samp_w``.
    """
    h = design_float(spec, ntap, method) if h is None else np.asarray(h, dtype=np.float64)
    cls = tap_format(samp_w, samp_i)

    peak = float(np.max(np.abs(h)))
    if peak <= 0.0:
        raise ValueError("degenerate prototype: all taps zero")
    s_max = max_representable(samp_w, samp_i) / peak

    best: tuple[float, float, np.ndarray] = (-1.0, s_max, quantize_taps(h, s_max, samp_w, samp_i))
    for s in np.logspace(np.log10(s_max * SCALE_LO), np.log10(s_max * SCALE_HI), SCALE_N):
        stored = quantize_taps(h, float(s), samp_w, samp_i)
        r2 = _r2(h, to_real(_as_fixed_array(stored, cls)))
        if r2 > best[0]:
            best = (r2, float(s), stored)

    r2, scale, stored = best
    taps_real = to_real(_as_fixed_array(stored, cls))
    lo, hi = -max_representable(samp_w, samp_i) - 2.0 ** -(samp_w - samp_i), \
        max_representable(samp_w, samp_i)
    scaled = h * scale
    return QuantizedFir(
        h=h, stored=stored, taps_real=np.asarray(taps_real, dtype=np.float64), scale=scale, r2=r2,
        n_clipped=int(np.count_nonzero((scaled > hi) | (scaled < lo))),
        n_zeroed=int(np.count_nonzero((stored == 0) & (h != 0.0))),
        method=method)


def _as_fixed_array(stored: np.ndarray, cls: type[FixedField]):
    """Wrap stored ints as a ``DataArray[cls]`` so :func:`to_real` applies the right binary point."""
    from waveflow.hw.dataschema import DataArray

    arr = np.asarray(stored, dtype=np.int64).reshape(-1)
    return DataArray.specialize(cls, max_shape=(max(len(arr), 1),))(arr)
