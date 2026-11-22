"""arbiter.py -- ``StreamArbiter``: ``n_in`` framed streams into one, a whole frame at a time.

The way several clients share one stream -- and so one kernel behind it (``plans/shared_kernel.md``).  Each
firing picks one input holding a frame, copies that frame whole to ``m_out``, and writes the input's index
on ``m_sel``, one word a frame, before the frame.  Frames are never interleaved: once a frame starts, the
block copies it to its last word, whatever the other inputs hold.

* **A frame** is ``frame_bursts`` consecutive TLAST bursts on one input -- 1 for a self-contained message,
  2 for a header then its payload (``Job`` then its words), and so on.  The block counts TLASTs and reads
  nothing else, so it knows no message format.
* **The policy** is fixed at build time (D4): ``"priority"`` -- the lowest-numbered input holding a frame
  wins, so input 0 is the most urgent (a feedback path, say); ``"round_robin"`` -- the search starts one
  past the last winner, so no input waits more than ``n_in - 1`` frames while it holds one.
* **``m_sel``** carries the winner's index, so a stage further on can route each frame back to its client
  (a :class:`~waveflow.streams.demux.StreamDemux` reading it as ``s_sel``) or tag it.  It is one word, TLAST
  set, ``bitwidth`` wide.

Two rules a design keeps (``docs/guide/streams/arbiter.md``):

* **announce a frame only when it is all there** -- the block commits on its first word and then waits for
  the rest, so an input whose frame is still being produced stalls every other input meanwhile;
* **read ``m_sel`` as the frames go by** -- the block writes it before each frame, so a full ``m_sel`` FIFO
  stops it.

Which input wins depends on when frames arrive, so a run's output order is a fact about its timing: pysim
and RTL agree per input, not on the interleave.  The HLS body (``src/stream_arbiter_task.h``) polls each
input with ``empty()``; the model waits with ``wait_any`` on the same inputs in the same order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave
from waveflow.streams.body_step import StreamBodyStep
from waveflow.utils.burst_io import StreamBurst

__all__ = ["StreamArbiter", "StreamArbiterStep", "POLICIES"]

#: The policies and their code in the HLS body's ``RR`` template argument.
POLICIES = {"priority": 0, "round_robin": 1}


@dataclass
class StreamArbiter(FreeRunMod):
    """``n_in`` framed inputs (``s_in_0`` .. ``s_in_<n_in-1>``) into one (``m_out``), a whole frame at a
    time; the winner's index on ``m_sel``, one word before each frame.

    Build parameters: ``n_in`` (2 to 16), ``bitwidth`` (every port's), ``frame_bursts`` (TLAST bursts a
    frame, at least 1) and ``policy`` (``"priority"`` or ``"round_robin"``)."""

    cpp_kernel_name: ClassVar[str | None] = "stream_arbiter"
    #: Build inputs that are not template parameters: a fresh copy (the harness's DUT) keeps them.
    build_fields: ClassVar[tuple[str, ...]] = ("policy",)

    n_in: HwParam[int] = 2
    bitwidth: HwParam[int] = 32
    frame_bursts: HwParam[int] = 1
    #: Not an ``HwParam`` (those are integers); it reaches the body as its ``RR`` template argument.
    policy: str = "priority"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        n, w = int(self.n_in), int(self.bitwidth)
        if not 2 <= n <= 16:
            raise ValueError(f"{self.name}: n_in must be 2 to 16, got {n}")
        if not 1 <= int(self.frame_bursts) <= 255:
            raise ValueError(f"{self.name}: frame_bursts must be 1 to 255, got {self.frame_bursts}")
        if self.policy not in POLICIES:
            raise ValueError(f"{self.name}: policy must be one of {sorted(POLICIES)}, got {self.policy!r}")
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=w)
        self.m_sel = FramedStreamIFMaster(name=f"{self.name}_m_sel", sim=self.sim, bitwidth=w)
        self.add_endpoint(self.m_out)
        self.add_endpoint(self.m_sel)
        self.s_in: list[FramedStreamIFSlave] = []
        for k in range(n):
            ep = FramedStreamIFSlave(name=f"{self.name}_s_in_{k}", sim=self.sim, bitwidth=w)
            setattr(self, f"s_in_{k}", ep)
            self.add_endpoint(ep)
            self.s_in.append(ep)
        #: Round-robin: where the next search starts.  State across firings, as the body's ``static``.
        self.next_k = 0

    @property
    def ports(self) -> tuple[str, ...]:
        """The body's signature: the outputs, then the inputs in order (the pack last)."""
        return ("m_out", "m_sel", *(f"s_in_{k}" for k in range(int(self.n_in))))

    def kernel_task(self):
        """The framework body, templated on the width, the frame, the policy and every port's word type."""
        from waveflow.hw.mem_stream import KernelTask

        return KernelTask("stream_arbiter_task", "stream_arbiter_task.h", self.ports,
                          template_args=(int(self.bitwidth), int(self.frame_bursts), POLICIES[self.policy]),
                          framed=self.ports)

    def search_order(self) -> list[int]:
        """The inputs in the order this firing checks them."""
        n = int(self.n_in)
        start = self.next_k if self.policy == "round_robin" else 0
        return [(start + j) % n for j in range(n)]

    def run_iter(self):
        """Wait for any input to hold a frame; the first in :meth:`search_order` wins.  Its index on
        ``m_sel``, then its ``frame_bursts`` bursts on ``m_out``."""
        order = self.search_order()
        i = yield from self.wait_any(*(self.s_in[k] for k in order))
        k = order[i]
        if self.policy == "round_robin":
            self.next_k = (k + 1) % int(self.n_in)
        yield from self.m_sel.write(np.asarray([k], dtype=np.uint64))
        for _ in range(int(self.frame_bursts)):
            words = yield from self.s_in[k].get()
            yield from self.m_out.write(words)

    def workload(self, kind: str = "test") -> dict:
        """The block's own scenario: per input, frames whose words name their input and frame (input
        ``k``, frame ``f``, word ``j`` is ``k << 24 | f << 12 | j``), of 1 to 9 words a burst."""
        return {f"s_in_{k}": frames_of(k, int(self.frame_bursts), kind) for k in range(int(self.n_in))}


def frames_of(k: int, frame_bursts: int, kind: str = "test") -> list[StreamBurst]:
    """Input *k*'s frames for a block's scenario (ports of 32 bits or more): bursts whose words name
    input ``k``, frame ``f``, burst ``b`` and position ``j`` -- ``k << 24 | f << 12 | b << 8 | j`` -- so a
    frame copied to the wrong place, split or merged is visible.  Inputs carry different frame counts."""
    lens = {"test": (1, 4, 9, 2, 1, 6), "long": (8,) * 12}[kind]
    out = []
    for f, n in enumerate(lens[k % 2:] if kind == "test" else lens):
        for b in range(frame_bursts):
            n_b = max(1, (n + b) % 10)
            out.append(StreamBurst(np.asarray([(k << 24) | (f << 12) | (b << 8) | j for j in range(n_b)],
                                              dtype=np.uint64), True))
    return out


class StreamArbiterStep(StreamBodyStep):
    """Copy ``stream_arbiter_task.h`` into a build (``output_dir`` relative to the build root)."""

    KEY = "stream_arbiter_task"
