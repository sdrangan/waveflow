"""examples/ltbf_dse/hw: the engines' wrappers on the bus, in pysim (plans/ltbf_dse/ltbf_dse.md, Phase 4).

Each test builds the piece of the system it checks -- a software host on a crossbar with Shared Mem,
the wrapper's memory-mapped device and the RespHub -- and runs it as the spec describes (§5): the
host writes jobs into the wrapper's Job FIFO, sleeps on the Resp FIFO's interrupt, and reads the
results back from Shared Mem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import pytest

from examples.ltbf_dse.hw.records import SRC_SYS, Resp
from examples.ltbf_dse.hw.resp_hub import RespHub
from examples.ltbf_dse.hw.sys_wrap import SysWrap
from examples.ltbf_dse.ltbf_fixed import reg
from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_system import DutTop, HwSystem
from waveflow.hw.interface import StreamIF, StreamIFMaster
from waveflow.hw.irq import IrqIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import AddrUnit, MemoryMod
from waveflow.hw.mm_device import build_mm_device
from waveflow.hw.mm_host import BoundMemSlaveAdaptor, MemSlaveLayout
from waveflow.simulation.simulation import Simulation
from waveflow.sw import SwHost

W = 64  # mm_host writes queue words of at most 64 bits (mm_host.py:427); the bus is 64-bit
SYS_BASE, HUB_BASE = 0x8000_0000, 0x8001_0000


@dataclass
class JobHost(SwHost):
    """Writes each job's descriptor into ``qjob``, then takes one ``Resp`` a job from ``qresp``."""

    bursts: list = field(default_factory=list)
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))
    cpp_model: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=W)
        self.irq = {v: self.add_irq(f"irq_{v}") for v in ("qjob", "qresp")}
        self.qjob = self.qresp = None
        self.done = self.env.event()
        self.resps: list[Resp] = []

    def scenario_bursts(self):
        """The scenario both realizations read: one burst a job (its descriptor's words)."""
        return [np.asarray(b, np.uint64) for b in self.bursts]

    def main(self):
        for b in self.bursts:
            yield from self.qjob.write(b)
        for _ in self.bursts:
            r = yield from self.qresp.get_schema(Resp)
            self.resps.append(r)
        self.done.succeed()


def systolic_system(engine_cfg: dict, kind: str = "test"):
    """The systolic wrapper, the RespHub, Shared Mem at bus address 0 and a host, on one crossbar."""
    sim = Simulation()
    clk = Clock(freq=250e6)
    sysw = SysWrap(name="sys", sim=sim, clk=clk, engine_cfg=engine_cfg)
    hub = RespHub(name="hub", sim=sim, clk=clk, n_src=1, word_bits=W)
    work = sysw.engine.workload(kind)
    image = work["m_in"]
    mem = MemoryMod(name="mem", sim=sim, word_size=W, inline=False, clk=clk,
                    nwords_tot=len(image), addr_unit=AddrUnit.byte)  # fmt: skip
    assert mem.alloc(len(image)) == 0  # Shared Mem's one region, at bus address 0
    host = JobHost(
        name="host", sim=sim, clk=clk, bursts=[b.words for b in work["s_job"]]
    )
    sdev = build_mm_device(sysw, sim=sim, clk=clk, mem_dwidth=W, prefix="sys_")
    hdev = build_mm_device(hub, sim=sim, clk=clk, mem_dwidth=W, prefix="hub_")
    link = StreamIF(name="sys_resp", sim=sim, clk=clk, bitwidth=W)
    link.bind("master", sysw.m_resp)
    link.bind("slave", hub.s_in0)
    masters = [host.m, sysw.m_in, sysw.m_out]
    ss, sr = sdev.ranges(SYS_BASE)
    hs, hr = hdev.ranges(HUB_BASE)
    slaves = ss + hs + [mem.s_mm]
    ranges = sr + hr + [(0, len(image) * W // 8)]
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=len(masters),
                           nports_slave=len(slaves), bitwidth=W)  # fmt: skip
    for k, m in enumerate(masters):
        xbar.bind(f"master_{k}", m)
    for k, s in enumerate(slaves):
        xbar.bind(f"slave_{k}", s)
    assign_address_ranges(slaves, ranges)
    smap = MemSlaveLayout.of(SysWrap, mem_dwidth=W).at(SYS_BASE)
    hmap = MemSlaveLayout.of(RespHub, mem_dwidth=W).at(HUB_BASE)
    for dev, view, key in ((sdev, "qjob", "qjob"), (hdev, "qresp", "qresp")):
        line = IrqIF(name=f"{view}_irq", sim=sim)
        line.bind("source", dev.views[view].m_irq)
        line.bind("sink", host.irq[key])
    host.qjob = BoundMemSlaveAdaptor(smap, host.m).stream_master(
        "qjob", irq=host.irq["qjob"]
    )
    host.qresp = BoundMemSlaveAdaptor(hmap, host.m).stream_slave(
        "qresp", irq=host.irq["qresp"]
    )
    dut = DutTop.of("ltbf_sys_dut", sim, comps=[sdev, hdev, mem], ifs=[xbar, link])
    sysm = HwSystem(sim, dut, run_until=host.done, check=lambda s: host.resps)
    sysm.mem, sysm.host, sysm.sysw, sysm.image = mem, host, sysw, image
    return sysm


def test_systolic_wrapper_runs_jobs_from_its_job_fifo():
    """Step 4.1: jobs written over the bus into the systolic wrapper's ``qjob`` run on the unchanged
    ``SystolicEngineMem``; each is answered by one ``Resp`` (``src`` 0, its ``tx_id``, OK) in the Resp
    FIFO after its ``C`` is in Shared Mem, and every ``C`` equals ``linalg.matmul``'s.
    """
    f = reg(12, 1)
    cfg = {"word_bits": W, "Mmax": 8, "Kmax": 8, "Nmax": 8, "Pmax": 4, "R": 4, "C": 8, "L": 4,
           "a": f, "b": f, "c": f, "pysim": "tiles"}  # fmt: skip
    sysm = systolic_system(cfg)
    mem = sysm.mem
    mem._mem.write(0, sysm.image)  # Shared Mem preloaded with A and B (block-major)
    resps = sysm.run()
    jobs = [d for d, *_ in sysm.sysw.engine._layout("test")[0]]
    assert [int(r.src) for r in resps] == [SRC_SYS] * len(jobs)
    assert sorted(int(r.tx_id) for r in resps) == sorted(int(d.tx_id) for d in jobs)
    assert all(int(r.status) == 0 for r in resps)
    for c_base, want in sysm.sysw.engine.expected_c("test"):
        got = mem._mem.read(c_base * W // 8, len(want))
        assert np.array_equal(np.asarray(got, np.uint64).reshape(want.shape), want)


# -- the vector unit's wrapper ------------------------------------------------------------------------

VEC_BASE = 0x8002_0000


def vec_system(vu_cfg, image_words: int, jobs: list):
    """The vector-unit wrapper, the RespHub, Shared Mem at bus address 0 and a host on one crossbar;
    the host sends *jobs* (``VecMemJob`` bursts)."""
    from examples.ltbf_dse.hw.vec_wrap import VecWrap

    sim = Simulation()
    clk = Clock(freq=250e6)
    vw = VecWrap(name="vec", sim=sim, clk=clk, vu_cfg=vu_cfg, word_bits=W)
    hub = RespHub(name="hub", sim=sim, clk=clk, n_src=1, word_bits=W)
    mem = MemoryMod(name="mem", sim=sim, word_size=W, inline=False, clk=clk,
                    nwords_tot=image_words, addr_unit=AddrUnit.byte)  # fmt: skip
    assert mem.alloc(image_words) == 0
    host = JobHost(name="host", sim=sim, clk=clk, bursts=jobs)
    vdev = build_mm_device(vw, sim=sim, clk=clk, mem_dwidth=W, prefix="vec_")
    hdev = build_mm_device(hub, sim=sim, clk=clk, mem_dwidth=W, prefix="hub_")
    link = StreamIF(name="vec_resp", sim=sim, clk=clk, bitwidth=W)
    link.bind("master", vw.m_resp)
    link.bind("slave", hub.s_in0)
    masters = [host.m, vw.m_rl, vw.m_rx, vw.m_rs, vw.m_rd, vw.m_w]
    vs, vr = vdev.ranges(VEC_BASE)
    hs, hr = hdev.ranges(HUB_BASE)
    slaves = vs + hs + [mem.s_mm]
    ranges = vr + hr + [(0, image_words * W // 8)]
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=len(masters),
                           nports_slave=len(slaves), bitwidth=W)  # fmt: skip
    for k, m in enumerate(masters):
        xbar.bind(f"master_{k}", m)
    for k, s in enumerate(slaves):
        xbar.bind(f"slave_{k}", s)
    assign_address_ranges(slaves, ranges)
    from examples.ltbf_dse.hw.vec_wrap import VecWrap as _VW

    vmap = MemSlaveLayout.of(_VW, mem_dwidth=W).at(VEC_BASE)
    hmap = MemSlaveLayout.of(RespHub, mem_dwidth=W).at(HUB_BASE)
    for dev, view in ((vdev, "qjob"), (hdev, "qresp")):
        line = IrqIF(name=f"{view}_irq", sim=sim)
        line.bind("source", dev.views[view].m_irq)
        line.bind("sink", host.irq[view])
    host.qjob = BoundMemSlaveAdaptor(vmap, host.m).stream_master(
        "qjob", irq=host.irq["qjob"]
    )
    host.qresp = BoundMemSlaveAdaptor(hmap, host.m).stream_slave(
        "qresp", irq=host.irq["qresp"]
    )
    dut = DutTop.of("ltbf_vec_dut", sim, comps=[vdev, hdev, mem], ifs=[xbar, link])
    sysm = HwSystem(sim, dut, run_until=host.done, check=lambda s: host.resps)
    sysm.mem, sysm.host, sysm.vw = mem, host, vw
    return sysm


def test_vector_unit_wrapper_runs_a_cg_step_from_shared_mem():
    """Step 4.2: one CG step (``hw/programs.py``, the last iteration's variant with ``X_b``) run by the
    vector-unit wrapper from Shared Mem: the programs and ``X, R, D, U`` in memory, one ``VecMemJob``
    in the Job FIFO, one ``Resp`` (``src`` 1, OK) after the outputs are in memory, and ``X, R, D, X_b``
    equal to the fixed-point chain's step (``linalg.cg.vec_step``) bit for bit."""
    from examples.ltbf_dse import ltbf_fixed_chain as fc
    from examples.ltbf_dse.hw import programs as pg
    from examples.ltbf_dse.hw.records import SRC_VEC
    from examples.ltbf_dse.hw.vec_wrap import MAX_LD, MAX_ST, VecMemJob
    from waveflow.linalg import cg as lcg
    from waveflow.linalg.lanes import from_words, nwords, to_words
    from waveflow.vecunit.asm import assemble

    ranges = {"H": 0.025, "G": 0.24, "BC": 0.08, "X": 6.8, "Y": 6.7, "Z": 8.5, "R": 0.07,
              "S": 0.02, "ps": 0.05, "rz": 0.18, "alpha": 200.0, "beta": 5.1, "w": 0.5, "y": 1.8,
              "xh": 1.8}  # fmt: skip
    cfg = fc.formats(ranges, 14)
    k, n, s = 16, 8, 2
    f = cfg.cg_step()
    vu = pg.cg_step_config(
        f, k, n, L=8, word_bits=W, lane_bits=16, m_max=max(8, cfg.m_shift)
    )
    progs = pg.cg_step_programs(vu, k, n, s, last_m=cfg.m_shift)
    words = assemble(vu, progs, tx_id=0)
    rng = np.random.default_rng(5)

    def ints(fmt, shift=3):
        lo, hi = -(1 << (fmt.W - 1)), 1 << (fmt.W - 1)
        return (
            rng.integers(lo, hi, (k, n)) >> shift,
            rng.integers(lo, hi, (k, n)) >> shift,
        )

    xr, xi = ints(cfg.cg.X, 0)
    rr, ri = ints(cfg.cg.R)
    dr, di = ints(cfg.bc)
    ur, ui = ints(cfg.bc, 2)
    lb = 16
    mw = nwords(k * n, lb, W)
    # Shared Mem: the three programs, then X, R, D, U, then four outputs
    layout, addr, image = {}, 0, []
    for name in ("load", "execute", "store"):
        layout[name] = (addr, len(words[name]))
        image.append(np.asarray(words[name], np.uint64))
        addr += len(words[name])
    ins = {
        "X": (xr, xi, cfg.cg.X),
        "R": (rr, ri, cfg.cg.R),
        "D": (dr, di, cfg.bc),
        "U": (ur, ui, cfg.bc),
    }
    for name, (re, im, fmt) in ins.items():
        layout[name] = (addr, mw)
        image.append(np.asarray(to_words(re, im, fmt, lb, W), np.uint64))
        addr += mw
    for name in ("oX", "oR", "oD", "oXB"):
        layout[name] = (addr, mw)
        image.append(np.zeros(mw, np.uint64))
        addr += mw
    image = np.concatenate(image)
    job = VecMemJob(tx_id=7, count=1, n_ld=4, n_st=4,
                    l_addr=layout["load"][0], l_words=layout["load"][1],
                    x_addr=layout["execute"][0], x_words=layout["execute"][1],
                    s_addr=layout["store"][0], s_words=layout["store"][1])  # fmt: skip
    for q, name in enumerate(("X", "R", "D", "U")):
        setattr(job, f"ld{q}_base", layout[name][0]), setattr(job, f"ld{q}_stride", 0)
        setattr(job, f"ld{q}_words", mw)
    for q, name in enumerate(("oX", "oR", "oD", "oXB")):
        setattr(job, f"st{q}_base", layout[name][0]), setattr(job, f"st{q}_stride", 0)
        setattr(job, f"st{q}_words", mw)
    assert MAX_LD == MAX_ST == 4
    sysm = vec_system(vu, len(image), [np.asarray(job.serialize(word_bw=W), np.uint64)])
    sysm.mem._mem.write(0, image)
    resps = sysm.run()
    assert [(int(r.src), int(r.tx_id), int(r.status)) for r in resps] == [
        (SRC_VEC, 7, 0)
    ]
    rz_full, rz_fmt = lcg._column_dot(rr, ri, cfg.cg.R, rr, ri, cfg.cg.R)
    st = lcg.CgState(xr, xi, rr, ri, dr, di, lcg._q(rz_full, rz_fmt, cfg.cg.rz))
    sr, si = fc.cg_step_s(dr, di, ur, ui, cfg, s)
    new, _ = lcg.vec_step(st, sr, si, cfg.cg)
    xb = fc.Format(cfg.cg.X.W + cfg.m_shift, cfg.cg.X.int_bits, True)
    want = {"oX": (new.xr, new.xi, cfg.cg.X), "oR": (new.rr, new.ri, cfg.cg.R),
            "oD": (new.pr, new.pi, cfg.bc),
            "oXB": (fc.fx.quantize(new.xr, xb, cfg.bc), fc.fx.quantize(new.xi, xb, cfg.bc), cfg.bc)}  # fmt: skip
    for name, (re, im, fmt) in want.items():
        a, nw = layout[name]
        got = np.asarray(sysm.mem._mem.read(a * W // 8, nw), np.uint64)
        gr, gi = from_words(got, k * n, fmt, lb, W)
        assert np.array_equal(gr.reshape(k, n), re) and np.array_equal(
            gi.reshape(k, n), im
        ), name


def test_vector_unit_wrapper_equalizes_a_chunk_by_affine_walks():
    """Step 4.2: the equalizer (``hw/programs.eq_programs``, ``r`` = 2) on a chunk of 3 tiles, one
    ``VecMemJob`` of 3 rounds.  ``Z`` and ``w`` sit in the spec's C-tile layout (tile ``j`` at
    ``base + j·wt``, stream ``r``'s ``U_pad x c`` block ``r·wb`` into it), so each load stream is an
    affine walk (``base + r·wb``, stride ``wt``); ``x̂`` goes out a tile at a time.  Every tile equals
    ``equalize_fixed`` (proven equal to ``vecunit.model.run_job`` in step 3.1) bit for bit.
    """
    from examples.ltbf_dse import ltbf_fixed_chain as fc
    from examples.ltbf_dse.hw import programs as pg
    from examples.ltbf_dse.hw.records import SRC_VEC
    from examples.ltbf_dse.hw.vec_wrap import VecMemJob
    from waveflow.linalg.lanes import from_words, nwords, to_words
    from waveflow.vecunit.asm import assemble

    ranges = {"H": 0.025, "G": 0.24, "BC": 0.08, "X": 6.8, "Y": 6.7, "Z": 8.5, "R": 0.07,
              "S": 0.02, "ps": 0.05, "rz": 0.18, "alpha": 200.0, "beta": 5.1, "w": 0.5, "y": 1.8,
              "xh": 1.8}  # fmt: skip
    cfg = fc.formats(ranges, 14)
    r, u_pad, c, tiles, lb = 2, 16, 8, 3, 16
    vu = pg.eq_config(cfg.eq, u_pad, c, L=8, r=r, word_bits=W, lane_bits=lb)
    words = assemble(vu, pg.eq_programs(vu, u_pad, c, r, tiles), tx_id=0)
    rng = np.random.default_rng(11)

    def ints(fmt, shift=0):
        lo, hi = -(1 << (fmt.W - 1)), 1 << (fmt.W - 1)
        return tuple(
            rng.integers(lo, hi, (tiles, r, u_pad, c)) >> shift for _ in range(2)
        )

    zr, zi = ints(cfg.eq.z)
    wr, wi = ints(cfg.eq.w, 2)
    wb = nwords(u_pad * c, lb, W)  # one stream's block
    wt = r * wb  # a C tile (Mmax = S = r·U_pad rows)

    def tiled(re, im, fmt):
        return np.concatenate([np.asarray(to_words(re[j, k], im[j, k], fmt, lb, W), np.uint64)
                               for j in range(tiles) for k in range(r)])  # fmt: skip

    layout, addr, image = {}, 0, []
    for name in ("load", "execute", "store"):
        layout[name] = (addr, len(words[name]))
        image.append(np.asarray(words[name], np.uint64))
        addr += len(words[name])
    for name, data in (("w", tiled(wr, wi, cfg.eq.w)), ("z", tiled(zr, zi, cfg.eq.z)),
                       ("xh", np.zeros(tiles * wb, np.uint64))):  # fmt: skip
        layout[name] = (addr, len(data))
        image.append(data)
        addr += len(data)
    image = np.concatenate(image)
    job = VecMemJob(tx_id=9, count=tiles, n_ld=2 * r, n_st=1,
                    l_addr=layout["load"][0], l_words=layout["load"][1],
                    x_addr=layout["execute"][0], x_words=layout["execute"][1],
                    s_addr=layout["store"][0], s_words=layout["store"][1])  # fmt: skip
    for q, (name, k) in enumerate((("w", 0), ("z", 0), ("w", 1), ("z", 1))):
        setattr(job, f"ld{q}_base", layout[name][0] + k * wb)
        setattr(job, f"ld{q}_stride", wt)
        setattr(job, f"ld{q}_words", wb)
    job.st0_base, job.st0_stride, job.st0_words = layout["xh"][0], wb, wb
    sysm = vec_system(vu, len(image), [np.asarray(job.serialize(word_bw=W), np.uint64)])
    sysm.mem._mem.write(0, image)
    resps = sysm.run()
    assert [(int(x.src), int(x.tx_id), int(x.status)) for x in resps] == [
        (SRC_VEC, 9, 0)
    ]
    a = layout["xh"][0]
    for j in range(tiles):
        got = np.asarray(sysm.mem._mem.read((a + j * wb) * W // 8, wb), np.uint64)
        gr, gi = from_words(got, u_pad * c, cfg.eq.xh, lb, W)
        xr, xi = fc.equalize_fixed(zr[j], zi[j], wr[j], wi[j], cfg.eq)
        assert np.array_equal(gr.reshape(u_pad, c), xr), j
        assert np.array_equal(gi.reshape(u_pad, c), xi), j


# -- the Ingest DMA ---------------------------------------------------------------------------------

ING_BASE = 0x8003_0000
T_SYM = 125e-6 / 14  # a symbol at 30 kHz


@dataclass
class OruSource(FreeRunMod):
    """The O-RU: a symbol every ``period``, antenna by antenna (one burst each), then nothing.  A
    symbol listed in ``gates`` waits for its event first."""

    symbols: list = field(default_factory=list)
    period: float = T_SYM
    gates: dict = field(default_factory=dict)
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=W)
        self.add_endpoint(self.m_out)

    def run_iter(self):
        for s, bursts in enumerate(self.symbols):
            if s in self.gates:
                yield self.gates[s]
            yield self.env.timeout(max(0.0, (s + 1) * self.period - self.env.now))
            for b in bursts:
                yield from self.m_out.write(b)
        yield self.env.event()  # done: wait forever


@dataclass
class IngestHost(SwHost):
    """Configures the Ingest DMA, then runs the test's ``script`` (a generator function of the host)."""

    cfg: object = None
    script: object = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))
    cpp_model: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=W)
        self.irq = {"qresp": self.add_irq("irq_qresp")}
        self.ctrl = self.status = self.qresp = None
        self.credit_addr = 0
        self.done = self.env.event()
        self.resps: list = []
        self.freed = 0

    def scenario_bursts(self):
        return []

    def take(self):
        r = yield from self.qresp.get_schema(Resp)
        self.resps.append(r)
        return r

    def free(self, n: int):
        """Return ``n`` chunks: the credit view takes the cumulative count."""
        self.freed += n
        yield from self.m.write(np.asarray([self.freed], np.uint64), self.credit_addr)

    def main(self):
        yield from self.ctrl.write(self.cfg)
        yield from self.script(self)
        self.done.succeed()


def ingest_system(n_ant: int, J: int, depth: int, n_sym: int, script, *, km=8, nm=8):
    """The Ingest DMA, the RespHub, Shared Mem at 0, the O-RU stand-in and a host on one crossbar;
    the grid is random ``Y`` in a 16-bit format.  ``want(s, c)`` is chunk ``c`` of symbol ``s`` in
    ``SystolicEngineMem``'s B layout (``block_major``), zero-padded past ``n_sc``."""
    from types import SimpleNamespace

    from examples.ltbf_dse.hw.ingest import IngestDev
    from examples.ltbf_dse.hw.records import IngestCfg
    from waveflow.linalg.lanes import to_words
    from waveflow.linalg.systolic_engine_mem import block_major

    n_sc, lb, fmt = 792, 16, reg(16, 4)
    rng = np.random.default_rng(9)
    gr = rng.integers(-(1 << 15), 1 << 15, (n_sym, n_ant, n_sc))
    gi = rng.integers(-(1 << 15), 1 << 15, (n_sym, n_ant, n_sc))
    symbols = [[np.asarray(to_words(gr[s, a], gi[s, a], fmt, lb, W), np.uint64)
                for a in range(n_ant)] for s in range(n_sym)]  # fmt: skip
    sim = Simulation()
    clk = Clock(freq=250e6)
    dma_cfg = {"word_bits": W, "Kmax": km, "Nmax": nm, "n_ant": n_ant, "J": J}
    dev = IngestDev(name="ing", sim=sim, clk=clk, dma_cfg=dma_cfg)
    dma = dev.dma
    hub = RespHub(name="hub", sim=sim, clk=clk, n_src=1, word_bits=W)
    oru = OruSource(name="oru", sim=sim, clk=clk, symbols=symbols)
    mem_words = depth * dma.slot_words
    mem = MemoryMod(name="mem", sim=sim, word_size=W, inline=False, clk=clk,
                    nwords_tot=mem_words, addr_unit=AddrUnit.byte)  # fmt: skip
    assert mem.alloc(mem_words) == 0
    cfg = IngestCfg(enable=1, ring_depth=depth, ring_base=0, chunk_words=dma.slot_words)
    host = IngestHost(name="host", sim=sim, clk=clk, cfg=cfg)
    idev = build_mm_device(dev, sim=sim, clk=clk, mem_dwidth=W, prefix="ing_")
    hdev = build_mm_device(hub, sim=sim, clk=clk, mem_dwidth=W, prefix="hub_")
    links = []
    for name, m, s in (("sym", oru.m_out, dev.s_sym), ("resp", dev.m_resp, hub.s_in0)):
        li = StreamIF(name=name, sim=sim, clk=clk, bitwidth=W)
        li.bind("master", m)
        li.bind("slave", s)
        links.append(li)
    masters = [host.m, dev.m_w]
    is_, ir = idev.ranges(ING_BASE)
    hs, hr = hdev.ranges(HUB_BASE)
    slaves = is_ + hs + [mem.s_mm]
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=len(masters),
                           nports_slave=len(slaves), bitwidth=W)  # fmt: skip
    for k, m in enumerate(masters):
        xbar.bind(f"master_{k}", m)
    for k, s in enumerate(slaves):
        xbar.bind(f"slave_{k}", s)
    assign_address_ranges(slaves, ir + hr + [(0, mem_words * W // 8)])
    imap = MemSlaveLayout.of(IngestDev, mem_dwidth=W).at(ING_BASE)
    hmap = MemSlaveLayout.of(RespHub, mem_dwidth=W).at(HUB_BASE)
    line = IrqIF(name="qresp_irq", sim=sim)
    line.bind("source", hdev.views["qresp"].m_irq)
    line.bind("sink", host.irq["qresp"])
    iad = BoundMemSlaveAdaptor(imap, host.m)
    host.ctrl, host.status = iad.stream_master("ctrl"), iad.status("ctrl")
    host.credit_addr = imap["credit"].base
    host.qresp = BoundMemSlaveAdaptor(hmap, host.m).stream_slave(
        "qresp", irq=host.irq["qresp"]
    )
    host.script = script
    dut = DutTop.of(
        "ltbf_ing_dut", sim, comps=[idev, hdev, mem, oru], ifs=[xbar, *links]
    )
    sysm = HwSystem(sim, dut, run_until=host.done, check=lambda s: host.resps)
    per = J * nm

    def want(s: int, c: int) -> np.ndarray:
        re = np.zeros((n_ant, per), np.int64)
        im = np.zeros((n_ant, per), np.int64)
        hi = min(n_sc, (c + 1) * per)
        re[:, : hi - c * per], im[:, : hi - c * per] = (
            gr[s][:, c * per : hi],
            gi[s][:, c * per : hi],
        )
        bm = block_major(re, im, fmt, km, nm, lane_bits=lb, word_bits=W, order="row")
        return np.asarray(bm, np.uint64).reshape(-1)

    def slot(k: int) -> np.ndarray:
        a = k * dma.slot_words * W // 8
        return np.asarray(mem._mem.read(a, dma.slot_words), np.uint64)

    return SimpleNamespace(sysm=sysm, host=host, oru=oru, dma=dma, want=want, slot=slot)


def test_ingest_dma_writes_chunks_in_the_systolic_b_layout():
    """Step 4.3: three symbols of a 16-antenna grid, antenna by antenna, through the Ingest DMA into
    a ring of two symbols' chunks (the host frees each chunk after its ``Resp``).  ``J = 4`` tiles of
    8 make 25 chunks a symbol, the last zero-padded.  Each chunk's slot holds the chunk's
    ``N_rx x J·Nmax`` matrix in ``SystolicEngineMem``'s B layout (panel ``(p, j)`` at
    ``(p·J + j)·wb``), one ``Resp`` (``src`` 2) arrives a chunk, in order, and the status counts them.
    """
    n_sym, depth = 3, 50
    ok: dict[int, bool] = {}

    def script(h):
        for k in range(n_sym * 25):
            r = yield from h.take()
            s, c = divmod(int(r.seq), 25)
            ok[int(r.seq)] = np.array_equal(t.slot(k % depth), t.want(s, c))
            yield from h.free(1)
        h.final = yield from h.status.read()

    t = ingest_system(16, 4, depth, n_sym, script)
    assert t.dma.n_chunks == 25 and t.dma.P == 2
    resps = t.sysm.run()
    assert [int(r.src) for r in resps] == [2] * (n_sym * 25)
    assert [int(r.seq) for r in resps] == list(range(n_sym * 25))
    assert len(ok) == n_sym * 25 and all(ok.values())
    assert (int(t.host.final.chunks), int(t.host.final.overruns)) == (n_sym * 25, 0)


def test_ingest_dma_drops_a_symbol_without_credit():
    """Flow control: a ring of one symbol's 11 chunks (``J = 9``) that the host does not free.
    Symbols 1 and 2 arrive without credit and are dropped and counted; once the host returns the
    11 credits, symbol 3 goes into the same slots with ``seq`` 33..43 (the gap marks the drop).
    """
    n_sym = 4
    ok: dict[int, bool] = {}

    def script(h):
        for k in range(11):
            r = yield from h.take()
            ok[int(r.seq)] = np.array_equal(t.slot(k), t.want(0, k))
        for _ in range(200):  # wait for the two drops
            st = yield from h.status.read()
            if int(st.overruns) == 2:
                break
            yield h.env.timeout(T_SYM / 4)
        h.mid = st
        yield from h.free(11)
        go.succeed()
        for k in range(11):
            r = yield from h.take()
            ok[int(r.seq)] = np.array_equal(t.slot(k), t.want(3, k))
        h.final = yield from h.status.read()

    t = ingest_system(8, 9, 11, n_sym, script)
    go = t.host.env.event()
    t.oru.gates[3] = go
    resps = t.sysm.run()
    assert [int(r.seq) for r in resps] == list(range(11)) + list(range(33, 44))
    assert len(ok) == 22 and all(ok.values())
    assert (int(t.host.mid.chunks), int(t.host.mid.overruns)) == (11, 2)
    assert (int(t.host.final.chunks), int(t.host.final.overruns)) == (22, 2)


# -- the system -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy,sector,form",
    [
        ("static", 1, "factored"),
        ("dynamic", 1, "factored"),
        ("static", 0, "factored"),
        ("static", 1, "explicit"),
    ],
)
def test_system_equals_the_fixed_chain(policy, sector, form, tmp_path):
    """Step 4.5 (AC9's configuration in pysim): drop 1, sector 1 (10 own UEs, 22 connected), N_rx =
    64, factored, ``t`` 3, ``r`` 1, ``c`` 8, ``Mmax`` 16, ``J`` 9, a ring of 2 symbols.  The host runs
    the scheduler over one long-term update, then one slot through the Ingest DMA (the O-RU paced at
    500 µs a symbol: one 64-bit port cannot carry 64 antennas at 8.9 µs).  Every job runs (no symbol
    dropped); ``G'`` in Shared Mem, the DMRS symbols' ``Z``, the weights ``w`` and every ``x̂`` tile
    equal the fixed-point chain's (``ltbf_fixed_chain``) bit for bit, under either policy.  Sector 0
    (5 own UEs: ``U_pad`` 8 under ``S`` = ``Mmax`` 16, padding rows in every tile) too, and the explicit
    form (``K = Ĥα'Ĥα'ᴴ`` once, from the SRS stage's ``Ĥα'ᴴ`` copy).  The replay
    file holds every host job's writes."""
    from examples.ltbf_dse.hw import system as sy

    inp = sy.inputs(
        sy.SysParams(policy=policy, sector=sector, form=form, sym_period=500e-6)
    )
    assert (inp.dims.U, inp.dims.u_pad, inp.dims.S) == (
        (10, 16, 16) if sector else (5, 8, 16)
    )
    hw, S, host = sy.build(inp)
    assert S.fits_gate  # Shared Mem within the RTL gate's 1 MiB (spec §5.2)
    hw.run()
    assert S.sch.finished and S.sch.overruns == 0 and S.sch.dropped == 0
    assert S.sch.error is None
    assert sy.check(S, host) == {"G'": True, "Z (DMRS)": True, "w": True, "x̂": True}
    n_host = sum(j.engine in ("py", "cpu") for j in [*S.lt.jobs, *S.rt.jobs])
    index = sy.write_replay(host, tmp_path / "replay")
    assert len(index["jobs"]) == n_host + 1  # and the programs' setup
    assert index["words"] == sum(n for j in index["jobs"] for _a, n, _o in j["writes"])
    assert {src for src, *_ in host.resp_log} == {0, 1, 2}
