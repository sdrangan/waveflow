"""framer.py -- ``StreamFramer``: a stream framed by count into a framed stream, TLAST every
``frame_words`` words.

Some units frame their output by count alone -- ``SsrFft``'s output is ``L/R`` words a frame with no TLAST
-- so nothing after them can route, split or check by frame: a :class:`~waveflow.streams.demux.StreamDemux`
needs TLAST to know where one frame ends.  The framer puts it back.  It reads ``frame_words`` words, and
writes them unchanged with TLAST on the last, frame after frame.  It knows no message format: the frame
length is a build parameter, the count the unit itself framed by.

It is a word-for-word stage (II = 1), so it costs a cycle of latency and nothing in rate, and it drains its
input whenever its output has room (``drains_inputs``), so a link into it on a cycle is no hazard by
itself (``waveflow.hw.loop_check``).

The HLS body (``src/stream_framer_task.h``) is templated on the width, the frame length and the output's
word type, supplied by the generator where the block sits (``KernelTask.framed``); :class:`StreamFramerStep`
copies it into a build.  ``docs/guide/streams/framer.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import FramedStreamIFMaster, StreamIFSlave
from waveflow.streams.body_step import StreamBodyStep
from waveflow.utils.burst_io import StreamBurst

__all__ = ["StreamFramer", "StreamFramerStep"]

#: The cap on one read of the unframed input: above any burst, so a read takes a burst whole.
_WHOLE_BURST = 1 << 30


@dataclass
class StreamFramer(FreeRunMod):
    """``s_in`` (words, no TLAST) to ``m_out`` (framed): every ``frame_words`` words a frame, unchanged.

    Build parameters: ``frame_words`` (at least 1) and ``bitwidth`` (both ports')."""

    cpp_kernel_name: ClassVar[str | None] = "stream_framer"
    #: A word in, the same word out: it reads whenever its output has room.
    drains_inputs: ClassVar[bool] = True

    frame_words: HwParam[int] = 16
    bitwidth: HwParam[int] = 32
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        if int(self.frame_words) < 1:
            raise ValueError(f"{self.name}: frame_words must be at least 1, got {self.frame_words}")
        w = int(self.bitwidth)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=w, has_tlast=False)
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=w)
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.m_out)
        #: Words read and not yet written: a frame may arrive over several bursts, or a burst hold
        #: several frames.  State across firings, as the HLS body's position in its frame.
        self._pending = np.zeros((0,), dtype=np.uint64)

    def kernel_task(self):
        """The framework body, templated on the width, the frame length and the output's word type."""
        from waveflow.hw.mem_stream import KernelTask

        return KernelTask("stream_framer_task", "stream_framer_task.h", ("s_in", "m_out"),
                          template_args=(int(self.bitwidth), int(self.frame_words)), framed=("m_out",))

    def run_iter(self):
        """One frame out: read until ``frame_words`` words are held, write them with TLAST on the last."""
        n = int(self.frame_words)
        while self._pending.shape[0] < n:
            # A whole burst, whatever its length: a read capped below it would drop the rest, and the
            # words past this frame are kept for the next.
            got = yield from self.s_in.get(nwords_max=_WHOLE_BURST)
            self._pending = np.concatenate([self._pending, np.asarray(got, dtype=np.uint64).reshape(-1)])
        frame, self._pending = self._pending[:n], self._pending[n:]
        yield from self.m_out.write(frame)

    def workload(self, kind: str = "test") -> dict:
        """The block's own scenario: words arriving in bursts that do not line up with the frames (a frame
        over several bursts, several frames in one), a whole number of frames in all."""
        n = int(self.frame_words)
        lengths = (1, 3, 2 * n, n - 1 if n > 1 else 1, 5) if kind == "test" else (n,) * 8
        total = sum(lengths)
        pad = (-total) % n
        if pad:
            lengths = (*lengths, pad)
        top = 1 << min(int(self.bitwidth), 63)
        rng = np.random.default_rng(11)
        return {"s_in": [StreamBurst(rng.integers(0, top, k, dtype=np.uint64), False) for k in lengths]}

    def golden(self, kind: str = "test") -> dict:
        """The same words, cut into frames of ``frame_words``."""
        words = np.concatenate([np.asarray(b.words, dtype=np.uint64) for b in self.workload(kind)["s_in"]])
        n = int(self.frame_words)
        return {"m_out": [words[i:i + n] for i in range(0, words.shape[0], n)]}


class StreamFramerStep(StreamBodyStep):
    """Copy ``stream_framer_task.h`` into a build (``output_dir`` relative to the build root)."""

    KEY = "stream_framer_task"
