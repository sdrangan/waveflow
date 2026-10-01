"""Waveflow ``HwModule``s for the Vitis L1 blocks — S1 of ``plans/vitis_l1_hwmodule.md``.

The models in this package prove that Python can reproduce a Vitis L1 block's output **bits**.
What a Waveflow user instantiates is an ``HwModule``: the block's interface, simulating through
that model, and (from S2) synthesizing into a design that calls the vendor library.

**No arithmetic lives in this file.**  Bits come from :mod:`waveflow.vitis_l1.fft`, which is
bit-exact against the vendor library.  If a multiply appears below, something has gone wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.vitis_l1 import fft as fft_model


class ScalingMode(IntEnum):
    """``xf::dsp::fft::scaling_mode_enum`` (``hls_ssr_fft_enums.hpp``), in **declaration** order.

    ``GROW_TO_MAX_WIDTH`` is 1 and ``SCALE`` is 2 — the vendor's own doc comment a few lines above
    the enum lists them the other way round.  Transcribing the comment instead of the declaration
    silently selects the wrong mode, and csynth accepts it.
    """

    NO_SCALING = 0
    GROW_TO_MAX_WIDTH = 1
    SCALE = 2


class OutputOrder(IntEnum):
    """``xf::dsp::fft::fft_output_order_enum``."""

    NATURAL = 0
    DIGIT_REVERSED_TRANSPOSED = 1


#: Our enum -> the model's mode token.  The model spells modes as strings.
_MODE_TO_MODEL = {
    ScalingMode.NO_SCALING: fft_model.NO_SCALING,
    ScalingMode.GROW_TO_MAX_WIDTH: fft_model.GROW_TO_MAX_WIDTH,
    ScalingMode.SCALE: fft_model.SCALE,
}


def _n_stages(length: int, radix: int) -> int:
    """``S`` such that ``length == radix**S``, else raise.

    Inlined rather than importing ``fft._log``: that name is private, and a cross-module
    reach-through would make this file depend on it staying put.  Four lines is cheaper than the
    coupling.
    """
    s, v = 0, 1
    while v < length:
        v *= radix
        s += 1
    if v != length:
        raise ValueError(f"L={length} is not a power of R={radix}")
    return s


def _pack_complex(re: np.ndarray, im: np.ndarray, width: int) -> np.ndarray:
    """Two stored integers per word: **real in the low** ``width`` bits, imaginary above.

    This mirrors ``std::complex<ap_fixed<W, I>>``, whose real part is the first member and so the
    low half of the packed stream word.  It is the pysim's convention, and S2/S3 are what *pin* it
    against the hardware — until then it is a documented choice, not a measured fact.
    """
    mask = (1 << width) - 1
    lo = (np.asarray(re, dtype=np.int64) & mask).astype(np.uint64)
    hi = (np.asarray(im, dtype=np.int64) & mask).astype(np.uint64)
    return (hi << np.uint64(width)) | lo


def _unpack_complex(words: np.ndarray, width: int) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of :func:`_pack_complex`, sign-extending both halves back to signed stored ints."""
    mask = np.uint64((1 << width) - 1)
    u = np.asarray(words, dtype=np.uint64).reshape(-1)
    re = (u & mask).astype(np.int64)
    im = ((u >> np.uint64(width)) & mask).astype(np.int64)
    sign, span = 1 << (width - 1), 1 << width
    return (np.where(re & sign, re - span, re), np.where(im & sign, im - span, im))


@dataclass
class VitisFft(FreeRunMod):
    """AMD Vitis DSP L1 SSR FFT, fixed point — ``L`` points across ``R`` lanes each side.

    ``R`` is the butterfly radix, the SSR factor **and** the port count, because the DUT is::

        void fft_top(hls::stream<T_in> p_in[R], hls::stream<T_out> p_out[R]);

    so this is an ``R``-wide AXI-Stream port group, not one port carrying an ``R``-element word.
    The precedent is :class:`~waveflow.hw.rfdc.Rfdc`'s per-channel ports.

    Sample ``n`` travels on port ``n % R`` at time ``n // R``, both sides — the layout the goldens
    record as ``stream_layout``.  Output ordering is natural, so ``X[k]`` leaves on port ``k % R``.

    A :class:`~waveflow.hw.hw_freerun.FreeRunMod`: the block streams continuously rather than
    being host-launched.  One :meth:`run_iter` firing is one transform.

    Parameters the model does not cover are **refused**, not approximated — a confident wrong
    answer is the one outcome worth engineering against here.
    """

    cpp_kernel_name: ClassVar[str | None] = "vitis_fft"
    cpp_namespace: ClassVar[str | None] = "vitis_fft_impl"

    # -- build-time; each is a C++ template argument, so distinct values are distinct artifacts --
    L: HwParam[int] = 1024      # ssr_fft_param_struct::N — there is no runtime N
    R: HwParam[int] = 4         # radix AND SSR factor AND ports per side
    in_w: HwParam[int] = 16     # input ap_fixed<W, I>
    in_i: HwParam[int] = 2
    tw_w: HwParam[int] = 18     # twiddle_table_word_length
    tw_i: HwParam[int] = 2      # twiddle_table_intger_part_length (the vendor's spelling)

    # Build-time too, but NOT HwParam: ``HwModule.__post_init__`` rewrites every HwParam value as
    # ``HwParamValue(int(value))``, which would flatten an enum member to a bare int and lose the
    # name ``run_iter`` needs.  They remain template arguments — ``kernel_task`` casts explicitly.
    # Same mechanical constraint that made ``Rfdc.word`` a plain field.
    scaling_mode: ScalingMode = ScalingMode.NO_SCALING
    output_order: OutputOrder = OutputOrder.NATURAL

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    # -- S6: timing.  Not HwParams — they change no artifact, only what the pysim predicts. -----
    #
    # **Opt-in, and deliberately without defaults.**  A pipeline exists to decouple latency from
    # throughput, so a model carrying one number is wrong the moment the block sits in a chain —
    # which is the only reason to build it.  But a number nobody measured is worse than no number,
    # and these have to come from a csynth report or an RTL run (see ``cycles_seed`` and S3 of
    # ``plans/vitis_l1_hwmodule.md``).  Leave them unset and the module is untimed: bits only,
    # exactly as S1 behaved.
    latency_cycles: int | None = None   # first input word to first output word
    ii_cycles: int | None = None        # cycles between successive frame *starts*
    max_inflight: int | None = None     # queue capacity; defaults to ceil(latency / II)

    def __post_init__(self) -> None:
        super().__post_init__()
        L, R = int(self.L), int(self.R)

        if R != 4:
            raise NotImplementedError(
                f"{type(self).__name__}: only R=4 is modelled (got R={R}). Radix 2/8/16 change "
                f"the butterfly matrix and the tree depth; see tests/vitis_l1/fft/PLAN.md.")
        self.n_stages = _n_stages(L, R)         # raises unless L == R**S
        if self.output_order is not OutputOrder.NATURAL:
            raise NotImplementedError(
                f"{type(self).__name__}: only SSR_FFT_NATURAL is modelled; every golden and gate "
                f"pins that ordering (got {self.output_order!r}).")
        if L != 16 and self.scaling_mode is not ScalingMode.NO_SCALING:
            raise NotImplementedError(
                f"{type(self).__name__}: all three scaling modes are validated at L=16 only; "
                f"beyond that the model covers NO_SCALING (got L={L}, {self.scaling_mode!r}).")

        # The output type is NOT the input type: it widens with L, by the library's own
        # OUTPUT_WL = in_W + log2(L) + 1.  Derive it from what the model actually produced, so
        # changing L cannot leave the port at a stale width.  Deliberately not
        # ``stage_formats(...)[-1][1]``: that is the last stage's tree output, one bit wider than
        # the port, because the library applies a final narrowing cast (22 vs 21 at L=16).
        zero = np.zeros(L, dtype=np.int64)
        *_, self.out_fmt = self._transform(zero, zero)

        in_bw = 2 * int(self.in_w)              # re and im, packed per word
        out_bw = 2 * int(self.out_fmt.W)

        self.s_in = [StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in_{i}",
                                   bitwidth=in_bw, has_tlast=True) for i in range(R)]
        self.m_out = [StreamIFMaster(sim=self.sim, name=f"{self.name}_m_out_{i}",
                                     bitwidth=out_bw, has_tlast=True) for i in range(R)]
        # Indexed attributes as well as lists: KernelTask.signature and BfmModel.ports name
        # endpoints by attribute name, and ``s_in[0]`` is not one.  Two views, one set of objects.
        for i, ep in enumerate(self.s_in):
            setattr(self, f"s_in_{i}", ep)
        for i, ep in enumerate(self.m_out):
            setattr(self, f"m_out_{i}", ep)
        for ep in (*self.s_in, *self.m_out):
            self.add_endpoint(ep)

        self._setup_timing()

    # -- S6: latency and II, as two processes ----------------------------------------------------
    def _setup_timing(self) -> None:
        """Validate the timing pair and size the in-flight queue.

        ``latency_cycles`` and ``ii_cycles`` are **one pair**: with only a latency the module
        serializes (II collapses into latency), and with only an II it emits instantly.  Either
        alone is a plausible-looking wrong model, so neither is accepted alone.
        """
        given = [n for n in ("latency_cycles", "ii_cycles") if getattr(self, n) is not None]
        self._timed = len(given) == 2
        if len(given) == 1:
            raise ValueError(
                f"{type(self).__name__}: {given[0]} was given without the other. latency_cycles "
                f"and ii_cycles are one pair — a pipeline's whole point is that they differ, and "
                f"one of them alone models something the hardware does not do. Give both, or "
                f"neither for an untimed (bits-only) module.")
        if not self._timed:
            if self.max_inflight is not None:
                raise ValueError(
                    f"{type(self).__name__}: max_inflight only means something once "
                    f"latency_cycles and ii_cycles are set.")
            return

        lat, ii = int(self.latency_cycles), int(self.ii_cycles)
        if lat < 0 or ii <= 0:
            raise ValueError(
                f"{type(self).__name__}: need latency_cycles >= 0 and ii_cycles > 0, "
                f"got {lat} and {ii}.")

        # Frames in flight ~ ceil(latency / II).  This bound is what makes back-pressure correct:
        # unbounded, the module would accept frames forever while its consumer is blocked, which no
        # hardware does.  It is also what keeps this on the right side of the recorded
        # free-running-chain deadlock — a finite queue plus explicit ready-times is the *paced*
        # form, and the bound is the pacing.  Never float("inf").
        cap = -(-lat // ii) if self.max_inflight is None else int(self.max_inflight)
        if cap < 1:
            raise ValueError(
                f"{type(self).__name__}: max_inflight must be >= 1, got {cap}.")
        self.max_inflight = cap
        self._inflight = self.transaction_queue(capacity=cap)

    @staticmethod
    def cycles_seed(length: int, radix: int = 4) -> dict[str, int]:
        """The plan's **unfalsified** starting estimate: ``II ~ L/R``, latency unknown.

        Offered as a seed for a csynth-backed value, not as a prediction. Measured co-simulation of
        the array-port DUT in ``tests/vitis_l1/fft/verifyFFT1024`` showed an interval far above
        ``L/R`` (that top wraps the core in ``ap_memory`` ports, so it is not the streaming form) —
        which is exactly why these are not defaults. S3 is what replaces them with measurements.
        """
        return {"ii_cycles": length // radix}

    # -- the model call, in one place ------------------------------------------------------------
    def _transform(self, x_re: np.ndarray, x_im: np.ndarray):
        """Dispatch to the entry point whose scope covers this configuration.

        ``fft16`` validates all three scaling modes at ``L=16``; ``fft_general`` covers any
        ``L = 4^S`` for ``NO_SCALING`` and raises for the others.  Calling ``fft_general``
        unconditionally would therefore fail for a perfectly supported case (``L=16``, ``SCALE``).
        """
        mode = _MODE_TO_MODEL[self.scaling_mode]
        args = (int(self.in_w), int(self.in_i), int(self.tw_w), int(self.tw_i))
        if int(self.L) == 16:
            return fft_model.fft16(x_re, x_im, *args, mode=mode)
        return fft_model.fft_general(x_re, x_im, int(self.L), *args, mode=mode)

    def kernel_task(self) -> KernelTask:
        """Hand the vendor call over as the task body — S2.

        Waveflow will not extract an ``xf::dsp::fft::fft<>`` instantiation from a Python body, and
        should not try; ``kernel_task`` is the documented hook for "the body is C++ someone else
        wrote".  The header it names is copied, not generated, and arrives in S2.
        """
        R = int(self.R)
        return KernelTask(
            "vitis_fft_task", "vitis_fft_task.h",
            tuple([f"s_in_{i}" for i in range(R)] + [f"m_out_{i}" for i in range(R)]),
            template_args=(int(self.L), R, int(self.in_w), int(self.in_i),
                           int(self.tw_w), int(self.tw_i),
                           int(self.scaling_mode), int(self.output_order),
                           # The derived output width, passed so the C++ can static_assert it
                           # against the vendor's own ssr_fft_output_type.  That turns "my
                           # derivation matches OUTPUT_WL" into a compile-time check: a wrong
                           # width cannot reach synthesis.
                           int(self.out_fmt.W)))

    # -- pysim ----------------------------------------------------------------------------------
    def _get_frame(self, ep, n_words: int):
        """Read exactly *n_words* from *ep*, which ``get`` is free to deliver in pieces."""
        parts, have = [], 0
        while have < n_words:
            got = np.asarray((yield from ep.get(nwords_max=n_words - have))).reshape(-1)
            if got.size == 0:
                raise RuntimeError(
                    f"{self.name}: {ep.name} returned no words while {n_words - have} of "
                    f"{n_words} remain — the producer stopped mid-frame.")
            parts.append(got)
            have += got.size
        return np.concatenate(parts)

    # The R lanes are **parallel ports**, so they must move concurrently.  Transferring them one
    # after another would make a frame cost R x (L/R) = L cycles instead of L/R, which silently
    # caps throughput at R times worse than the hardware and makes any II below L unobservable.
    # Measured while building S6: with sequential lanes, an II of 8 showed up as 16.
    def _read_lanes(self, per_lane: int):
        """Read one frame's words from every input lane at once; returns them lane-ordered."""
        got: dict[int, np.ndarray] = {}

        def lane(j, ep):
            got[j] = yield from self._get_frame(ep, per_lane)

        yield self.env.all_of([self.env.process(lane(j, ep))
                               for j, ep in enumerate(self.s_in)])
        return [got[j] for j in range(len(self.s_in))]

    def _write_lanes(self, lanes: list[np.ndarray]):
        """Write one frame's words to every output lane at once."""
        def lane(ep, words):
            yield from ep.write(words)

        yield self.env.all_of([self.env.process(lane(ep, w))
                               for ep, w in zip(self.m_out, lanes)])

    def run_iter(self):
        """One firing = one transform.  The pysim golden; no arithmetic of its own."""
        L, R = int(self.L), int(self.R)
        per_lane = L // R
        x_re = np.zeros(L, dtype=np.int64)
        x_im = np.zeros(L, dtype=np.int64)

        # Sample n arrives on port n % R at time n // R, so lane j holds n = j, j+R, j+2R, ...
        in_lanes = yield from self._read_lanes(per_lane)
        for j, words in enumerate(in_lanes):
            x_re[j::R], x_im[j::R] = _unpack_complex(words, int(self.in_w))

        y_re, y_im, _ = self._transform(x_re, x_im)

        out_w = int(self.out_fmt.W)
        out_lanes = [_pack_complex(y_re[j::R], y_im[j::R], out_w) for j in range(R)]

        if not self._timed:
            yield from self._write_lanes(out_lanes)
            return

        # Timed: the bits are computed ONCE, here, and the emit side only releases them.  Intake
        # stays in run_iter because `_run_iter_forever` is what populates `firing_records` and
        # drives `timed_delay`, so the calibration path keeps working unchanged.
        period = self.clk.period
        yield self._inflight.put((out_lanes, self.now + period * int(self.latency_cycles)))
        # Pace the II.  `timed_delay` returns 0.0 with no model attached and the predicted delay
        # with one, so adding it is safe unconditionally and keeps calibration live.
        yield self.timeout(period * int(self.ii_cycles)
                           + self.timed_delay({"L": L, "R": R, "n_stages": int(self.n_stages)}))

    def run_proc(self):
        """Leaf behaviour, plus the emit process when timed.

        A single sequential ``run_iter`` **cannot** express latency and II at once: its loop is
        read-frame → delay → write-frame, so frame *k+1* is not accepted until frame *k* has been
        written, back-to-back jobs serialize, and II collapses into latency.  That is wrong exactly
        where it matters — a host issuing frames back to back.  Hence two processes with a bounded
        queue between them, following :meth:`~waveflow.hw.rfdc.Rfdc.run_proc`.

        Two pysim processes against one C++ task is not a divergence: ``kernel_task`` is the
        realization hook and the generator never extracts ``run_iter``, so the pysim's process
        structure is free. These two are the pysim expressing what Vitis implements with
        ``#pragma HLS DATAFLOW`` inside a single call.
        """
        if self._timed:
            self.process(self._emit_proc())
        return super().run_proc()

    def _emit_proc(self):
        """Release each computed frame at its ready time — this is where *latency* is observable."""
        while True:
            out_lanes, t_ready = yield self._inflight.get()
            if self.now < t_ready:
                yield self.timeout(t_ready - self.now)
            yield from self._write_lanes(out_lanes)
