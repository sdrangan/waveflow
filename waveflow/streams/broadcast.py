"""broadcast.py -- ``StreamBroadcast``: one framed stream in, ``n_out`` copies out.

Every word read is written to every output, TLAST included, so the framing travels with the data: each
output carries exactly the input's bursts.  The block knows nothing about messages -- which is what
makes it general, and why a design reaches for it first when two stages each need part of one stream:
each consumer reads its copy and skips what is not its own.

Two consequences a design must respect (``docs/guide/streams/broadcast.md``):

* **the outputs move in lockstep** -- a word is written to every output in the same firing, so a full
  output stalls them all, and the slowest consumer paces the rest (within the FIFO depths);
* **a consumer must keep consuming its own copy** -- it may not wait on a sibling for something that
  only comes after data still queued in its own FIFO, or the block stalls and the sibling starves.

The HLS body (``src/stream_broadcast_task.h``) is templated on the width and on each port's word type,
supplied by the generator where the block sits (``KernelTask.framed``): one body serves the block as the
top of its own harness, at a composite's boundary and inside one.  :class:`StreamBroadcastStep` copies
it into a build.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.build.build import Buildable, BuildConfig
from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave
from waveflow.utils.burst_io import StreamBurst

_SRC_DIR = Path(__file__).resolve().parent / "src"

__all__ = ["StreamBroadcast", "StreamBroadcastStep"]


@dataclass
class StreamBroadcast(FreeRunMod):
    """One framed stream in (``s_in``), ``n_out`` copies out (``m_out_0`` .. ``m_out_<n_out-1>``).

    Build parameters: ``n_out`` (at least 1) and ``bitwidth`` (the data width of every port)."""

    cpp_kernel_name: ClassVar[str | None] = "stream_broadcast"

    n_out: HwParam[int] = 2
    bitwidth: HwParam[int] = 32
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        n, w = int(self.n_out), int(self.bitwidth)
        if n < 1:
            raise ValueError(f"{self.name}: n_out must be at least 1, got {n}")
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=w)
        self.add_endpoint(self.s_in)
        self.m_out: list[FramedStreamIFMaster] = []
        for k in range(n):
            ep = FramedStreamIFMaster(name=f"{self.name}_m_out_{k}", sim=self.sim, bitwidth=w)
            setattr(self, f"m_out_{k}", ep)
            self.add_endpoint(ep)
            self.m_out.append(ep)

    @property
    def ports(self) -> tuple[str, ...]:
        """The body's signature: the input, then the outputs in order."""
        return ("s_in", *(f"m_out_{k}" for k in range(int(self.n_out))))

    def kernel_task(self):
        """The framework body, templated on the width and every port's word type (all framed)."""
        from waveflow.hw.mem_stream import KernelTask

        return KernelTask("stream_broadcast_task", "stream_broadcast_task.h", self.ports,
                          template_args=(int(self.bitwidth),), framed=self.ports)

    def run_iter(self):
        """One burst in, the same burst out on every output, in order."""
        x = yield from self.s_in.get()
        for ep in self.m_out:
            yield from ep.write(x)

    def workload(self, kind: str = "test") -> dict:
        """The block's own scenario: bursts of 1 to 17 words, from a fixed seed."""
        rng = np.random.default_rng(7)
        top = 1 << min(int(self.bitwidth), 63)
        lengths = (1, 3, 8, 1, 17, 2) if kind == "test" else (64,) * 8
        return {"s_in": [StreamBurst(rng.integers(0, top, n, dtype=np.uint64), True) for n in lengths]}


class StreamBroadcastStep(Buildable):
    """Copy ``stream_broadcast_task.h`` into a build (``output_dir`` relative to the build root) --
    framework code, shipped from ``waveflow/streams/src`` as the RF blocks' bodies are."""

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {"stream_broadcast_task": self._output_dir / "stream_broadcast_task.h"}

    def generate(self, key: str, config: BuildConfig) -> str:
        if key != "stream_broadcast_task":
            raise KeyError(f"Unknown StreamBroadcastStep output key: {key!r}")
        return (_SRC_DIR / "stream_broadcast_task.h").read_text(encoding="utf-8")
