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

from examples.ltbf_dse.hw.records import SRC_SYS, Resp
from examples.ltbf_dse.hw.resp_hub import RespHub
from examples.ltbf_dse.hw.sys_wrap import SysWrap
from examples.ltbf_dse.ltbf_fixed import reg
from waveflow.hw.clock import Clock
from waveflow.hw.hw_system import DutTop, HwSystem
from waveflow.hw.interface import StreamIF
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
