"""demux.py -- ``StreamDemux``: one framed stream out to ``n_out``, a whole frame at a time.

The return half of a shared kernel: each frame goes back to the client it belongs to.  Which output a
frame takes is chosen one of two ways (D3 of ``plans/shared_kernel.md``):

* **by a side stream** (``route="sel"``, the default): one word on ``s_sel`` per frame, the output's index.
  It is what a :class:`~waveflow.streams.arbiter.StreamArbiter`'s ``m_sel`` carries, so the pair
  ``arbiter -> K -> demux`` returns every frame to its input, for any K that keeps frames in order --
  including one that knows nothing of tags (an FFT framed by count);
* **by a field of the frame's first word** (``route="header"``): bits ``sel_lsb`` to ``sel_lsb + sel_bits - 1``
  of the frame's first word, the word itself forwarded with the rest.  No side stream; the frame carries
  its own destination, so whatever produced it had to put one there.

A **frame** is ``frame_bursts`` consecutive TLAST bursts, as the arbiter's.  An index of ``n_out`` or more
names no output: that frame is read and **dropped**, and :attr:`dropped` counts it (the HLS body drops it
too).  The block knows no message format and reports nothing, so a design that cannot lose a frame keeps
its indices in range.

The outputs do not move in lockstep: a full output stalls the block, and with it every other output, until
that output's consumer reads.  Output order per output is the input's, so pysim and RTL record the same
words on every output.

**Word, not burst, and timed on a platform** (``plans/osfb_pysim_fidelity.md``, causes F and H; Stages 8 and
7 of ``plans/stream_dispatch.md`` for the arbiter).  The body reads the index, then the frame's first word,
and copies the frame through as its words arrive; so does the model: it commits on the frame's first word
(``begin_read(pending_ok=True)``) and copies with :func:`~waveflow.streams.arbiter.copy_burst`.  Given
``platform_dir``, a platform its decision cost was calibrated on, each firing first spends the cycles the
RTL does deciding (:mod:`waveflow.streams.timing`); without one it decides at once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave
from waveflow.streams.arbiter import copy_burst
from waveflow.streams.body_step import StreamBodyStep
from waveflow.streams.timing import attach, decide, demux_features, require_points
from waveflow.utils.burst_io import StreamBurst

__all__ = ["StreamDemux", "StreamDemuxStep", "ROUTES"]

#: How a frame's output is chosen.
ROUTES = ("sel", "header")


@dataclass
class StreamDemux(FreeRunMod):
    """One framed input (``s_in``) out to ``n_out`` (``m_out_0`` .. ``m_out_<n_out-1>``), a whole frame at
    a time, to the output ``s_sel`` names (``route="sel"``) or a field of the frame's first word names
    (``route="header"``: bits ``sel_lsb`` .. ``sel_lsb + sel_bits - 1``).

    Build parameters: ``n_out`` (2 to 16), ``bitwidth`` (every port's), ``frame_bursts`` (TLAST bursts a
    frame), and for ``route="header"`` ``sel_lsb`` and ``sel_bits`` (1 to 16).  ``platform_dir``, a
    platform its decision cost was calibrated on, times each firing as the RTL does
    (:mod:`waveflow.streams.timing`); without one the block decides at once."""

    cpp_kernel_name: ClassVar[str | None] = "stream_demux"
    #: Build inputs that are not template parameters: a fresh copy (the harness's DUT) keeps them.
    build_fields: ClassVar[tuple[str, ...]] = ("route", "platform_dir")

    n_out: HwParam[int] = 2
    bitwidth: HwParam[int] = 32
    frame_bursts: HwParam[int] = 1
    sel_lsb: HwParam[int] = 0
    sel_bits: HwParam[int] = 8
    #: Not an ``HwParam`` (those are integers); it picks the body.
    route: str = "sel"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: A calibrated platform (``xczu48dr_250mhz_vitis2025_1``, at its 250 MHz): each firing then spends
    #: the cycles its RTL does.  Not part of the module's identity (``elaborate._CONTEXT_ATTRS``).
    platform_dir: str | Path | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        n, w = int(self.n_out), int(self.bitwidth)
        if not 2 <= n <= 16:
            raise ValueError(f"{self.name}: n_out must be 2 to 16, got {n}")
        if not 1 <= int(self.frame_bursts) <= 255:
            raise ValueError(f"{self.name}: frame_bursts must be 1 to 255, got {self.frame_bursts}")
        if self.route not in ROUTES:
            raise ValueError(f"{self.name}: route must be one of {ROUTES}, got {self.route!r}")
        if self.route == "header":
            lsb, bits = int(self.sel_lsb), int(self.sel_bits)
            if not (1 <= bits <= 16 and 0 <= lsb and lsb + bits <= w):
                raise ValueError(f"{self.name}: the header field (lsb {lsb}, {bits} bits) must lie in a "
                                 f"{w}-bit word and be 1 to 16 bits")
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=w)
        self.add_endpoint(self.s_in)
        if self.route == "sel":
            self.s_sel = FramedStreamIFSlave(name=f"{self.name}_s_sel", sim=self.sim, bitwidth=w)
            self.add_endpoint(self.s_sel)
        self.m_out: list[FramedStreamIFMaster] = []
        for k in range(n):
            ep = FramedStreamIFMaster(name=f"{self.name}_m_out_{k}", sim=self.sim, bitwidth=w)
            setattr(self, f"m_out_{k}", ep)
            self.add_endpoint(ep)
            self.m_out.append(ep)
        #: Frames whose index named no output, read and dropped.
        self.dropped = 0
        if self.platform_dir is not None:
            require_points(self, "demux", [self.firing()])

    def pre_sim(self) -> None:
        """On a calibrated platform, attach the decision model (:func:`waveflow.streams.timing.attach`)."""
        super().pre_sim()
        if self.platform_dir is not None and self.timing_model is None:
            attach(self, "demux", [self.firing()])

    def firing(self) -> dict:
        """A firing's point on the decision model's basis (:mod:`waveflow.streams.timing`)."""
        return demux_features(int(self.n_out), self.route == "header", int(self.bitwidth), int(self.frame_bursts))

    @property
    def ports(self) -> tuple[str, ...]:
        """The body's signature: the inputs, then the outputs in order (the pack last)."""
        ins = ("s_in", "s_sel") if self.route == "sel" else ("s_in",)
        return (*ins, *(f"m_out_{k}" for k in range(int(self.n_out))))

    def kernel_task(self):
        """The framework body: ``stream_demux_task`` (by ``s_sel``) or ``stream_demux_hdr_task`` (by a
        header field), templated on the width, the frame (and the field) and every port's word type."""
        from waveflow.hw.mem_stream import KernelTask

        w, nb = int(self.bitwidth), int(self.frame_bursts)
        if self.route == "sel":
            return KernelTask("stream_demux_task", "stream_demux_task.h", self.ports,
                              template_args=(w, nb), framed=self.ports)
        return KernelTask("stream_demux_hdr_task", "stream_demux_task.h", self.ports,
                          template_args=(w, nb, int(self.sel_lsb), int(self.sel_bits)), framed=self.ports)

    def index_of(self, first_word) -> int:
        """``route="header"``: the output a frame names in its first word.  A word wider than 64 bits
        arrives as its 64-bit chunks, chunk 0 the low bits."""
        return (_value(first_word) >> int(self.sel_lsb)) & ((1 << int(self.sel_bits)) - 1)

    def run_iter(self):
        """One frame, as the body: its index (an ``s_sel`` word, or the first word's field, read as the
        frame's first word arrives), the decision cycles on a calibrated platform, then its bursts to that
        output copied as their words arrive -- or read and dropped, if the index names none."""
        if self.route == "sel":
            sel = np.asarray((yield from self.s_sel.get()), dtype=np.uint64)
            k = _value(sel.reshape(len(sel), -1)[0])
        x = yield from self.s_in.begin_read(pending_ok=True)        # committed on the first word
        if self.route == "header":
            k = self.index_of(np.asarray(x.data)[0])
        yield from decide(self, self.firing())
        nb = int(self.frame_bursts)
        if k >= int(self.n_out):
            yield from self.s_in.end_read(x)
            for _ in range(nb - 1):
                yield from self.s_in.get()
            self.dropped += 1
            return
        w = yield from self.m_out[k].begin(x.data)
        yield from self.s_in.end_read(x)
        yield from self.m_out[k].end(w)
        for _ in range(nb - 1):
            yield from copy_burst(self.s_in, self.m_out[k])

    def workload(self, kind: str = "test") -> dict:
        """The block's own scenario: frames to every output in an irregular order, one index out of
        range (its frame dropped), frames of 1 to 9 words a burst; for ``route="header"`` the index in
        each frame's first word."""
        n, nb = int(self.n_out), int(self.frame_bursts)
        dest = [(3 * f + f // 2) % n for f in range(12)]
        dest[5] = n                                   # names no output: dropped
        frames, sel = [], []
        for f, k in enumerate(dest):
            for b in range(nb):
                n_b = 1 + (f * 7 + b * 3) % 9
                words = [(f << 12) | (b << 8) | j for j in range(n_b)]
                if self.route == "header" and b == 0:
                    words[0] = (words[0] & ~(((1 << int(self.sel_bits)) - 1) << int(self.sel_lsb))) \
                        | (k << int(self.sel_lsb))
                frames.append(StreamBurst(np.asarray(words, dtype=np.uint64), True))
            sel.append(StreamBurst(np.asarray([k], dtype=np.uint64), True))
        return {"s_in": frames, **({"s_sel": sel} if self.route == "sel" else {})}

    def golden(self, kind: str = "test") -> dict:
        """Each output: the frames that name it, in order."""
        stim = self.workload(kind)
        nb, n = int(self.frame_bursts), int(self.n_out)
        frames = [stim["s_in"][i:i + nb] for i in range(0, len(stim["s_in"]), nb)]
        out = {f"m_out_{k}": [] for k in range(n)}
        for f, fr in enumerate(frames):
            k = int(stim["s_sel"][f].words[0]) if self.route == "sel" else self.index_of(fr[0].words[0])
            if k < n:
                out[f"m_out_{k}"] += [np.asarray(b.words, dtype=np.uint64) for b in fr]
        return out


def _value(word) -> int:
    """A word as an integer: a word wider than 64 bits arrives as its 64-bit chunks, chunk 0 the low bits."""
    chunks = np.asarray(word, dtype=np.uint64).reshape(-1)
    return sum(int(c) << (64 * i) for i, c in enumerate(chunks))


class StreamDemuxStep(StreamBodyStep):
    """Copy ``stream_demux_task.h`` (both bodies) into a build (``output_dir`` relative to the build root)."""

    KEY = "stream_demux_task"
