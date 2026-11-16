"""unit.py — the SIMD core in pysim (``plans/simdcore.md`` Stages 4 and 5).

**Structure** (what the generated top instantiates; ``plans/simdcore_records.md`` Stage 5):
``SimdCore`` is a composite of two free-running tasks joined by two streams --

* **SimdScalar** (``s_prog``, ``m_status``): the scalar task, decoding and issuing; the vector
  instructions on ``vq`` (:data:`~waveflow.simdcore.hls.VQ_BITS`-bit words), the scalar results back on
  ``rq``;
* **SimdVector** (``s_in<p>``, ``s_out<p>``): the vector task with the ports' loaders and storers.

**pysim** is the composite's own flat model (the two tasks are passive, as the tile model of
``waveflow/linalg/systolic_engine_mem.py``): per input port a loader, each burst one block -- a header
word :func:`block_header` (window, ``k``, ``n``) and ``k x n`` elements row-major, two 32-bit elements a
64-bit word -- landed as the next block of that window's queue (a ping-pong of two blocks); the core, a
job at a time -- its program burst on ``s_prog`` (:func:`job_words`: header, ``a0``.. arguments, the
instructions two to a word) on the interpreter (:class:`~waveflow.simdcore.model.Core`) an instruction
at a time, timed by its cycle model; at a ``wf.qpop`` it releases the window's previous block and waits
for the next, at a ``wf.qpush`` it hands the block to the port's storer; one status word per job on
``m_status`` (:func:`status_word`), an error fatal (the status, then nothing more) -- and per output
port a storer, each pushed block one burst, a :func:`resp_word` (``tx_id``, window, ``k``, ``n``) then
its elements.

The protocol is ``docs/guide/systems/command_response.md``'s: a job's command is its program burst,
its results bursts headed by a response that echoes ``tx_id``, a status per job on its own stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    FramedStreamIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.simdcore.asm import assemble
from waveflow.simdcore.config import SimdCoreConfig
from waveflow.simdcore.model import Core, JobResult
from waveflow.simulation.simobj import ProcessGen
from waveflow.utils.burst_io import StreamBurst

WORD_BITS = 64


class Status(IntEnum):
    """A job's status (records D11: every error is fatal)."""

    OK = 0
    ILLEGAL = 1
    ADDR = 2
    BUDGET = 3
    PC = 4
    EMPTY = 5  # the interpreter only: a wf.qpop with no block (the hardware would wait)


# --- the words ---------------------------------------------------------------------------------------

def job_words(tx_id: int, program, args=(), budget: int = 0) -> np.ndarray:
    """A job's program burst: ``tx_id[15:0] ninstr[31:16] nargs[35:32] budget[63:36]``, then ``nargs``
    words of ``a0..`` (low 32 bits), then the instructions two a word (the first in the low half)."""
    words = assemble(program).words if isinstance(program, str) else [int(w) for w in program]
    args = list(args)
    if len(args) > 8 or len(words) >= 1 << 16 or budget >= 1 << 28:
        raise ValueError("job: at most 8 arguments, 65535 instructions, a 28-bit budget")
    head = (tx_id & 0xFFFF) | (len(words) << 16) | (len(args) << 32) | (budget << 36)
    pairs = [(words[i] | ((words[i + 1] if i + 1 < len(words) else 0) << 32))
             for i in range(0, len(words), 2)]
    return np.asarray([head] + [a & 0xFFFFFFFF for a in args] + pairs, dtype=np.uint64)


def parse_job(words) -> tuple[int, list[int], list[int], int]:
    """``(tx_id, program words, args, budget)`` from a program burst."""
    w = [int(x) for x in np.asarray(words, np.uint64).reshape(-1)]
    head = w[0]
    tx, n, nargs, budget = head & 0xFFFF, (head >> 16) & 0xFFFF, (head >> 32) & 0xF, head >> 36
    args = w[1:1 + nargs]
    prog: list[int] = []
    for x in w[1 + nargs:]:
        prog += [x & 0xFFFFFFFF, x >> 32]
    return tx, prog[:n], args, budget


def block_header(window: int, k: int, n: int) -> int:
    return (window & 0xFF) | ((k & 0xFFFF) << 8) | ((n & 0xFF) << 24)


def resp_word(tx_id: int, window: int, k: int, n: int) -> int:
    return (tx_id & 0xFFFF) | ((window & 0xFF) << 16) | ((k & 0xFFFF) << 24) | ((n & 0xFF) << 40)


def status_word(tx_id: int, status: int, steps: int = 0) -> int:
    return (tx_id & 0xFFFF) | ((int(status) & 0xFF) << 16) | ((steps & 0xFFFFFFFF) << 32)


def pack_elems(blk: np.ndarray) -> np.ndarray:
    """A ``k x n`` block of 32-bit elements, row-major, two a 64-bit word (the first low)."""
    e = np.asarray(blk, np.uint64).reshape(-1) & np.uint64(0xFFFFFFFF)
    if e.size % 2:
        e = np.concatenate([e, np.zeros(1, np.uint64)])
    return e[0::2] | (e[1::2] << np.uint64(32))


def unpack_elems(words, k: int, n: int) -> np.ndarray:
    w = np.asarray(words, np.uint64).reshape(-1)
    e = np.empty(2 * w.size, np.uint64)
    e[0::2], e[1::2] = w & np.uint64(0xFFFFFFFF), w >> np.uint64(32)
    return e[:k * n].astype(np.uint32).reshape(k, n)


def block_burst(window: int, blk: np.ndarray) -> np.ndarray:
    blk = np.asarray(blk)
    if blk.ndim == 1:
        blk = blk[:, None]
    k, n = blk.shape
    return np.concatenate([np.asarray([block_header(window, k, n)], np.uint64), pack_elems(blk)])


def out_burst(tx_id: int, window: int, blk: np.ndarray) -> np.ndarray:
    k, n = blk.shape
    return np.concatenate([np.asarray([resp_word(tx_id, window, k, n)], np.uint64), pack_elems(blk)])


# --- the jobs ----------------------------------------------------------------------------------------

@dataclass
class Job:
    """One job: its program (text or words), ``a0..`` arguments, budget and input blocks by window
    (``k x n`` arrays of 32-bit elements, in the order the program pops them)."""

    tx_id: int
    program: object
    args: tuple = ()
    budget: int = 0
    inputs: dict = field(default_factory=dict)


def run_jobs(cfg: SimdCoreConfig, jobs: list[Job]) -> list[JobResult]:
    """The jobs on the interpreter, up to and including the first that fails (an error is fatal)."""
    out = []
    for j in jobs:
        core = Core(cfg)
        out.append(core.run(j.program, inputs={k: list(v) for k, v in j.inputs.items()},
                            args=list(j.args), budget=j.budget))
        if out[-1].status != "OK":
            break
    return out


def job_bursts(cfg: SimdCoreConfig, jobs: list[Job]) -> dict[str, list[StreamBurst]]:
    """The harness's stimulus: a program burst per job on ``s_prog``; on each input port, the blocks of
    its windows in the order the jobs pop them (from an interpreter run)."""
    out: dict[str, list] = {"s_prog": []}
    ports = sorted({w.port for w in cfg.windows if w.direction == "in"})
    for p in ports:
        out[f"s_in{p}"] = []
    for j, r in zip(jobs, run_jobs(cfg, jobs)):
        out["s_prog"].append(StreamBurst(job_words(j.tx_id, j.program, j.args, j.budget), True))
        nxt = {k: iter(v) for k, v in j.inputs.items()}
        for name in r.pops:
            w = cfg.window(name)
            out[f"s_in{w.port}"].append(StreamBurst(block_burst(cfg.window_index(name), next(nxt[name])), True))
    for j in jobs[len(out["s_prog"]):]:  # after a fatal job: its program still goes out
        out["s_prog"].append(StreamBurst(job_words(j.tx_id, j.program, j.args, j.budget), True))
    return out


def golden_bursts(cfg: SimdCoreConfig, jobs: list[Job]) -> dict[str, list[np.ndarray]]:
    """Every output port's bursts and ``m_status``, from the interpreter; nothing after a failed job."""
    out: dict[str, list] = {f"s_out{p}": [] for p in sorted({w.port for w in cfg.windows if w.direction == "out"})}
    out["m_status"] = []
    for j, r in zip(jobs, run_jobs(cfg, jobs)):
        for name, blk in r.pushes:
            w = cfg.window(name)
            out[f"s_out{w.port}"].append(out_burst(j.tx_id, cfg.window_index(name), blk))
        out["m_status"].append(np.asarray([status_word(j.tx_id, Status[r.status], r.steps)], np.uint64))
    return out


# --- the tasks ---------------------------------------------------------------------------------------

@dataclass
class _Part(FreeRunMod):
    cfg: SimdCoreConfig = None  # type: ignore[assignment]  # required: checked below
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))
    build_fields: ClassVar[tuple[str, ...]] = ("cfg",)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.cfg is None:
            raise ValueError(f"{self.name}: a SIMD core needs its SimdCoreConfig")

    def _ep(self, cls, name: str, bits: int = WORD_BITS, framed: bool = True):
        kw = {"has_tlast": True} if framed else {}
        ep = cls(name=f"{self.name}_{name}", sim=self.sim, bitwidth=int(bits), **kw)
        self.add_endpoint(ep)
        setattr(self, name, ep)
        return ep

    def _ports(self, direction: str) -> list[int]:
        return sorted({w.port for w in self.cfg.windows if w.direction == direction})

    def _kernel(self, fn: str, signature: tuple[str, ...], framed: tuple[str, ...]):
        from waveflow.hw.mem_stream import KernelTask
        from waveflow.simdcore.hls import header_name

        return KernelTask(fn, header_name(self.cfg), signature, framed=framed)


@dataclass
class SimdScalar(_Part):
    """The scalar task: ``s_prog`` in, ``m_status`` out; vector instructions on ``m_vq``, results on
    ``s_rq`` (its body: ``simdcore_scalar_<id>``, waveflow/build/simdcore_core.h)."""

    cpp_kernel_name: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        from waveflow.simdcore.hls import VQ_BITS

        self._ep(FramedStreamIFSlave, "s_prog")
        self._ep(FramedStreamIFMaster, "m_status")
        self._ep(StreamIFMaster, "m_vq", VQ_BITS, framed=False)
        self._ep(StreamIFSlave, "s_rq", 32, framed=False)

    def kernel_task(self):
        from waveflow.simdcore.hls import cfg_id

        return self._kernel(f"simdcore_scalar_{cfg_id(self.cfg)}", ("s_prog", "m_status", "m_vq", "s_rq"),
                            ("s_prog", "m_status"))

    def run_iter(self) -> ProcessGen[None]:
        # Passive: SimdCore's flat model does the work through these ports (see the module doc).
        yield self.env.event()


@dataclass
class SimdVector(_Part):
    """The vector task and the ports' loaders and storers (its body: ``sc_vector_<id>``)."""

    cpp_kernel_name: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        from waveflow.simdcore.hls import VQ_BITS

        self._ep(StreamIFSlave, "s_vq", VQ_BITS, framed=False)
        self._ep(StreamIFMaster, "m_rq", 32, framed=False)
        if self.cfg.vdiv:
            from waveflow.simdcore.hls import dq_bits

            self._ep(StreamIFMaster, "m_dq", dq_bits(self.cfg), framed=False)
            self._ep(StreamIFSlave, "s_drq", 32 * self.cfg.L, framed=False)
        for p in self._ports("in"):
            self._ep(FramedStreamIFSlave, f"s_in{p}")
        for p in self._ports("out"):
            self._ep(FramedStreamIFMaster, f"s_out{p}")

    def kernel_task(self):
        from waveflow.simdcore.hls import cfg_id

        io = tuple(f"s_in{p}" for p in self._ports("in")) + tuple(f"s_out{p}" for p in self._ports("out"))
        div = ("m_dq", "s_drq") if self.cfg.vdiv else ()
        return self._kernel(f"sc_vector_{cfg_id(self.cfg)}", ("s_vq", "m_rq", *div, *io), io)

    def run_iter(self) -> ProcessGen[None]:
        yield self.env.event()  # passive (see SimdScalar)


@dataclass
class SimdDivider(_Part):
    """The divider task of a ``vdiv`` build (its body: ``sc_divider_<id>``): a word of ``L`` dividends
    and divisors a cycle in, ``L`` results out."""

    cpp_kernel_name: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        from waveflow.simdcore.hls import dq_bits

        self._ep(StreamIFSlave, "s_dq", dq_bits(self.cfg), framed=False)
        self._ep(StreamIFMaster, "m_drq", 32 * self.cfg.L, framed=False)

    def kernel_task(self):
        from waveflow.simdcore.hls import cfg_id

        return self._kernel(f"sc_divider_{cfg_id(self.cfg)}", ("s_dq", "m_drq"), ())

    def run_iter(self) -> ProcessGen[None]:
        yield self.env.event()  # passive (see SimdScalar)


@dataclass
class SimdCore(_Part):
    """The SIMD core: a job's program on ``s_prog``; blocks in on ``s_in<p>`` and out on ``s_out<p>``;
    a status per job on ``m_status`` (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "simdcore"

    def __post_init__(self) -> None:
        super().__post_init__()
        from waveflow.simdcore.hls import VQ_BITS

        cfg = self.cfg
        iports, oports = self._ports("in"), self._ports("out")
        self.scal = SimdScalar(name=f"{self.name}_scalar", sim=self.sim, clk=self.clk, cfg=cfg)
        self.vec = SimdVector(name=f"{self.name}_vector", sim=self.sim, clk=self.clk, cfg=cfg)
        kids = [self.scal, self.vec]
        links = [("vq", self.scal.m_vq, self.vec.s_vq, VQ_BITS, cfg.vq_depth),
                 ("rq", self.vec.m_rq, self.scal.s_rq, 32, 2)]
        if cfg.vdiv:
            from waveflow.simdcore.hls import dq_bits

            self.div = SimdDivider(name=f"{self.name}_divider", sim=self.sim, clk=self.clk, cfg=cfg)
            kids.append(self.div)
            links += [("dq", self.vec.m_dq, self.div.s_dq, dq_bits(cfg), 2),
                      ("drq", self.div.m_drq, self.vec.s_drq, 32 * cfg.L, cfg.vlen // 32 // cfg.L)]
        for comp in kids:
            self.add_comp(comp)
            comp.make_passive()
        for name, m, s, bits, depth in links:
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=bits,
                             depth=depth)
            iface.bind("master", m)
            iface.bind("slave", s)
            self.add_if(iface)
        for p in iports:
            setattr(self, f"s_in{p}", getattr(self.vec, f"s_in{p}"))
        for p in oports:
            setattr(self, f"s_out{p}", getattr(self.vec, f"s_out{p}"))
        self.s_prog, self.m_status = self.scal.s_prog, self.scal.m_status
        # Boundary names pair with the children's unwired ports in walk order: the scalar task's
        # program and status, then the vector task's inputs and outputs.
        self.boundary = (["s_prog", "m_status"] + [f"s_in{p}" for p in iports]
                         + [f"s_out{p}" for p in oports])
        self.halted = False
        self.results: list[JobResult] = []
        self.status_times: list[float] = []

    # -- the flat pysim -------------------------------------------------------------------------------
    def run_proc(self):
        import simpy

        env, cfg = self.sim.env, self.cfg
        self._free = {i: simpy.Container(env, capacity=2, init=2) for i, w in enumerate(cfg.windows)
                      if w.direction == "in"}
        self._full = {i: simpy.Store(env) for i in self._free}
        self._ofree = {p: simpy.Container(env, capacity=2, init=2) for p in self._ports("out")}
        self._ofull = {p: simpy.Store(env) for p in self._ports("out")}
        for p in self._ports("in"):
            env.process(self._loader(p))
        for p in self._ports("out"):
            env.process(self._storer(p))
        return self._core_forever()

    def _loader(self, p: int) -> ProcessGen[None]:
        ep = getattr(self.vec, f"s_in{p}")
        while True:
            words = np.asarray((yield from ep.get()), np.uint64).reshape(-1)
            head = int(words[0])
            win, k, n = head & 0xFF, (head >> 8) & 0xFFFF, (head >> 24) & 0xFF
            if win not in self._free or self.cfg.windows[win].port != p:
                raise ValueError(f"{self.name}: a block for window {win}, not an input of port {p}")
            yield self._free[win].get(1)
            yield self._full[win].put(unpack_elems(words[1:], k, n))

    def _storer(self, p: int) -> ProcessGen[None]:
        ep = getattr(self.vec, f"s_out{p}")
        while True:
            tx_id, win, data = yield self._ofull[p].get()
            yield from ep.write(out_burst(tx_id, win, data))
            yield self._ofree[p].put(1)

    def _until(self, t0: float, cycles: float) -> ProcessGen[None]:
        t = t0 + cycles * self.clk.period
        if t > self.env.now:
            yield self.timeout(t - self.env.now)

    def _core_forever(self) -> ProcessGen[None]:
        while not self.halted:
            yield from self._job()
        yield self.env.event()

    def _job(self) -> ProcessGen[None]:
        cfg = self.cfg
        words = np.asarray((yield from self.scal.s_prog.get()), np.uint64).reshape(-1)
        tx_id, prog, args, budget = parse_job(words)
        # the job's clock starts when its program's first word arrived (a word a cycle)
        t0 = self.env.now - (words.size - 1) * self.clk.period
        core = Core(cfg)
        core.begin(prog, args=args, budget=budget)
        core.tm.external = True
        held: set[int] = set()
        while True:
            d = core.peek()
            if d is not None and d.spec.sem == "qpop" and d.imm in self._free:
                q = int(d.imm)
                yield from self._until(t0, core.tm.ts)
                if q in held:
                    yield self._free[q].put(1)
                blk = yield self._full[q].get()
                held.add(q)
                core.inq[cfg.windows[q].name].append(blk)
                core.tm.arrival = int(np.ceil((self.env.now - t0) / self.clk.period - 1e-9))
            pushes = sum(len(v) for v in core.outq.values())
            more = core.step()
            if sum(len(v) for v in core.outq.values()) > pushes:  # a wf.qpush ran
                w = cfg.windows[int(d.imm)]
                yield from self._until(t0, core.tm.last_push)
                yield self._ofree[w.port].get(1)
                yield self._ofull[w.port].put((tx_id, int(d.imm), core.outq[w.name][-1]))
            if not more:
                break
        r = core.result()
        self.results.append(r)
        for q in held:
            yield self._free[q].put(1)
        yield from self._until(t0, r.cycles)
        yield from self.scal.m_status.write(np.asarray([status_word(tx_id, Status[r.status], r.steps)], np.uint64))
        self.status_times.append(self.env.now)  # a job's interval in pysim: the record, not the RTL
        if r.status != "OK":
            self.halted = True

    # -- scenarios: an application subclasses the core and gives its jobs ---------------------------
    def jobs(self, kind: str = "test") -> list[Job]:
        raise ValueError(f"{type(self).__name__}: no workload {kind!r} (an application gives its jobs)")

    def workload(self, kind: str = "test") -> dict:
        return job_bursts(self.cfg, self.jobs(kind))

    def golden(self, kind: str = "test") -> dict:
        return golden_bursts(self.cfg, self.jobs(kind))
