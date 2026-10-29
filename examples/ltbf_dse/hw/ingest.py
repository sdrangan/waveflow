"""ingest.py -- the Ingest DMA: the O-RU's grid into Shared Mem as the projection's B operand (spec §5.4).

``plans/ltbf_dse/ltbf_dse.md`` step 4.3.  The O-RU (7.2x) delivers each symbol **antenna by antenna**:
an antenna's ``n_sc`` subcarriers as memory elements packed into bus words, one burst.  They are
already ``Y = q_bc(2^-k y)``: the gain ``2^-k`` of spec amendment 1 is the O-RU's digital gain before
its quantizer (a shift after a 16-bit quantizer could not round the same way), so the DMA moves words
and never touches a value.  :class:`IngestDma`:

* gathers ``Kmax`` antennas of a symbol in a **corner-turn buffer** (one panel row of every tile), the
  last chunk zero-padded when ``J·Nmax`` does not divide ``n_sc``;
* writes each full group as the chunks' panels: a chunk is ``J`` tiles of ``Nmax`` subcarriers of one
  symbol on every antenna, and its panel ``(p, j)`` (``Kmax`` antennas x ``Nmax`` subcarriers,
  row-major) lands at ``slot_base + (p·J + j)·wb`` -- the B layout ``SystolicEngineMem`` reads
  (``b_base + (p·NT + j)·wb``);
* after a symbol's last antenna group, pushes one :class:`~examples.ltbf_dse.hw.records.Resp`
  (``src`` 2) a chunk once its panels are in memory.  ``seq`` numbers the chunks that **arrived**, so a
  dropped symbol leaves a gap of ``n_chunks``; the ``k``-th record's slot is ``k mod ring_depth``.

**Flow control** (the framework's ``CreditIn`` view): every chunk of a symbol is written from its first
antenna group on, so a symbol takes its ``n_chunks`` slots at once (``ring_depth >= n_chunks``).  The
host writes the cumulative count of chunks it has freed (16 bits) into the ``credit`` view; when a
symbol's first antenna arrives without ``n_chunks`` free slots, the whole symbol is dropped and counted
(``IngestStatus.overruns``).  The configuration is a ``RegBank`` (``IngestCfg``); a new one is taken at
a symbol's start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from examples.ltbf_dse.hw.records import (
    SRC_INGEST,
    IngestCfg,
    IngestStatus,
    Resp,
)
from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.hw.mem_stream import MemWCmd, MemWStream
from waveflow.hw.mm_device import CreditIn, QueueIn, RegBank
from waveflow.linalg.lanes import nwords

#: The credit view's counter width (cumulative chunks freed, modulo 2^16).
CTR_BITS = 16


@dataclass
class IngestDma(FreeRunMod):
    """Antenna-by-antenna symbols in; chunk panels to memory, a ``Resp`` a chunk, credit flow control."""

    mm_views: ClassVar[tuple] = (
        RegBank("ctrl", cfg_port="s_cfg", status_port="m_status", cfg_type=IngestCfg,
                status_type=IngestStatus),
        CreditIn("credit", port="s_crd", ctr_bits=CTR_BITS),
    )  # fmt: skip

    word_bits: int = 64
    lane_bits: int = 16
    Kmax: int = 8
    Nmax: int = 8
    #: Antennas a symbol (a multiple of ``Kmax``), tiles a chunk, subcarriers a symbol.
    n_ant: int = 16
    J: int = 9
    n_sc: int = 792
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, lb = int(self.word_bits), int(self.lane_bits)
        km, nm, J = int(self.Kmax), int(self.Nmax), int(self.J)
        self.epw = w // (2 * lb)
        if nm % self.epw or self.n_sc % self.epw or self.n_ant % km:
            raise ValueError(f"{self.name}: Nmax {nm} and n_sc {self.n_sc} must be multiples of "
                             f"{self.epw} elements a word, n_ant {self.n_ant} of Kmax {km}")  # fmt: skip
        self.s_sym = StreamIFSlave(name=f"{self.name}_s_sym", sim=self.sim, bitwidth=w)
        self.s_cfg = StreamIFSlave(name=f"{self.name}_s_cfg", sim=self.sim, bitwidth=w)
        self.s_crd = StreamIFSlave(
            name=f"{self.name}_s_crd", sim=self.sim, bitwidth=CTR_BITS
        )
        self.m_w = FramedStreamIFMaster(
            name=f"{self.name}_m_w", sim=self.sim, bitwidth=w
        )
        self.m_status = StreamIFMaster(
            name=f"{self.name}_m_status", sim=self.sim, bitwidth=w
        )
        for ep in (self.s_sym, self.s_cfg, self.s_crd, self.m_w, self.m_status):
            self.add_endpoint(ep)
        self.wb = nwords(km * nm, lb, w)
        self.ant_words = nwords(int(self.n_sc), lb, w)
        self.n_chunks = -(-int(self.n_sc) // (J * nm))
        self.P = int(self.n_ant) // km
        #: A slot's words: P·J panels.
        self.slot_words = self.P * J * self.wb
        self.chunks = 0
        self.overruns = 0
        #: The credit register: the cumulative count of chunks the host has freed.
        self.credit = 0

    def run_proc(self):
        proc = super().run_proc()
        if proc is not None:
            self.env.process(self._credit_reg())
        return proc

    def _credit_reg(self):
        """The credit register follows the view: in RTL a non-blocking read every cycle, so the DMA
        sees the newest value, never the older ones still in the view's stream."""
        while True:
            v = yield from self.s_crd.get(nwords_max=1)
            self.credit = int(np.asarray(v).reshape(-1)[0])

    def _check(self, cfg) -> None:
        if int(cfg.enable) and (
            int(cfg.ring_depth) < self.n_chunks
            or int(cfg.chunk_words) < self.slot_words
        ):
            raise ValueError(f"{self.name}: a symbol needs {self.n_chunks} slots of "
                             f"{self.slot_words} words; got ring_depth {int(cfg.ring_depth)}, "
                             f"chunk_words {int(cfg.chunk_words)}")  # fmt: skip

    def kernel_task(self):
        """The hand-written HLS body, ``src/ltbf_ingest_task.h``: the twin of :meth:`run_iter` (one
        firing a symbol; the credit register read every cycle, as :meth:`_credit_reg` models).
        """
        from waveflow.hw.mem_stream import KernelTask

        ports = ("s_sym", "s_cfg", "s_crd", "m_w", "m_status")
        return KernelTask(
            "ltbf_ingest_task",
            "ltbf_ingest_task.h",
            ports,
            framed=ports,
            template_args=(
                int(self.word_bits),
                int(self.lane_bits),
                int(self.Kmax),
                int(self.Nmax),
                int(self.n_ant),
                int(self.J),
                int(self.n_sc),
            ),
        )

    def run_iter(self):
        """One firing: the configuration, then symbols forever."""
        cfg = yield from self.s_cfg.get_schema(IngestCfg)
        self._check(cfg)
        km, nm, J, P = int(self.Kmax), int(self.Nmax), int(self.J), self.P
        cols = (
            self.n_chunks * J * nm // self.epw
        )  # words a buffered antenna, padding included
        taken = seq = 0
        while True:
            first = yield from self.s_sym.get(nwords_max=self.ant_words)
            new = yield from self.s_cfg.get_schema_nb(IngestCfg)
            if new is not None:
                self._check(new)
                cfg = new
            free = int(cfg.ring_depth) - ((taken - self.credit) & ((1 << CTR_BITS) - 1))
            if not int(cfg.enable) or free < self.n_chunks:
                for _a in range(int(self.n_ant) - 1):
                    yield from self.s_sym.get(nwords_max=self.ant_words)
                if int(cfg.enable):
                    self.overruns += 1
                    seq += self.n_chunks
                    yield from self._status()
                continue
            slots = [(taken + c) % int(cfg.ring_depth) for c in range(self.n_chunks)]
            for p in range(P):
                buf = np.zeros((km, cols), np.uint64)  # the corner-turn buffer
                for a in range(km):
                    if p or a:
                        first = yield from self.s_sym.get(nwords_max=self.ant_words)
                    buf[a, : self.ant_words] = np.asarray(first, np.uint64)
                # Panel (p, j) of chunk c: these Kmax antennas' subcarriers c·J·Nmax + j·Nmax .. +Nmax,
                # row-major (Nmax elements a row, epw a word).
                for c in range(self.n_chunks):
                    base = int(cfg.ring_base) + slots[c] * int(cfg.chunk_words)
                    for j in range(J):
                        w0 = (c * J + j) * nm // self.epw
                        panel = buf[:, w0 : w0 + nm // self.epw].reshape(-1)
                        last = p == P - 1 and j == J - 1
                        yield from self.m_w.write(
                            MemWCmd(
                                addr=base + (p * J + j) * self.wb,
                                len=self.wb,
                                fwd_bursts=1 if last else 0,
                            )
                        )
                        if last:
                            yield from self.m_w.write(
                                Resp(
                                    src=SRC_INGEST,
                                    status=0,
                                    tx_id=0,
                                    seq=(seq + c) & 0xFFFF,
                                )
                            )
                        yield from self.m_w.write(panel)
            seq += self.n_chunks
            taken += self.n_chunks
            self.chunks += self.n_chunks
            yield from self._status()

    def _status(self):
        yield from self.m_status.write(
            IngestStatus(chunks=self.chunks, overruns=self.overruns)
        )


@dataclass
class IngestDev(FreeRunMod):
    """The Ingest DMA with its memory writer: the device on the bus."""

    cpp_kernel_name: ClassVar[str | None] = "ltbf_ingest"
    #: What the system top re-creates the kernel from (``hw_module.build_params``).
    build_fields: ClassVar[tuple[str, ...]] = ("dma_cfg",)
    mm_views: ClassVar[tuple] = IngestDma.mm_views

    dma_cfg: dict = field(default_factory=dict)
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.dma = IngestDma(
            name=f"{self.name}_dma", sim=self.sim, clk=self.clk, **self.dma_cfg
        )
        w = int(self.dma.word_bits)
        self.wstream = MemWStream(name=f"{self.name}_memw", sim=self.sim, mem_dwidth=w, inband=True,
                                  emit_done=True, done_framed=False, max_fwd_words=1, clk=self.clk)  # fmt: skip
        for comp in (self.dma, self.wstream):
            self.add_comp(comp)
        link = StreamIF(
            name=f"{self.name}_w_if",
            sim=self.sim,
            clk=self.clk,
            bitwidth=w,
            framed=True,
        )
        link.bind("master", self.dma.m_w)
        link.bind("slave", self.wstream.s_in)
        self.add_if(link)
        # In the walk order the top generator pairs names with (children, then their ports).
        self.boundary = ["s_sym", "s_cfg", "s_crd", "m_status", "m_w", "m_resp"]
        self.s_sym, self.s_cfg, self.s_crd = (
            self.dma.s_sym,
            self.dma.s_cfg,
            self.dma.s_crd,
        )
        self.m_status = self.dma.m_status
        self.m_w = self.wstream.m_mem
        self.m_resp = self.wstream.s_done


@dataclass
class IngestDevQ(IngestDev):
    """The Ingest DMA with its symbol input as a memory-mapped queue (``qsym``): the RTL gates' form,
    where the software host plays the O-RU, an antenna's burst a packet (a stream driver beside a
    software host has no bundle in the XSI run).  The same HLS top; the view is RTL beside it."""

    mm_views: ClassVar[tuple] = (*IngestDev.mm_views, QueueIn("qsym", port="s_sym", depth=512))
