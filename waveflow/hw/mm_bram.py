"""mm_bram.py — a memory-mapped window onto one port of a block RAM.

``plans/mm_slave_adaptor.md``, Stage 3: the pysim half of ``waveflow/build/rtl/mm_bram_port.v`` (with
the ``bram_t2p`` it drives).  The bus reaches the memory through :attr:`MemSlaveBramWindow.s_mem`
(port A); the kernel's port B is :meth:`port_b_read` / :meth:`port_b_write` -- untimed, as a BRAM port
is one word per cycle at a fixed latency that the kernel's own model accounts for.

Window: word *i* at local byte ``i * bytes_per_word``; words at or beyond ``nelem`` are outside the
memory (a write there is dropped, a read answers 0 and is recorded in :attr:`errors` -- SLVERR in RTL).

**Ordering lives in the adaptor, not here.**  Put this view and its doorbell (a queue or register bank)
behind one :class:`~waveflow.hw.mm_adaptor.MemSlaveAdaptor` and a doorbell written after the data
cannot reach the kernel before it; put them on separate bus ports and nothing orders them.  The RTL
pair is ``tests/build/test_mm_bram_order_xsi.py``; the pysim pair is ``tests/hw/test_mm_bram.py``.

Not yet joined to :class:`~waveflow.hw.bram.T2pBram` / ``BramIF`` (the kernel-side port B as a
declared endpoint, so a generated kernel can bind it) -- a follow-up the plan records.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.hw.memif import MMIFSlave, Words
from waveflow.simulation.simobj import ProcessGen


@dataclass
class MemSlaveBramWindow(HwModule):
    """A BRAM of ``nelem`` words: port A on the bus (:attr:`s_mem`), port B for the kernel."""

    mem_dwidth: HwParam[int] = 64
    nelem: HwParam[int] = 512
    window: int = 4096
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        dw, n = int(self.mem_dwidth), int(self.nelem)
        self._bpw = dw // 8
        if n < 1 or n & (n - 1):
            raise ValueError(f"{type(self).__name__}: nelem must be a power of two, got {n} "
                             f"(bram_t2p addresses mem[addr[AW-1:0]]; any other depth aliases)")
        if n * self._bpw > int(self.window):
            raise ValueError(f"{self.name}: {n} words of {self._bpw} bytes exceed the "
                             f"{int(self.window)}-byte window")
        self._dt = np.dtype(np.uint32) if dw <= 32 else np.dtype(np.uint64)
        self.mem = np.zeros(n, dtype=self._dt)
        self.s_mem = MMIFSlave(name=f"{self.name}_s_mem", sim=self.sim, bitwidth=dw,
                               rx_write_proc=self._on_write, rx_read_proc=self._on_read,
                               peek_read=self._peek)
        self.add_endpoint(self.s_mem)
        self.errors: list[tuple[float, str, int]] = []

    # -- kernel side: port B -------------------------------------------------------------------------
    def port_b_read(self, i: int) -> int:
        return int(self.mem[int(i) % int(self.nelem)])

    def port_b_write(self, i: int, value: int) -> None:
        self.mem[int(i) % int(self.nelem)] = value

    # -- bus side: port A ----------------------------------------------------------------------------
    def _on_write(self, words: Words, local_addr: int) -> ProcessGen[None]:
        w0 = int(local_addr) // self._bpw
        for i, w in enumerate(np.asarray(words).tolist()):
            if w0 + i < int(self.nelem):
                self.mem[w0 + i] = w
            else:
                self.errors.append((self.env.now, "outside_memory", int(local_addr) + i * self._bpw))
        yield self.env.timeout(0)

    def _on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        yield self.env.timeout(0)
        out = self._peek(nwords, local_addr)
        w0 = int(local_addr) // self._bpw
        for i in range(int(nwords)):
            if w0 + i >= int(self.nelem):
                self.errors.append((self.env.now, "outside_memory", int(local_addr) + i * self._bpw))
        return out

    def _peek(self, nwords: int, local_addr: int) -> Words:
        w0 = int(local_addr) // self._bpw
        out = np.zeros(int(nwords), dtype=self._dt)
        for i in range(int(nwords)):
            if w0 + i < int(self.nelem):
                out[i] = self.mem[w0 + i]
        return out


__all__ = ["MemSlaveBramWindow"]
