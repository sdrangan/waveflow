"""devsys.py -- each device alone on the bus with a host: the systems of step 5.1's RTL gates (AC8).

``plans/ltbf_dse/ltbf_dse.md`` step 5.1.  A :class:`DevHost` (a ``SwHost``; C++ twins
``ltbf_job_host.h``, ``ltbf_ingest_host.h``) writes a memory image over its bus, sends a job set into the device's Job FIFO,
takes one ``Resp`` a job from the RespHub, and reads the job's outputs back -- all recorded, so the
flow's ``compare`` step checks RTL against pysim word for word.  The devices are AC9's (``build``):

* ``sys``    -- the systolic wrapper on one job of each kind at N_rx = 64: ``T``, ``U``, ``W``, ``Z`` and
  the explicit ``K``, random operands in the build's formats;
* ``vec``    -- the vector unit's wrapper on a CG step, the last CG step (with ``X_b``) and an equalizer
  chunk of ``J`` tiles, the system's programs;
* ``ingest`` -- the Ingest DMA fed by a ``StreamDriver`` (the O-RU): two symbols of 64 antennas into a
  ring of two symbols, the host returning each chunk's credit after reading its slot.

The RespHub of a device system has one source (``RespHub1``, its own top ``ltbf_hub1``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from examples.ltbf_dse.hw.records import IngestCfg, Resp
from examples.ltbf_dse.hw.resp_hub import RespHub
from waveflow.hw.clock import Clock
from waveflow.hw.hw_system import DutTop, HwSystem
from waveflow.hw.interface import StreamIF
from waveflow.hw.irq import IrqIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import AddrUnit, MemoryMod
from waveflow.hw.mm_device import build_mm_device
from waveflow.hw.mm_host import BoundMemSlaveAdaptor, BusReader, MemSlaveLayout
from waveflow.simulation.simulation import Simulation
from waveflow.sw import SwHost

F_HOST = 250e6
W = 64
DEV_BASE, HUB_BASE = 0x8000_0000, 0x8001_0000
#: The longest plain bus write (AXI4's 256 beats).
MAX_BURST = 256
#: Scenario burst kinds (the C++ twin decodes them the same way).
K_IMAGE, K_JOB, K_READ, K_CFG, K_CHUNKS, K_SYM = 0, 1, 2, 3, 4, 5


def _burst(head, words=()) -> np.ndarray:
    """A scenario burst: its kind and fields, then *words*, all ``uint64``.  (Concatenating a Python
    int list with uint64 words promotes to float64 and rounds every word to 53 bits.)"""
    return np.concatenate([np.asarray(head, np.uint64), np.asarray(words, np.uint64)])


@dataclass
class RespHub1(RespHub):
    """A RespHub of one source: a device system's (its own HLS top)."""

    cpp_kernel_name: ClassVar[str | None] = "ltbf_hub1"
    n_src: int = 1


@dataclass
class DevHost(SwHost):
    """Image, jobs, one ``Resp`` a job, read-back -- or, for the Ingest DMA (``chunks``), its
    configuration and one ``Resp`` a chunk with the chunk's slot read back and its credit returned.
    """

    image: list = field(default_factory=list)  # (word address, words)
    jobs: list = field(default_factory=list)  # descriptor words
    reads: list = field(default_factory=list)  # (word address, words)
    #: The Ingest DMA's run: (IngestCfg words, chunks, slot words, ring base, ring depth), and the
    #: O-RU's antenna bursts it writes into the DMA's ``qsym`` queue.
    ingest: tuple | None = None
    symbols: list = field(default_factory=list)
    clk: Clock = field(default_factory=lambda: Clock(freq=F_HOST))
    #: Whether the device has a Job FIFO (its C++ twin then writes one).
    has_qjob: ClassVar[bool] = True

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=W)
        if self.has_qjob:
            self.irq_qjob = self.add_irq("irq_qjob")
        self.irq_qresp = self.add_irq("irq_qresp")
        self.bus = BusReader(self.m)
        self.qjob = self.qresp = self.ctrl = None
        self.credit_addr = 0
        self.done = self.env.event()
        self.resps: list = []
        self.got: list = []
        #: Bundles the scenario writes beside the host's (name -> bursts), and their readers.
        self.extra_bundles: dict = {}
        self.drivers: list = []

    def scenario_bursts(self):
        out = []
        for addr, words in self.image:
            for i in range(0, len(words), MAX_BURST):
                out.append(_burst([K_IMAGE, addr + i], words[i : i + MAX_BURST]))
        out += [_burst([K_JOB], j) for j in self.jobs]
        out += [_burst([K_READ, a, n]) for a, n in self.reads]
        if self.ingest is not None:
            cfg, n_chunks, slot_words, base, depth = self.ingest
            out.append(_burst([K_CFG], cfg))
            out += [_burst([K_SYM], b) for b in self.symbols]
            out.append(
                np.asarray(
                    [K_CHUNKS, n_chunks, slot_words, base, depth, self.credit_addr]
                )
            )
        return [np.asarray(b, np.uint64) for b in out]

    def write_scenario(self, root) -> None:
        from waveflow.utils.burst_io import write_burst_bundle

        super().write_scenario(root)
        for name, bursts in self.extra_bundles.items():
            write_burst_bundle(
                [np.asarray(b, np.uint64) for b in bursts], Path(root) / name
            )
        for (
            drv
        ) in self.drivers:  # the flow anchors only the host: the bundles' readers too
            drv.root = Path(root)

    def _read(self, addr: int, n: int):
        """*n* words at word address *addr*, in AXI4 bursts of at most 256 beats (as the twin)."""
        parts = []
        for i in range(0, n, MAX_BURST):
            parts.append(
                (yield from self.bus.read(min(MAX_BURST, n - i), (addr + i) * 8))
            )
        return np.concatenate(parts)

    def main(self):
        for addr, words in self.image:
            for i in range(0, len(words), MAX_BURST):
                yield from self.bus.write(
                    np.asarray(words[i : i + MAX_BURST], np.uint64), (addr + i) * 8
                )
        for j in self.jobs:
            yield from self.qjob.write(np.asarray(j, np.uint64))
        for _ in self.jobs:
            self.resps.append((yield from self.qresp.get_schema(Resp)))
        for a, n in self.reads:
            self.got.append((yield from self._read(a, n)))
        if self.ingest is not None:
            cfg, n_chunks, slot_words, base, depth = self.ingest
            yield from self.ctrl.write(np.asarray(cfg, np.uint64))
            for b in self.symbols:  # the O-RU: an antenna's burst a packet
                yield from self.qsym.write(np.asarray(b, np.uint64))
            for k in range(n_chunks):
                self.resps.append((yield from self.qresp.get_schema(Resp)))
                a = base + (k % depth) * slot_words
                self.got.append((yield from self._read(a, slot_words)))
                yield from self.bus.write(
                    np.asarray([k + 1], np.uint64), self.credit_addr
                )
        self.done.succeed()


@dataclass
class LtbfJobHost(DevHost):
    """A device with a Job FIFO (the systolic and vector-unit wrappers)."""

    cpp_model: ClassVar[str | None] = "LtbfJobHostModel"
    cpp_header: ClassVar[str | None] = "ltbf_job_host.h"


@dataclass
class LtbfIngestHost(DevHost):
    """The Ingest DMA's host: its configuration, chunks, credit."""

    cpp_model: ClassVar[str | None] = "LtbfIngestHostModel"
    cpp_header: ClassVar[str | None] = "ltbf_ingest_host.h"
    has_qjob: ClassVar[bool] = False

    def __post_init__(self) -> None:
        super().__post_init__()
        self.irq_qsym = self.add_irq("irq_qsym")
        self.qsym = None


def dev_system(
    dev,
    *,
    mem_words: int,
    host: DevHost,
    driver=None,
    sym_port: str = "s_sym",
    fresh=None,
):
    """*dev* (a wrapper) with a one-source RespHub, Shared Mem at 0 and *host*, on one crossbar; an
    optional *driver* feeds the device's *sym_port*."""
    sim, clk = dev.sim, dev.clk
    mem_words = (
        1 << (int(mem_words) - 1).bit_length()
    )  # the crossbar's RTL: power-of-two ranges
    hub = RespHub1(name="hub", sim=sim, clk=clk, word_bits=W)
    mem = MemoryMod(name="mem", sim=sim, word_size=W, inline=False, clk=clk, nwords_tot=mem_words,
                    addr_unit=AddrUnit.byte)  # fmt: skip
    assert mem.alloc(mem_words) == 0
    ddev = build_mm_device(dev, sim=sim, clk=clk, mem_dwidth=W, prefix="dev_")
    hdev = build_mm_device(hub, sim=sim, clk=clk, mem_dwidth=W, prefix="hub_")
    links = []
    pairs = [("resp", dev.m_resp, hub.s_in0)]
    if driver is not None:
        pairs.append(("sym", driver, getattr(dev, sym_port)))
    for name, m, s in pairs:
        li = StreamIF(name=name, sim=sim, clk=clk, bitwidth=W)
        li.bind("master", getattr(m, "stream_ep", m))  # a StreamDriver: its stream
        li.bind("slave", s)
        links.append(li)
    masters = [host.m, *(ep for ep in (getattr(dev, n, None) for n in
                                       ("m_in", "m_out", "m_rl", "m_rx", "m_rs", "m_rd", "m_w"))
                         if ep is not None and type(ep).__name__.startswith("MMIF"))]  # fmt: skip
    ds, dr = ddev.ranges(DEV_BASE)
    hs, hr = hdev.ranges(HUB_BASE)
    slaves = ds + hs + [mem.s_mm]
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=len(masters),
                           nports_slave=len(slaves), bitwidth=W)  # fmt: skip
    for k, m in enumerate(masters):
        xbar.bind(f"master_{k}", m)
    for k, s in enumerate(slaves):
        xbar.bind(f"slave_{k}", s)
    assign_address_ranges(slaves, dr + hr + [(0, mem_words * W // 8)])
    dmap = MemSlaveLayout.of(type(dev), mem_dwidth=W).at(DEV_BASE)
    hmap = MemSlaveLayout.of(RespHub1, mem_dwidth=W).at(HUB_BASE)
    line = IrqIF(name="qresp_irq", sim=sim)
    line.bind("source", hdev.views["qresp"].m_irq)
    line.bind("sink", host.irq_qresp)
    dad = BoundMemSlaveAdaptor(dmap, host.m)
    if "qjob" in ddev.views:
        jl = IrqIF(name="qjob_irq", sim=sim)
        jl.bind("source", ddev.views["qjob"].m_irq)
        jl.bind("sink", host.irq_qjob)
        host.qjob = dad.stream_master("qjob", irq=host.irq_qjob)
    if "credit" in ddev.views:
        host.credit_addr = dmap["credit"].base
        host.ctrl = dad.stream_master("ctrl")
    if "qsym" in ddev.views:
        sl = IrqIF(name="qsym_irq", sim=sim)
        sl.bind("source", ddev.views["qsym"].m_irq)
        sl.bind("sink", host.irq_qsym)
        host.qsym = dad.stream_master("qsym", irq=host.irq_qsym)
    host.qresp = BoundMemSlaveAdaptor(hmap, host.m).stream_slave(
        "qresp", irq=host.irq_qresp
    )
    dut = DutTop.of(
        f"ltbf_{dev.cpp_kernel_name}_dut",
        sim,
        comps=[ddev, hdev, mem],
        ifs=[xbar, *links],
    )
    # The RTL run's bound (it ends when the host is done): a device gate at N_rx 1024 moves 16 times
    # AC9's data at two cycles a read word.
    return HwSystem(
        sim,
        dut,
        n_cycles=40_000_000,
        run_until=host.done,
        check=lambda s: (host.resps, host.got),
        fresh=fresh,
    )  # fresh: the flow's pysim runs an unrun copy


def _ac9(p=None):
    """``(system, LtbfSystem)`` at *p* (``SysParams``; AC9's when ``None``)."""
    from examples.ltbf_dse.hw import system as sy

    S = sy.LtbfSystem(sy.inputs(p or sy.SysParams()))
    return sy, S


class _Mem:
    """A bump allocator over Shared Mem's words, and the image written into it."""

    def __init__(self, S, start: int = 0, seed: int = 51) -> None:
        self.S = S
        self.top = start
        self.image: list = []
        self.rng = np.random.default_rng(seed)

    def region(self, n: int, fill=None) -> int:
        """*n* words; *fill* a format: random values in it, packed as the system packs them (a
        register narrower than its lane leaves the lane's finer bits zero)."""
        a = self.top
        self.top += n
        if fill is not None:
            p = self.S.p
            lim = 1 << (fill.W - 1)
            count = n * p.word_bits // (2 * p.lane_bits)
            re, im = (
                self.rng.integers(-lim, lim, count) * 2.0**-fill.frac_bits
                for _ in range(2)
            )
            self.image.append((a, self.S.to_w(re, im, fill)))
        return a


def sys_system(p=None) -> HwSystem:
    """The systolic wrapper on one job of each kind at AC9's shapes (N_rx 64, 22 connected UEs), or
    at *p*'s; ``K`` (``R x C x R``: 8192 tiles at N_rx 1024) only up to N_rx 64, where its tile count
    is an XSI run's, not hours."""
    from examples.ltbf_dse.hw.sys_wrap import SysWrap
    from waveflow.linalg.systolic import MatmulOp
    from waveflow.linalg.systolic_engine_mem import EngineJobDesc

    sy, S = _ac9(p)
    d = S.d
    sim = Simulation()
    dev = SysWrap(
        name="ltbf_sys", sim=sim, clk=Clock(freq=F_HOST), engine_cfg=sy.engine_cfg(S)
    )
    R, C, c = d.rows, d.cols, d.c
    shapes = [("T", MatmulOp.MUL_AH, C, R, c), ("U", MatmulOp.MUL, R, C, c),
              ("W", MatmulOp.MUL_AH, C, R, c), ("Z", MatmulOp.MUL_AH, d.S, R, d.J * c),
              ("K", MatmulOp.MUL, R, C, R)][: 5 if d.n_rx <= 64 else 4]  # fmt: skip
    mem = _Mem(S)
    jobs, reads = [], []
    for tx, (_name, op, m, k, n) in enumerate(shapes):
        a = mem.region(S.w(m, k), fill=S.cfg.a)
        b = mem.region(S.w(k, n), fill=S.cfg.bc)
        cc = mem.region(S.w(m, n))
        jobs.append(EngineJobDesc(tx_id=tx, op=int(op), M=m, K=k, N=n, a_base=a, b_base=b,
                                  c_base=cc).serialize(word_bw=W))  # fmt: skip
        reads.append((cc, S.w(m, n)))
    host = LtbfJobHost(name="host", sim=sim, image=mem.image, jobs=jobs, reads=reads)
    return dev_system(dev, mem_words=mem.top, host=host, fresh=lambda: sys_system(p))


def vec_system(p=None) -> HwSystem:
    """The vector unit's wrapper on a CG step, the last CG step and an equalizer chunk, with the
    system's programs (AC9's build: one unit for both)."""
    from examples.ltbf_dse.hw.vec_wrap import VecMemJob, VecWrap

    _sy, S = _ac9(p)
    d, p = S.d, S.p
    sim = Simulation()
    dev = VecWrap(
        name="ltbf_vec", sim=sim, clk=Clock(freq=F_HOST), vu_cfg=S.vu, word_bits=W
    )
    mem = _Mem(S, start=max(a + len(w) for a, w in S.programs.values()))
    mem.image += list(S.programs.values())
    jobs, reads = [], []

    def job(prog, lds, sts, count):
        j = VecMemJob(tx_id=len(jobs), count=count, n_ld=len(lds), n_st=len(sts),
                      l_addr=prog["load"][0], l_words=prog["load"][1],
                      x_addr=prog["execute"][0], x_words=prog["execute"][1],
                      s_addr=prog["store"][0], s_words=prog["store"][1])  # fmt: skip
        for k, (base, stride, n) in enumerate(lds):
            setattr(j, f"ld{k}_base", base), setattr(j, f"ld{k}_stride", stride)
            setattr(j, f"ld{k}_words", n)
        for k, (base, stride, n) in enumerate(sts):
            setattr(j, f"st{k}_base", base), setattr(j, f"st{k}_stride", stride)
            setattr(j, f"st{k}_words", n)
        jobs.append(j.serialize(word_bw=W))

    mw = S.w(d.rows, d.c)
    for name, n_out in (("cg", 3), ("cg_last", 4)):
        ins = [(mem.region(mw, fill=f), 0, mw) for f in (S.f.X, S.f.R, S.f.D, S.f.U)]
        outs = [(mem.region(mw), 0, mw) for _ in range(n_out)]
        job(S.prog_at[name], ins, outs, 1)
        reads += [(a, n) for a, _s, n in outs]
    wb, wt = S.w(d.u_pad, d.c), S.w(d.S, d.c)
    z = mem.region(d.J * wt, fill=S.cfg.eq.z)
    w = mem.region(d.J * wt, fill=S.cfg.eq.w)
    xh = mem.region(d.J * wb)
    lds = []
    for k in range(p.r):
        lds += [(w + k * wb, wt, wb), (z + k * wb, wt, wb)]
    job(S.prog_at["eq"], lds, [(xh, wb, wb)], d.J)
    reads.append((xh, d.J * wb))
    host = LtbfJobHost(name="host", sim=sim, image=mem.image, jobs=jobs, reads=reads)
    return dev_system(dev, mem_words=mem.top, host=host, fresh=lambda: vec_system(p))


def ingest_system(n_sym: int = 2, p=None) -> HwSystem:
    """The Ingest DMA (its queue-fed form, ``IngestDevQ``) on *n_sym* of AC9's symbols (64
    antennas), a ring of two symbols; the host plays the O-RU, writing each antenna's burst.
    """
    from examples.ltbf_dse.hw.ingest import IngestDevQ

    sy, S = _ac9(p)
    d = S.d
    sim = Simulation()
    dev = IngestDevQ(
        name="ltbf_ingest", sim=sim, clk=Clock(freq=F_HOST), dma_cfg=sy.ingest_cfg(S)
    )
    depth = 2 * d.n_chunks
    yr, yi = S.inp.y
    bursts = [
        S.to_w(yr[a, s], yi[a, s], S.cfg.bc)
        for s in range(n_sym)
        for a in range(d.n_rx)
    ]
    cfg = IngestCfg(enable=1, ring_depth=depth, ring_base=0, chunk_words=S.slot_words)
    host = LtbfIngestHost(name="host", sim=sim, symbols=bursts,
                          ingest=(cfg.serialize(word_bw=W), n_sym * d.n_chunks, S.slot_words, 0,
                                  depth))  # fmt: skip
    return dev_system(
        dev,
        mem_words=depth * S.slot_words,
        host=host,
        fresh=lambda: ingest_system(n_sym, p),
    )
