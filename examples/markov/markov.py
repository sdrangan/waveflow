"""markov.py — a two-state Markov chain simulator: two kernels that talk over the bus.

The gate of ``plans/mm_credit_stream.md``.  Deliberately not useful hardware: each kernel is tiny, so
the example is about the **links** -- a host that sends commands and sleeps on an interrupt, and two
kernels joined by a credit stream routed over a shared bus::

    host  -> gen.qcmd     : MkvCmd(tx_id, n, x0, seed, p01, p10, dstaddr)     (queue in, room IRQ)
    gen   -> chain.qu     : MkvCmd | u[0..n-1]          (a CreditStreamIF, routed over the bus)
    chain -> gen.u_crd    : cumulative words consumed   (its credit half, routed back)
    chain -> mem          : x[0..n-1] at dstaddr        (its MemWStream's m_axi bursts)
    chain -> chain.qresp  : MkvResp(tx_id, n, ones)     (forwarded by the writer once x is stored;
                                                         queue out, data IRQ -> host)
    host  <- mem          : x

* :class:`MarkovGen` (kernel 1) draws ``u[k]`` from an xorshift32 PRNG seeded per command, keeping
  the top :data:`UBITS` bits, and forwards the command header in front of them.
* :class:`MarkovChain` (kernel 2) is a composite: :class:`ChainCore` runs the chain and hands ``x``
  and the response to the framework's in-band memory writer.  The core runs the chain: ``P(0 -> 1) = p01``, ``P(1 -> 0) = p10``, both in
  Q\\ :data:`UBITS`, so a step is two integer compares and a select::

      t0 = u < p01          # from state 0: go to 1
      t1 = u >= p10         # from state 1: stay at 1
      x' = x ? t1 : t0

  Both compares depend only on ``u``; the dependency carried from step to step is the 2:1 select,
  which is why the HLS body is expected to reach one sample per cycle.

**Flow control, with no polling anywhere.**  The kernel-to-kernel link is a
:class:`~waveflow.hw.reverse_stream.CreditStreamIF` -- the generator writes only what its credit
says fits, so it never stalls the bus -- routed over the crossbar by
:class:`~waveflow.hw.mm_credit.MmCreditStreamIF`.  The chain batches its credit (one bus write per
half queue, and one when it drains).  The host keeps at most :data:`MAX_IN_FLIGHT` jobs outstanding,
which bounds the response queue (end-to-end admission); it waits on the command queue's room
interrupt and the response queue's data interrupt.

The same two kernel classes also run joined **directly** (``link="direct"``): a plain
``CreditStreamIF`` between them, the host's ends joined straight to the kernels.  The kernels' code is
identical; only the wiring differs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.irq import IrqIF, IrqIFSink
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.mem_stream import MemWCmd, MemWStream
from waveflow.hw.memory import AddrUnit, MemoryMod
from waveflow.hw.mm_credit import MmCreditStreamIF
from waveflow.hw.mm_device import CreditIn, QueueIn, QueueOut, build_mm_device
from waveflow.hw.mm_host import BoundMemSlaveAdaptor, MemSlaveLayout
from waveflow.hw.reverse_stream import CreditStreamIF, CreditStreamSlaveIF, FramedCreditStreamMasterIF
from waveflow.simulation.simobj import SimObj
from waveflow.simulation.simulation import Simulation

DW = 64
#: Bits of each uniform draw, and the Q-format of p01 / p10.
UBITS = 16
#: Samples per transfer between the kernels (and per memory write): 16 words of u, 8 words of x.
CHUNK = 64
#: The chain's input queue, in words: eight chunks.  Credit is returned every 32 words consumed.
#: The credit window (QDEPTH - 1 words) must cover the bandwidth-delay product of the link -- a word's
#: round trip through the forward FIFO, the store-and-forward writer, the queue, up to CRD_EVERY - 1
#: unreported words and the credit path back -- or credit, not compute, sets the rate.  Measured at RTL:
#: 64 words throttled the generator at every job start and starved the chain (2015 cycles).
QDEPTH = 128
CRD_EVERY = 32
#: The command and response queues, in words.
CDEPTH = 16
RDEPTH = 16
#: The FIFO between the generator and its forward bus writer, in words: two chunks, so the generator
#: fills one while the writer bursts the other (the writer is store-and-forward).
FWD_DEPTH = 2 * (CHUNK // 4)
#: Jobs the host keeps outstanding.
MAX_IN_FLIGHT = 2

U8 = IntField.specialize(bitwidth=8, signed=False, include_dir="include")
U16 = IntField.specialize(bitwidth=16, signed=False, include_dir="include")
U32 = IntField.specialize(bitwidth=32, signed=False)
U64 = IntField.specialize(bitwidth=64, signed=False)


class MkvCmd(DataList):
    """One job: three 64-bit words, scalars only.  The generator forwards it to the chain."""

    elements = {
        "n": {"schema": U32, "description": "steps to run"},
        "tx_id": {"schema": U16, "description": "the host's job id, echoed in the response"},
        "x0": {"schema": U16, "description": "initial state, 0 or 1"},
        "seed": {"schema": U32, "description": "xorshift32 seed (0 is replaced by 1)"},
        "p01": {"schema": U16, "description": "P(0 -> 1) in Q16"},
        "p10": {"schema": U16, "description": "P(1 -> 0) in Q16"},
        "dstaddr": {"schema": U64, "description": "bus address x[0..n-1] is written to"},
    }


class MkvResp(DataList):
    """The chain's response to one job: two 64-bit words."""

    elements = {
        "n": {"schema": U32, "description": "steps run"},
        "ones": {"schema": U32, "description": "how many x[k] were 1"},
        "tx_id": {"schema": U16, "description": "echo of the job's tx_id"},
    }


# ---------------------------------------------------------------------------
# The golden
# ---------------------------------------------------------------------------

def xorshift32(seed: int, n: int) -> np.ndarray:
    """``n`` successive xorshift32 states after *seed* (0 is replaced by 1).  Sequential by nature:
    each state is a function of the last."""
    s = (int(seed) & 0xFFFF_FFFF) or 1
    out = np.empty(int(n), dtype=np.uint32)
    for k in range(int(n)):
        s ^= (s << 13) & 0xFFFF_FFFF
        s ^= s >> 17
        s ^= (s << 5) & 0xFFFF_FFFF
        out[k] = s
    return out


def uniforms(seed: int, n: int) -> np.ndarray:
    """The draws: the top :data:`UBITS` bits of each state."""
    return (xorshift32(seed, n) >> (32 - UBITS)).astype(np.uint16)


def chain_golden(u: np.ndarray, x0: int, p01: int, p10: int) -> np.ndarray:
    """The chain: ``x[k]`` is the state after step *k*.  The compares are vectorized; the select
    carried from step to step is the one sequential part."""
    u = np.asarray(u, dtype=np.int64)
    t0 = u < int(p01)
    t1 = u >= int(p10)
    x = np.empty(len(u), dtype=np.uint8)
    s = int(x0) & 1
    for k in range(len(u)):
        s = int(t1[k]) if s else int(t0[k])
        x[k] = s
    return x


def markov_golden(cmd: dict) -> np.ndarray:
    return chain_golden(uniforms(cmd["seed"], cmd["n"]), cmd["x0"], cmd["p01"], cmd["p10"])


# ---------------------------------------------------------------------------
# The kernels
# ---------------------------------------------------------------------------

@dataclass
class MarkovGen(FreeRunMod):
    """Kernel 1: one firing = one job.  Takes a command, forwards it, then sends ``n`` draws in
    chunks of :data:`CHUNK`, each written only when the credit says it fits."""

    cpp_kernel_name: ClassVar[str | None] = "markov_gen"
    cpp_namespace: ClassVar[str | None] = "markov_gen_impl"

    mm_views: ClassVar[tuple] = (
        QueueIn("qcmd", port="s_cmd", depth=CDEPTH),
        CreditIn("u_crd", port="m_u"),
    )

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: Cycles per draw (the HLS body's II).
    proc_ii: int = 1
    #: Cycles a chunk costs beyond its draws: the credit check before the loop, the loop's fill and
    #: drain.  MEASURED at RTL (markov_xsi probes: a chunk leaves every 71 cycles for 64 draws).
    chunk_overhead: int = 7

    def kernel_task(self):
        """The hand-written HLS body, ``include/markov_gen_task.h``: the twin of :meth:`run_iter`,
        credit accounting included.  ``m_u`` is two ports (forward, credit) in that order."""
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("markov_gen_task", "markov_gen_task.h", ("s_cmd", "m_u"),
                          template_args=(DW, QDEPTH, CRD_EVERY))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=DW,
                                   has_tlast=True)
        # Framed: over the bus, the writer frames each write as one queue-in packet, and at RTL only a
        # TLAST pin can say where a write ends.
        self.m_u = FramedCreditStreamMasterIF(name=f"{self.name}_m_u", sim=self.sim, bitwidth=DW)
        for ep in (self.s_cmd, self.m_u):
            self.add_endpoint(ep)
        self.njobs = 0

    def run_iter(self):
        cmd = yield from self.s_cmd.get_schema(MkvCmd)
        yield from self.m_u.write(cmd)
        n = int(cmd.n)
        u = uniforms(int(cmd.seed), n)
        for k0 in range(0, n, CHUNK):
            c = min(CHUNK, n - k0)
            # The credit check, then one draw per proc_ii (markov_gen_task.h).
            yield self.timeout((self.chunk_overhead + c * self.proc_ii) * self.clk.period)
            yield from self.m_u.write(array(U16, u[k0:k0 + c]))
        self.njobs += 1


@dataclass
class ChainCore(FreeRunMod):
    """The chain itself (a leaf, streams only): one firing = one job.  Reads the forwarded command,
    then the draws chunk by chunk, and runs the chain.  Its output is the in-band frame stream a
    :class:`~waveflow.hw.mem_stream.MemWStream` takes: per chunk ``[MemWCmd(addr, len) | x words]``,
    then ``[MemWCmd(len=0, fwd_bursts=1) | MkvResp]`` -- the writer stores ``x`` and only then forwards
    the response, so a host holding a response knows its ``x`` is in memory."""

    cpp_kernel_name: ClassVar[str | None] = "markov_chain_core"
    cpp_namespace: ClassVar[str | None] = "markov_chain_core_impl"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    proc_ii: int = 1
    #: Cycles a chunk costs beyond its steps: the MemWCmd words, the step loop's fill and drain (its
    #: depth is 5), the credit offer.  MEASURED at RTL (markov_xsi probes: x leaves for memory every
    #: 79 cycles for 64 steps).
    chunk_overhead: int = 15

    def kernel_task(self):
        """The hand-written HLS body, ``include/markov_chain_core_task.h``: the twin of
        :meth:`run_iter`.  ``s_u`` is two ports (forward in, credit out) in that order."""
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("markov_chain_core_task", "markov_chain_core_task.h", ("s_u", "m_x"),
                          template_args=(DW, CRD_EVERY))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_u = CreditStreamSlaveIF(name=f"{self.name}_s_u", sim=self.sim, bitwidth=DW,
                                       crd_every=CRD_EVERY)
        self.m_x = StreamIFMaster(name=f"{self.name}_m_x", sim=self.sim, bitwidth=DW, has_tlast=True)
        for ep in (self.s_u, self.m_x):
            self.add_endpoint(ep)
        self.njobs = 0

    def run_iter(self):
        cmd = yield from self.s_u.get_schema(MkvCmd)
        n, x = int(cmd.n), int(cmd.x0) & 1
        dst, ones = int(cmd.dstaddr), 0
        for k0 in range(0, n, CHUNK):
            c = min(CHUNK, n - k0)
            u = yield from self.s_u.get_array(U16, c)
            xs = chain_golden(np.asarray(u.val), x, int(cmd.p01), int(cmd.p10))
            x = int(xs[-1])
            ones += int(xs.sum())
            yield self.timeout((self.chunk_overhead + c * self.proc_ii) * self.clk.period)
            # Chunk k0 lands at dstaddr + k0 bytes (one U8 per sample, eight to a word); MemWCmd's
            # address is a word index (the writer's m_axi base is 0).
            xw = array(U8, xs).serialize(word_bw=DW)
            yield from self.m_x.write(MemWCmd(addr=(dst + k0) // 8, len=len(xw), fwd_bursts=0))
            yield from self.m_x.write(np.asarray(xw, dtype=np.uint64))
        yield from self.m_x.write(MemWCmd(addr=0, len=0, fwd_bursts=1))
        yield from self.m_x.write(MkvResp(n=n, ones=ones, tx_id=int(cmd.tx_id)))
        self.njobs += 1


@dataclass
class MarkovChain(FreeRunMod):
    """Kernel 2: a composite of :class:`ChainCore` and the framework's in-band memory writer
    (:class:`~waveflow.hw.mem_stream.MemWStream`), joined by a framed stream.  Its boundary: the
    credit stream in (``s_u``), the writer's ``m_axi`` (``m_mem``) and the forwarded responses
    (``m_resp``)."""

    cpp_kernel_name: ClassVar[str | None] = "markov_chain"
    cpp_namespace: ClassVar[str | None] = "markov_chain_impl"

    mm_views: ClassVar[tuple] = (
        QueueIn("qu", port="s_u", depth=QDEPTH),
        QueueOut("qresp", port="m_resp", depth=RDEPTH),
    )

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.core = ChainCore(name=f"{self.name}_core", sim=self.sim, clk=self.clk)
        # The response is forwarded through the writer: MkvResp is its whole forward buffer.
        self.writer = MemWStream(name=f"{self.name}_w", sim=self.sim, mem_dwidth=DW, inband=True,
                                 emit_done=True, done_framed=False,
                                 max_fwd_words=int(MkvResp.nwords_per_inst(DW)), clk=self.clk)
        for c in (self.core, self.writer):
            self.add_comp(c)
        self._x_if = StreamIF(name=f"{self.name}_x_if", sim=self.sim, clk=self.clk, bitwidth=DW,
                              framed=True)
        self._x_if.bind("master", self.core.m_x)
        self._x_if.bind("slave", self.writer.s_in)
        self.add_if(self._x_if)
        self.boundary = ["s_u_fwd", "s_u_crd", "m_mem", "m_resp"]
        self.s_u = self.core.s_u
        self.m_mem = self.writer.m_mem
        self.m_resp = self.writer.s_done

    @property
    def njobs(self) -> int:
        return self.core.njobs


#: Each kernel type's address layout (offsets within its slave).
GEN_LAYOUT = MemSlaveLayout.of(MarkovGen, mem_dwidth=DW)
CHAIN_LAYOUT = MemSlaveLayout.of(MarkovChain, mem_dwidth=DW)
#: Where the system places them.
GEN_BASE, CHAIN_BASE = 0x0000, 0x4000
#: The shared memory: one 4 KB window -- in RTL a BRAM behind the adaptor's front (which echoes AXI IDs,
#: as a four-master crossbar's responses need).  Each job's x gets a REGION_BYTES region, so a job is at
#: most REGION_BYTES steps and at most MEM_SPAN // REGION_BYTES jobs fit.
MEM_BASE, MEM_SPAN = 0x10_0000, 0x1000
REGION_BYTES = 0x200


# ---------------------------------------------------------------------------
# The host and the system
# ---------------------------------------------------------------------------

def default_jobs(njobs: int = 4, n: int = 300, seed: int = 11) -> list[dict]:
    rng = np.random.default_rng(seed)
    jobs = []
    for j in range(njobs):
        p01 = int(rng.integers(2000, 20000))
        p10 = int(rng.integers(2000, 20000))
        jobs.append(dict(tx_id=j, n=int(n), x0=j & 1, seed=int(rng.integers(1, 2**32 - 1)),
                         p01=p01, p10=p10))
    return jobs


@dataclass
class MarkovHost(SimObj):
    """The host program: a **writer** that sends each job's command once a slot is free, and a
    **reader** that takes each response, reads that job's ``x`` from memory and frees the slot.
    At most :data:`MAX_IN_FLIGHT` jobs are outstanding.  Nothing polls: over the bus the command
    endpoint sleeps on the command queue's room interrupt and the response endpoint on the response
    queue's data interrupt.  ``x`` is read with ``mem_read`` -- a bus read (memory-mapped) or a
    direct read of the memory (direct)."""

    jobs: list = field(default_factory=list)
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=DW)
        self.qcmd: StreamIFMaster | None = None
        self.qresp: StreamIFSlave | None = None
        self.irq: dict[str, IrqIFSink] = {}
        self.mem: MemoryMod | None = None
        self.mem_bus_base: int | None = None       # None: read the memory directly
        self.done = self.env.event()
        self.results: dict[int, dict] = {}
        self._slots = MAX_IN_FLIGHT
        self._slot_free = None

    def _dst(self, j: int) -> int:
        """Job *j*'s region, memory-local: ``REGION_BYTES`` per job."""
        return j * REGION_BYTES

    def _writer(self):
        for j, job in enumerate(self.jobs):
            while self._slots == 0:
                self._slot_free = self.env.event()
                yield self._slot_free
            self._slots -= 1
            dst = self._dst(j) + (self.mem_bus_base or 0)
            yield from self.qcmd.write(MkvCmd(**job, dstaddr=dst))

    def _reader(self):
        for _ in self.jobs:
            resp = yield from self.qresp.get_schema(MkvResp)
            j = int(resp.tx_id)
            n = int(resp.n)
            if self.mem_bus_base is None:
                x = np.asarray(self.mem.read_array(self._dst(j), U8, n))
            else:
                x = np.asarray((yield from self.m.read_array(U8, n, self.mem_bus_base + self._dst(j),
                                                             word_bw=DW)))
            self.results[j] = dict(n=n, ones=int(resp.ones), x=np.asarray(x, dtype=np.uint8),
                                   t=self.env.now)
            self._slots += 1
            if self._slot_free is not None and not self._slot_free.triggered:
                self._slot_free.succeed()
        self.done.succeed()

    def run_proc(self):
        self.env.process(self._writer())
        yield from self._reader()


@dataclass
class MarkovSystem:
    """Everything wired.  ``link="mm"``: host, generator writer, chain credit writer and chain memory
    master on one crossbar, with the two kernels' views and the memory as slaves.  ``link="direct"``:
    a ``CreditStreamIF`` between the kernels and plain streams to the host."""

    jobs: list
    link: str = "mm"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    xbar_latency: float = 4
    xbar_travel: float = 2

    def __post_init__(self) -> None:
        if self.link not in ("mm", "direct"):
            raise ValueError(f"link must be 'mm' or 'direct', got {self.link!r}")
        sim = self.sim = Simulation()
        self.gen = MarkovGen(name="gen", sim=sim, clk=self.clk)
        self.chain = MarkovChain(name="chain", sim=sim, clk=self.clk)
        self.mem = MemoryMod(name="mem", sim=sim, word_size=DW, inline=False, clk=self.clk,
                             nwords_tot=MEM_SPAN // 8, addr_unit=AddrUnit.byte)
        self.host = MarkovHost(name="host", sim=sim, jobs=list(self.jobs), clk=self.clk)
        self.host.mem = self.mem
        if len(self.jobs) > MEM_SPAN // REGION_BYTES or any(j["n"] > REGION_BYTES for j in self.jobs):
            raise ValueError(f"at most {MEM_SPAN // REGION_BYTES} jobs of at most {REGION_BYTES} steps")
        for j in range(len(self.jobs)):          # each job's x region, at the host's offsets
            a = self.mem.alloc(REGION_BYTES // 8)
            assert a == self.host._dst(j), (a, self.host._dst(j))
        if self.link == "direct":
            self._wire_direct()
        else:
            self._wire_mm()

    def _stream(self, name, master, slave, depth):
        si = StreamIF(name=name, sim=self.sim, clk=self.clk, bitwidth=DW, depth=depth)
        si.bind(ep_name="master", endpoint=master)
        si.bind(ep_name="slave", endpoint=slave)

    def _wire_direct(self) -> None:
        sim, host = self.sim, self.host
        host.qcmd = StreamIFMaster(name="host_qcmd", sim=sim, bitwidth=DW, has_tlast=True)
        host.qresp = StreamIFSlave(name="host_qresp", sim=sim, bitwidth=DW, has_tlast=False)
        self._stream("k_cmd", host.qcmd, self.gen.s_cmd, CDEPTH)
        self._stream("k_resp", self.chain.m_resp, host.qresp, RDEPTH)
        self.u_link = CreditStreamIF(name="u", sim=sim, clk=self.clk, bitwidth=DW, depth=QDEPTH)
        self.u_link.bind("master", self.gen.m_u)
        self.u_link.bind("slave", self.chain.s_u)
        # The chain's memory master straight to the memory.
        from waveflow.hw.memif import DirectMMIF
        self.mem_link = DirectMMIF(name="k_mem", sim=sim, clk=self.clk)
        self.mem_link.bind("master", self.chain.m_mem)
        self.mem_link.bind("slave", self.mem.s_mm)

    def _wire_mm(self) -> None:
        sim, clk, host = self.sim, self.clk, self.host
        self.gen_dev = build_mm_device(self.gen, sim=sim, clk=clk, mem_dwidth=DW, prefix="gen_")
        self.chain_dev = build_mm_device(self.chain, sim=sim, clk=clk, mem_dwidth=DW,
                                         prefix="chain_")
        self.u_link = MmCreditStreamIF(name="u", sim=sim, clk=clk, bitwidth=DW, fwd_depth=FWD_DEPTH)
        self.u_link.bind("master", self.gen.m_u)
        self.u_link.bind("slave", self.chain.s_u)
        masters = [host.m, *self.u_link.bus_masters(), self.chain.m_mem]
        gs, gr = self.gen_dev.ranges(GEN_BASE)
        cs, cr = self.chain_dev.ranges(CHAIN_BASE)
        slaves = gs + cs + [self.mem.s_mm]
        ranges = gr + cr + [(MEM_BASE, MEM_SPAN)]
        self.xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=len(masters),
                                    nports_slave=len(slaves), bitwidth=DW,
                                    latency_init=self.xbar_latency,
                                    latency_travel=self.xbar_travel)
        for k, m in enumerate(masters):
            self.xbar.bind(f"master_{k}", m)
        for k, s in enumerate(slaves):
            self.xbar.bind(f"slave_{k}", s)
        assign_address_ranges(slaves, ranges)
        gmap, cmap = GEN_LAYOUT.at(GEN_BASE), CHAIN_LAYOUT.at(CHAIN_BASE)
        self.u_link.place(qin=cmap["qu"], crd_in=gmap["u_crd"])
        # The host: the command queue (room IRQ) and the response queue (data IRQ).
        for dev, name in ((self.gen_dev, "qcmd"), (self.chain_dev, "qresp")):
            v = dev.views[name]
            line = IrqIF(name=f"{v.name}_irq", sim=sim)
            line.bind("source", v.m_irq)
            host.irq[name] = IrqIFSink(name=f"host_{name}_irq", sim=sim)
            line.bind("sink", host.irq[name])
        host.qcmd = BoundMemSlaveAdaptor(gmap, host.m).stream_master("qcmd", irq=host.irq["qcmd"])
        host.qresp = BoundMemSlaveAdaptor(cmap, host.m).stream_slave("qresp", irq=host.irq["qresp"])
        host.mem_bus_base = MEM_BASE

    def run(self) -> dict[int, dict]:
        self.sim.run_sim(until=self.host.done)
        return self.host.results


def demo(link: str = "mm", njobs: int = 4, n: int = 300) -> dict:
    jobs = default_jobs(njobs, n)
    sysm = MarkovSystem(jobs=jobs, link=link)
    res = sysm.run()
    ok = all(np.array_equal(res[j["tx_id"]]["x"], markov_golden(j)) for j in jobs)
    return {"system": sysm, "jobs": jobs, "results": res, "bit_exact": ok,
            "cycles": sysm.sim.env.now / sysm.clk.period}


if __name__ == "__main__":
    for link in ("direct", "mm"):
        r = demo(link)
        print(f"{link:6s} bit-exact={r['bit_exact']}  cycles={r['cycles']:.0f}")
