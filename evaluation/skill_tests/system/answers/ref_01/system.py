"""The two devices, a memory and a host on one bus, as ``HwSystem(sim, dut)``.

::

    host (SwHost) --m--+                                   +-- scale: q_cmd, q_status   (ScaleDev's views)
    scale.m_rd_mem ----+--[ AXIMMCrossBarIF  xbar ]--------+-- chk:   q_chk, q_sum      (Checksum's views)
    scale.m_wr_mem ----+                                   +-- mem:   MemoryMod (DDR)

The DUT is the two devices (each built from its class's ``mm_views`` by ``build_mm_device``), the
memory and the crossbar; the host is the testbench, derived.  Every wait of the host is on a queue's
interrupt (four ``IrqIF`` lines); nothing polls.

Per job the host writes the input buffer into memory, sends the scale command, waits for its status
(the scaler echoes it only once the output is stored), reads the output back, sends it to the checksum
device and waits for the checksum.  One job at a time: the status and result queues can never fill.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.arrayutils import array, read_array
from waveflow.hw.clock import Clock
from waveflow.hw.hw_system import DutTop, HwSystem
from waveflow.hw.irq import IrqIF, IrqIFSink
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import AddrUnit, MemoryMod
from waveflow.hw.mm_device import build_mm_device
from waveflow.hw.mm_host import BoundMemSlaveAdaptor, BusReader
from waveflow.simulation.simulation import Simulation
from waveflow.sw import SwHost

from checksum import ChkCmd, ChkResp, checksum
from devices import Checksum, ScaleDev
from scale_msgs import DW, I16, NMAX, Code, ScaleCmd, ScaleStatus, nwords, scale

SCALE_BASE, CHK_BASE = 0x0000, 0x4000
MEM_BASE, MEM_WORDS = 0x10_0000, 8192
WB = DW // 8
VIEWS = ("q_cmd", "q_status", "q_chk", "q_sum")


@dataclass
class Job:
    tx_id: int
    x: np.ndarray
    gain: int
    shift: int
    src: int = 0                       # word addresses on the bus
    dst: int = 0
    n_claimed: int | None = None

    @property
    def n(self) -> int:
        return len(self.x) if self.n_claimed is None else self.n_claimed

    def cmd(self) -> ScaleCmd:
        return ScaleCmd(tx_id=self.tx_id, n=self.n, src=self.src, dst=self.dst, gain=self.gain,
                        shift=self.shift)


def jobs_for(kind: str = "test") -> list[Job]:
    rng = np.random.default_rng({"test": 21, "error": 22}[kind])

    def x(n):
        return rng.integers(-32768, 32767, n).astype(np.int16)

    jobs = [Job(0, x(70), 7, 2), Job(1, x(2048), -3, 0), Job(2, x(3), 1, 0), Job(3, x(512), 300, 6)]
    if kind == "error":
        jobs = [jobs[0], Job(1, x(4), 1, 0, n_claimed=NMAX + 7), jobs[2]]
    src, dst = MEM_BASE // WB, MEM_BASE // WB + MEM_WORDS // 2
    for j in jobs:
        j.src, j.dst = src, dst
        src, dst = src + nwords(len(j.x)), dst + nwords(len(j.x))
    return jobs


def golden(jobs: list[Job]) -> list[dict]:
    """What the host must end with: per job answered, its status and checksum; nothing after an
    error."""
    out = []
    for j in jobs:
        if j.n > NMAX:
            out.append(dict(tx_id=j.tx_id, code=Code.TOO_LONG))
            break
        y, nsat = scale(j.x, j.gain, j.shift)
        out.append(dict(tx_id=j.tx_id, code=Code.OK, nsat=nsat, y=y, sum=checksum(y)))
    return out


@dataclass
class Host(SwHost):
    jobs: list = field(default_factory=list)
    clk: Clock = field(default_factory=lambda: Clock(freq=200e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=DW)
        self.irq: dict[str, IrqIFSink] = {v: self.add_irq(f"irq_{v}") for v in VIEWS}
        self.ep: dict = {}                              # set by the wiring
        self.bus = BusReader(self.m)
        self.done = self.env.event()
        self.results: list[dict] = []

    def scenario_bursts(self) -> list[np.ndarray]:
        """One burst per job, ``[the scale command's words | the input words]``: what the host's C++
        twin would replay at RTL (the twin is not written here)."""
        return [np.concatenate([np.asarray(j.cmd().serialize(word_bw=DW), dtype=np.uint64),
                                np.asarray(array(I16, j.x).serialize(word_bw=DW), dtype=np.uint64)])
                for j in self.jobs]

    def main(self):
        for j in self.jobs:
            if j.n <= NMAX:
                yield from self.bus.write(array(I16, j.x).serialize(word_bw=DW), j.src * WB)
            yield from self.ep["q_cmd"].write(j.cmd())
            st = yield from self.ep["q_status"].get_schema(ScaleStatus)
            r = dict(tx_id=int(st.tx_id), code=Code(int(st.code)))
            self.results.append(r)
            if r["code"] != Code.OK:
                break                                   # fatal: the scaler answers nothing more
            r["nsat"] = int(st.nsat)
            words = yield from self.bus.read(nwords(j.n), j.dst * WB)
            r["y"] = np.asarray(read_array(words, I16, word_bw=DW, shape=j.n).val)
            yield from self.ep["q_chk"].write(ChkCmd(tx_id=j.tx_id, n=j.n))
            yield from self.ep["q_chk"].write(array(I16, r["y"]).serialize(word_bw=DW))
            cr = yield from self.ep["q_sum"].get_schema(ChkResp)
            assert int(cr.tx_id) == j.tx_id and int(cr.n) == j.n
            r["sum"] = int(cr.sum)
        self.done.succeed()


def make_system(kind: str = "test") -> HwSystem:
    jobs = jobs_for(kind)
    sim = Simulation()
    clk = Clock(freq=200e6)
    scale_k = ScaleDev(name="scale", sim=sim, clk=clk)
    chk_k = Checksum(name="chk", sim=sim, clk=clk)
    mem = MemoryMod(name="mem", sim=sim, word_size=DW, inline=False, clk=clk, nwords_tot=MEM_WORDS,
                    addr_unit=AddrUnit.byte)
    mem.alloc(MEM_WORDS)
    host = Host(name="host", sim=sim, jobs=jobs, clk=clk)

    scale_dev = build_mm_device(scale_k, sim=sim, clk=clk, mem_dwidth=DW, prefix="scale_")
    chk_dev = build_mm_device(chk_k, sim=sim, clk=clk, mem_dwidth=DW, prefix="chk_")
    ss, sr = scale_dev.ranges(SCALE_BASE)
    cs, cr = chk_dev.ranges(CHK_BASE)
    masters = [host.m, scale_k.m_rd_mem, scale_k.m_wr_mem]
    slaves = ss + cs + [mem.s_mm]
    ranges = sr + cr + [(MEM_BASE, MEM_WORDS * WB)]
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=len(masters),
                           nports_slave=len(slaves), bitwidth=DW)
    for k, m in enumerate(masters):
        xbar.bind(f"master_{k}", m)
    for k, s in enumerate(slaves):
        xbar.bind(f"slave_{k}", s)
    assign_address_ranges(slaves, ranges)

    for dev, base, views in ((scale_dev, SCALE_BASE, ("q_cmd", "q_status")),
                             (chk_dev, CHK_BASE, ("q_chk", "q_sum"))):
        mm = BoundMemSlaveAdaptor(dev.layout.at(base), host.m)
        for v in views:
            line = IrqIF(name=f"{v}_irq", sim=sim)
            line.bind("source", dev.views[v].m_irq)
            line.bind("sink", host.irq[v])
            into = v in ("q_cmd", "q_chk")
            host.ep[v] = (mm.stream_master if into else mm.stream_slave)(v, irq=host.irq[v])

    dut = DutTop.of("pair_dut", sim, comps=[scale_dev, chk_dev, mem], ifs=[xbar])
    sysm = HwSystem(sim, dut, run_until=host.done, check=lambda s: host.results,
                    fresh=lambda: make_system(kind))
    sysm.host, sysm.jobs = host, jobs
    return sysm


__all__ = ["Host", "Job", "jobs_for", "golden", "make_system"]
