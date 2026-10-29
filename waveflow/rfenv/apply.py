"""apply.py -- a :class:`~waveflow.rfenv.paths.PathSet` applied to complex-baseband samples, block by block.

**What is computed** (design note, ``plans/sionna_rt/sionna_rt.md`` §14, items 4 and A1)::

    y_ref[r](n) = sum_t sum_p  a_p * exp(j 2 pi f_D,p n / f_s) * x[t](n / f_s - tau_p)
    y[r](n)     = y_ref[r](n - D)          D = filt_len / 2, the declared group delay

``a`` is the generator's coefficient unchanged (carrier phase included), ``tau`` the path's delay
(absolute, or relative to the edge's earliest valid path), ``f_D`` its Doppler shift, and ``n`` the
**absolute** sample index on the edge's grid, so time zero is the run's first sample.

**Why the extra delay D.**  A delay that is not a whole number of samples is realized by a windowed
sinc interpolator, which is two-sided: x(m - mu) needs samples on both sides of m.  Adding the same
``filt_len / 2`` samples to every path makes the filter use only past and present input, so the node
is causal: one block in, one block out, whatever ``blksize`` is, and nothing about the edge's
metronome changes.  The cost is a declared, constant latency (:attr:`ChannelApplier.group_delay_samp`)
that a loop adds to its own -- the way ``RfSampDelay.blk_latency`` is declared rather than hidden.

**Why the output does not depend on blksize.**  The interpolator reads a history buffer that carries
the last ``floor(max tau * f_s + D) + D`` input samples across block boundaries -- exactly what the
latest path's interpolator reaches back to, and at most ``ceil(max tau * f_s) + filt_len`` -- and the
Doppler phase is computed from the absolute sample index, not from a per-block counter.  Feeding the same
samples in blocks of 1 or of 1024 gives the same output (AC3); :func:`reference_apply` evaluates the
same formula on a whole signal at once, by a different code path, to show it.

**Dropped blocks.**  :meth:`ChannelApplier.process` takes each block's absolute start index.  A gap
-- an input block that never arrived -- is zero-filled in the history and counted
(:attr:`ChannelApplier.dropped_samples`), so the grid, the Doppler phase and the delays stay aligned;
an index that goes backwards is an error.

:func:`taps` is the other view of the same channel: Sionna's documented discrete taps (sinc pulse
shaping, absolute delays), the reference the ray-traced channel is checked against (AC5, AC7).
"""

from __future__ import annotations

import math

import numpy as np

from waveflow.rfenv.paths import PathSet

#: Default interpolator length and Kaiser beta: about -67 dB worst case for |f| <= 0.4 f_s (M1 review).
DEFAULT_FILT_LEN = 64
DEFAULT_BETA = 6.0

#: The two ways a channel's delays can be applied (design note, item 3).
DELAY_MODES = ("absolute", "relative")


def fractional_delay_kernel(mu: float, filt_len: int, beta: float) -> np.ndarray:
    """Kaiser-windowed sinc taps ``c[j]`` for a delay of ``mu`` in [0, 1) samples.

    Index ``j`` runs from ``-filt_len/2 + 1`` to ``filt_len/2`` (array position ``j + filt_len/2 - 1``),
    and the interpolated value is ``sum_j c[j] * x[m - j]`` for x(m - mu).  ``mu = 0`` gives a unit
    impulse at ``j = 0``: a whole-sample delay passes samples through untouched.
    """
    half = filt_len // 2
    if mu == 0.0:  # exact: np.sinc at a nonzero integer is ~1e-17, not 0
        c = np.zeros(filt_len)
        c[half - 1] = 1.0
        return c
    u = np.arange(-half + 1, half + 1, dtype=np.float64) - mu
    window = np.i0(beta * np.sqrt(np.clip(1.0 - (2.0 * u / filt_len) ** 2, 0.0, None)))
    return np.sinc(u) * window / np.i0(beta)


class ChannelApplier:
    """Apply a :class:`PathSet` to blocks of samples, keeping the state that spans block boundaries.

    ``process(block, n0)`` takes ``(n_tx_ch, B)`` complex samples whose first sample has absolute
    index ``n0`` and returns the ``(n_rx_ch, B)`` output for the same indices.  Successive calls must
    advance ``n0``; a jump forward is a dropped stretch of input, zero-filled and counted.
    """

    def __init__(
        self,
        pathset: PathSet,
        samp_rate: float,
        *,
        filt_len: int = DEFAULT_FILT_LEN,
        beta: float = DEFAULT_BETA,
        delay_mode: str = "absolute",
        start_n0: int | None = None,
    ) -> None:
        if samp_rate <= 0:
            raise ValueError(f"samp_rate must be positive, got {samp_rate}")
        if filt_len < 2 or filt_len % 2:
            raise ValueError(f"filt_len must be even and at least 2, got {filt_len}")
        if delay_mode not in DELAY_MODES:
            raise ValueError(
                f"delay_mode must be one of {DELAY_MODES}, got {delay_mode!r}"
            )
        self.pathset = pathset
        self.samp_rate = float(samp_rate)
        self.filt_len = int(filt_len)
        self.beta = float(beta)
        self.delay_mode = delay_mode
        #: The delay subtracted from every path in ``relative`` mode: the edge's earliest valid path.
        self.bulk_delay_s = pathset.min_delay() if delay_mode == "relative" else 0.0
        self._paths = _compile(
            pathset, self.samp_rate, self.filt_len, self.beta, self.bulk_delay_s
        )
        max_k = max((p[2] for p in self._paths), default=self.group_delay_samp)
        #: Input samples kept across blocks: exactly as far back as the latest path's interpolator
        #: reads (x[n0 - K - j] with j up to D, so K + D samples before the block).
        self.history_len = int(max_k) + self.group_delay_samp
        self._hist = np.zeros((pathset.n_tx_ch, self.history_len), dtype=np.complex128)
        #: Where the input is expected to start.  ``None`` takes the first block as the start; an
        #: index (a node passes 0) makes a first block that arrives later a gap, zero-filled and
        #: counted like any other dropped stretch.
        self._next_n0: int | None = None if start_n0 is None else int(start_n0)
        #: Input samples zero-filled because their block never arrived.
        self.dropped_samples = 0

    @property
    def group_delay_samp(self) -> int:
        """D: the constant delay the interpolator adds to every path, in samples."""
        return self.filt_len // 2

    @property
    def latency_samp(self) -> int:
        """What a loop subtracts for this node: D, in either delay mode (design note, A1)."""
        return self.group_delay_samp

    def process(self, block: np.ndarray, n0: int) -> np.ndarray:
        """Return the output for one input block that starts at absolute sample ``n0``."""
        x = np.asarray(block, dtype=np.complex128)
        if x.ndim != 2 or x.shape[0] != self.pathset.n_tx_ch:
            raise ValueError(
                f"block must be (n_tx_ch={self.pathset.n_tx_ch}, blksize); got {x.shape}"
            )
        n0 = int(n0)
        if self._next_n0 is not None and n0 != self._next_n0:
            if n0 < self._next_n0:
                raise ValueError(
                    f"block starts at sample {n0}, before the end of the previous one "
                    f"({self._next_n0}); blocks must arrive in order"
                )
            self._skip(n0 - self._next_n0)
        n_blk = x.shape[1]
        buf = np.concatenate([self._hist, x], axis=1)
        y = _apply(
            self._paths,
            buf,
            self.history_len,
            n0,
            n_blk,
            self.pathset.n_rx_ch,
            self.samp_rate,
            self.group_delay_samp,
        )
        self._hist = buf[:, buf.shape[1] - self.history_len :]
        self._next_n0 = n0 + n_blk
        return y

    def _skip(self, n_gap: int) -> None:
        """Zero-fill ``n_gap`` input samples that never arrived."""
        self.dropped_samples += n_gap
        keep = max(self.history_len - n_gap, 0)
        zeros = np.zeros(
            (self._hist.shape[0], min(n_gap, self.history_len)), dtype=np.complex128
        )
        self._hist = np.concatenate(
            [self._hist[:, self._hist.shape[1] - keep :], zeros], axis=1
        )


#: A delay within this many samples of a whole number is that whole number: tau * f_s in floating
#: point lands a few ulps off an integer (k/f_s * f_s = k - 6e-14), which would turn the exact
#: impulse of :func:`fractional_delay_kernel` into a sinc of almost-zero fraction.
SNAP_SAMPLES = 1e-9


def split_delay(tau_s: float, fs: float, half: int) -> tuple[int, float]:
    """``(K, mu)`` with ``tau_s * fs + half = K + mu``, ``mu`` in [0, 1), snapped to whole samples."""
    s = tau_s * fs + half
    r = round(s)
    if abs(s - r) < SNAP_SAMPLES:
        return int(r), 0.0
    k = math.floor(s)
    return k, s - k


def _compile(pathset, fs, filt_len, beta, bulk):
    """Per valid path: (rx, tx, K, taps, a, f_D), with K + j the whole-sample part of the delay."""
    half = filt_len // 2
    out = []
    for r, t, p in zip(*np.nonzero(pathset.valid), strict=True):
        k, mu = split_delay(float(pathset.tau[r, t, p]) - bulk, fs, half)
        out.append(
            (
                int(r),
                int(t),
                k,
                fractional_delay_kernel(mu, filt_len, beta),
                complex(pathset.a[r, t, p]),
                float(pathset.doppler[r, t, p]),
            )
        )
    return out


def _apply(paths, buf, hist_len, n0, n_blk, n_rx, fs, d):
    """The block output from ``buf`` = history + block, ``buf[:, hist_len]`` being sample ``n0``."""
    y = np.zeros((n_rx, n_blk), dtype=np.complex128)
    if not paths:
        return y
    n = np.arange(n0, n0 + n_blk, dtype=np.float64)
    half = len(paths[0][3]) // 2
    for r, t, k, c, a, fd in paths:
        # filtered[q] = sum_j c[j] * x[n0 + q - k - j],  j = -half+1 .. half
        start = hist_len - k - half
        seg = buf[t, start : start + n_blk + len(c) - 1]
        filtered = np.convolve(seg, c, mode="valid")
        y[r] += a * filtered * (np.exp(2j * np.pi * fd * (n - d) / fs) if fd else 1.0)
    return y


def reference_apply(
    pathset: PathSet,
    x: np.ndarray,
    samp_rate: float,
    *,
    filt_len: int = DEFAULT_FILT_LEN,
    beta: float = DEFAULT_BETA,
    delay_mode: str = "absolute",
    n0: int = 0,
) -> np.ndarray:
    """The same output as :class:`ChannelApplier`, evaluated on a whole signal at once.

    A deliberately different code path -- every output sample's interpolator sum is gathered by
    explicit indexing, with zeros before ``n0`` -- so agreement with the block applier tests the
    history handling rather than repeating it.  Memory grows with ``len(x) * filt_len``; use it on
    test-sized signals.
    """
    x = np.asarray(x, dtype=np.complex128)
    fs = float(samp_rate)
    half = filt_len // 2
    bulk = pathset.min_delay() if delay_mode == "relative" else 0.0
    n_out = x.shape[1]
    n = np.arange(n_out)
    j = np.arange(-half + 1, half + 1)
    y = np.zeros((pathset.n_rx_ch, n_out), dtype=np.complex128)
    for r, t, p in zip(*np.nonzero(pathset.valid), strict=True):
        k, mu = split_delay(float(pathset.tau[r, t, p]) - bulk, fs, half)
        c = fractional_delay_kernel(mu, filt_len, beta)
        m = n[:, None] - k - j[None, :]  # input index (relative to n0) of every tap
        taps_in = np.where(m >= 0, x[t, np.clip(m, 0, None)], 0.0)
        phase = np.exp(2j * np.pi * pathset.doppler[r, t, p] * (n0 + n - half) / fs)
        y[r] += pathset.a[r, t, p] * phase * (taps_in @ c)
    return y


def taps(
    pathset: PathSet,
    bandwidth: float,
    l_min: int,
    l_max: int,
    *,
    sampling_frequency: float | None = None,
    num_time_steps: int = 1,
) -> np.ndarray:
    """Sionna's discrete channel taps, from the paths, with absolute delays.

    ``h[r, t, n, l] = sum_p a_p * exp(j 2 pi f_D,p n / f_s) * sinc(l - W tau_p)`` for lags ``l`` from
    ``l_min`` to ``l_max`` and time steps ``n`` at ``f_s`` (``sampling_frequency``, default ``W``) --
    the formula of Sionna RT's ``Paths.taps`` with ``normalize_delays=False`` and ``normalize=False``.
    Shape ``[n_rx_ch, n_tx_ch, num_time_steps, l_max - l_min + 1]``.
    """
    fs = float(bandwidth if sampling_frequency is None else sampling_frequency)
    lags = np.arange(int(l_min), int(l_max) + 1, dtype=np.float64)
    steps = np.arange(int(num_time_steps), dtype=np.float64)
    a = np.where(pathset.valid, pathset.a, 0.0)
    tau = np.where(pathset.valid, pathset.tau, 0.0)
    fd = np.where(pathset.valid, pathset.doppler, 0.0)  # padding may hold anything
    rot = np.exp(2j * np.pi * fd[..., None, :] * steps[:, None] / fs)  # [r,t,n,p]
    kernel = np.sinc(lags[:, None] - float(bandwidth) * tau[..., None, :])  # [r,t,l,p]
    return np.einsum("rtp,rtnp,rtlp->rtnl", a, rot, kernel)
