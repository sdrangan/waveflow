"""mm_adaptor.py — several memory-mapped views behind ONE bus slave port.

``plans/mm_slave_adaptor.md``, Stage 4: the pysim half of
:func:`waveflow.build.mm_adaptor_gen.render_adaptor_slot`.  Each view module
(:class:`~waveflow.hw.mm_queue.MemSlaveWStream`, :class:`~waveflow.hw.mm_queue.MemSlaveRStream`,
:class:`~waveflow.hw.mm_regbank.MemSlaveRegBank`) keeps its own kernel-side streams and its own
semantics; this module replaces their individual bus ports with one:

    adaptor = MemSlaveAdaptor(name="fir_mm", sim=sim, views=[regs, qin, qout])
    xbar.bind("slave_0", adaptor.s_mem)                  # instead of three slave ports
    assign_address_ranges([adaptor.s_mem], [(base, adaptor.span())])

View *k* answers the 4 KB window at local offset ``k * 0x1000``, exactly as the RTL decoder lays it
out.  An AXI burst cannot cross a 4 KB boundary, so every burst lands in exactly one view.

**Ordering.**  The RTL front serves one AXI transaction at a time, reads and writes alike, from its
address phase to its response -- that is the plan's guarantee 1.  The bus port here says both halves:
``half_duplex`` (reads and writes share one channel) and ``serialize_transactions`` (the crossbar holds
that channel for the whole transfer, not just the callback).  Without the second, a short doorbell
issued after a long data burst reached the views first -- measured, ``tests/hw/test_mm_bram.py``.
The views' own ``s_mem`` endpoints stay unbound.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.hw.memif import MMIFSlave, Words
from waveflow.simulation.simobj import ProcessGen

#: Bytes per view window -- the same 4 KB the RTL decoder uses (``VIEW_LAW = 12``).
VIEW_BYTES = 4096


@dataclass
class MemSlaveAdaptor(HwModule):
    """One bus slave port in front of *views*, view *k* at local ``k * 4 KB``."""

    views: list = field(default_factory=list)
    mem_dwidth: HwParam[int] = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.views:
            raise ValueError(f"{type(self).__name__} needs at least one view")
        names = [v.name for v in self.views]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            # A bus master reaches a view BY NAME (MemSlaveMap), so a name must pick out one window.
            raise ValueError(f"{self.name}: two views are named {dup[0]!r}; view names must be "
                             f"unique within an adaptor")
        for v in self.views:
            if not hasattr(v, "s_mem"):
                raise TypeError(f"{type(v).__name__} is not a memory-mapped view (no s_mem)")
            if int(getattr(v, "window", VIEW_BYTES)) != VIEW_BYTES:
                raise ValueError(f"view '{v.name}': a multi-view adaptor uses 4 KB windows")
            if int(getattr(v, "mem_dwidth", self.mem_dwidth)) != int(self.mem_dwidth):
                raise ValueError(f"view '{v.name}' is {int(v.mem_dwidth)} bits wide, the adaptor "
                                 f"{int(self.mem_dwidth)}")
        dw = int(self.mem_dwidth)
        self._dt = np.dtype(np.uint32) if dw <= 32 else np.dtype(np.uint64)
        self.s_mem = MMIFSlave(name=f"{self.name}_s_mem", sim=self.sim, bitwidth=dw,
                               rx_write_proc=self._on_write, rx_read_proc=self._on_read,
                               peek_read=self._peek, half_duplex=True,
                               serialize_transactions=True)
        self.add_endpoint(self.s_mem)
        #: ``(time, kind, local_addr)`` for accesses that hit no view (SLVERR in RTL).
        self.errors: list[tuple[float, str, int]] = []

    def span(self) -> int:
        """Bytes of address space the adaptor needs: the view count rounded up to a power of two,
        times 4 KB (the RTL decoder's select field is whole bits)."""
        k = 1
        while k < len(self.views):
            k *= 2
        return k * VIEW_BYTES

    def offset_of(self, view) -> int:
        """Local byte offset of *view*'s window."""
        return self.views.index(view) * VIEW_BYTES

    def slave_map(self):
        """The address map a bus master needs, as plain data -- every view by name, at its absolute
        bus address.  Call it after :func:`~waveflow.hw.memif.assign_address_ranges` has given
        :attr:`s_mem` a base.  See :class:`~waveflow.hw.mm_host.MemSlaveMap`."""
        from waveflow.hw.mm_host import MemSlaveMap
        return MemSlaveMap.from_adaptor(self)

    def _route(self, local_addr: int):
        k, off = divmod(int(local_addr), VIEW_BYTES)
        return (self.views[k], off) if k < len(self.views) else (None, off)

    def _on_write(self, words: Words, local_addr: int) -> ProcessGen[None]:
        view, off = self._route(local_addr)
        if view is None:
            self.errors.append((self.env.now, "no_view", int(local_addr)))
            yield self.env.timeout(0)
            return
        yield from view.s_mem.rx_write_proc(words, off)

    def _on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        view, off = self._route(local_addr)
        if view is None:
            self.errors.append((self.env.now, "no_view", int(local_addr)))
            yield self.env.timeout(0)
            return np.zeros(int(nwords), dtype=self._dt)
        return (yield from view.s_mem.rx_read_proc(nwords, off))

    def _peek(self, nwords: int, local_addr: int) -> Words:
        view, off = self._route(local_addr)
        if view is None:
            return np.zeros(int(nwords), dtype=self._dt)
        return view.s_mem.peek_read(nwords, off)


__all__ = ["MemSlaveAdaptor", "VIEW_BYTES"]
