"""unit.py — the SIMD core in pysim: loaders, the core and storers joined by block queues
(``plans/simdcore.md`` Stage 4).

``SimdCore`` is a composite of free-running tasks:

* **SimdLoad** (one an input port ``s_in<p>``): each burst is one block -- a header word
  :func:`block_header` (window, ``k``, ``n``) and ``k x n`` elements row-major, two 32-bit elements a
  64-bit word -- landed as the next block of that window's queue (a ping-pong of two blocks);
* **SimdExec**: a job a firing.  Its program burst on ``s_prog`` (:func:`job_words`: header, ``a0``..
  arguments, the instructions two to a word) runs on the interpreter
  (:class:`~waveflow.simdcore.model.Core`) an instruction at a time, timed by its cycle model; at a
  ``wf.qpop`` it releases the window's previous block and waits for the next, at a ``wf.qpush`` it
  hands the block to the port's storer.  One status word per job on ``m_status``
  (:func:`status_word`); an error is fatal: the status, then the task takes nothing more;
* **SimdStore** (one an output port ``s_out<p>``): each pushed block as one burst, a
  :func:`resp_word` (``tx_id``, window, ``k``, ``n``) then its elements.

The protocol is ``docs/guide/systems/command_response.md``'s: a job's command is its program burst,
its results bursts headed by a response that echoes ``tx_id``, a status per job on its own stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    FramedStreamIFSlave,
    SobIFMaster,
    SobIFSlave,
    StreamOfBlocksIF,
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

def _block_type(cfg: SimdCoreConfig, n: int) -> type[DataArray]:
    return DataArray.specialize(element_type=IntField.specialize(bitwidth=32, signed=False),
                                max_shape=(cfg.vlmax(32) * n,), member_name="elems")


@dataclass
class _Part(FreeRunMod):
    cfg: SimdCoreConfig = None  # type: ignore[assignment]  # required: checked below
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))
    build_fields: ClassVar[tuple[str, ...]] = ("cfg",)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.cfg is None:
            raise ValueError(f"{self.name}: a SIMD core needs its SimdCoreConfig")

    def _ep(self, cls, name: str, bits: int = WORD_BITS):
        ep = cls(name=f"{self.name}_{name}", sim=self.sim, bitwidth=int(bits), has_tlast=True)
        self.add_endpoint(ep)
        setattr(self, name, ep)
        return ep

    def _blocks(self, cls, name: str, n: int):
        ep = cls(name=f"{self.name}_{name}", sim=self.sim, element_type=_block_type(self.cfg, n))
        self.add_endpoint(ep)
        setattr(self, name, ep)
        return ep

    def _halt(self) -> ProcessGen[None]:
        yield self.env.event()


@dataclass
class SimdLoad(_Part):
    """The loader of one input port: each burst is a block of the window its header names."""

    port: int = 0
    cpp_kernel_name: ClassVar[str | None] = None
    build_fields: ClassVar[tuple[str, ...]] = ("cfg", "port")

    def __post_init__(self) -> None:
        super().__post_init__()
        self._ep(FramedStreamIFSlave, "s_in")
        self.wins = [i for i, w in enumerate(self.cfg.windows) if w.direction == "in" and w.port == self.port]
        self.q = {i: self._blocks(SobIFMaster, f"q{i}", self.cfg.windows[i].n) for i in self.wins}

    def run_iter(self) -> ProcessGen[None]:
        words = np.asarray((yield from self.s_in.get()), np.uint64).reshape(-1)
        head = int(words[0])
        win, k, n = head & 0xFF, (head >> 8) & 0xFFFF, (head >> 24) & 0xFF
        if win not in self.q:
            raise ValueError(f"{self.name}: a block for window {win}, not on port {self.port}")
        blk = yield from self.q[win].acquire_write()
        blk.payload = unpack_elems(words[1:], k, n)
        yield from self.q[win].commit_write(blk)


@dataclass
class SimdStore(_Part):
    """The storer of one output port: each pushed block as a response word and its elements."""

    port: int = 0
    cpp_kernel_name: ClassVar[str | None] = None
    build_fields: ClassVar[tuple[str, ...]] = ("cfg", "port")

    def __post_init__(self) -> None:
        super().__post_init__()
        nmax = max([w.n for w in self.cfg.windows if w.direction == "out" and w.port == self.port] or [1])
        self._blocks(SobIFSlave, "q", nmax)
        self._ep(FramedStreamIFMaster, "s_out")

    def run_iter(self) -> ProcessGen[None]:
        blk = yield from self.q.acquire_read()
        tx_id, win, data = blk.payload
        yield from self.s_out.write(out_burst(tx_id, win, data))
        yield from self.q.release_read()


@dataclass
class SimdExec(_Part):
    """The core: a job a firing (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        cfg = self.cfg
        self._ep(FramedStreamIFSlave, "s_prog")
        self._ep(FramedStreamIFMaster, "m_status")
        self.qi = {i: self._blocks(SobIFSlave, f"qi{i}", w.n) for i, w in enumerate(cfg.windows)
                   if w.direction == "in"}
        oports = sorted({w.port for w in cfg.windows if w.direction == "out"})
        self.qo = {p: self._blocks(SobIFMaster, f"qo{p}",
                                   max(w.n for w in cfg.windows if w.direction == "out" and w.port == p))
                   for p in oports}
        self.halted = False
        self.results: list[JobResult] = []
        self.status_times: list[float] = []

    def _until(self, t0: float, cycles: float) -> ProcessGen[None]:
        t = t0 + cycles * self.clk.period
        if t > self.env.now:
            yield self.timeout(t - self.env.now)

    def run_iter(self) -> ProcessGen[None]:
        if self.halted:
            yield from self._halt()
        cfg = self.cfg
        words = np.asarray((yield from self.s_prog.get()), np.uint64).reshape(-1)
        tx_id, prog, args, budget = parse_job(words)
        # the job's clock starts when its program's first word arrived (a word a cycle)
        t0 = self.env.now - (words.size - 1) * self.clk.period
        core = Core(cfg)
        core.begin(prog, args=args, budget=budget)
        core.tm.external = True
        held: dict[int, object] = {}
        while True:
            d = core.peek()
            if d is not None and d.spec.sem == "qpop" and d.imm in self.qi:
                q = int(d.imm)
                yield from self._until(t0, core.tm.ts)
                if q in held:
                    yield from self.qi[q].release_read()
                blk = yield from self.qi[q].acquire_read()
                held[q] = blk
                core.inq[cfg.windows[q].name].append(blk.payload)
                core.tm.arrival = int(np.ceil((self.env.now - t0) / self.clk.period - 1e-9))
            pushes = sum(len(v) for v in core.outq.values())
            more = core.step()
            if sum(len(v) for v in core.outq.values()) > pushes:  # a wf.qpush ran
                w = cfg.windows[int(d.imm)]
                yield from self._until(t0, core.tm.last_push)
                out = yield from self.qo[w.port].acquire_write()
                out.payload = (tx_id, int(d.imm), core.outq[w.name][-1])
                yield from self.qo[w.port].commit_write(out)
            if not more:
                break
        r = core.result()
        self.results.append(r)
        for q in list(held):
            yield from self.qi[q].release_read()
        yield from self._until(t0, r.cycles)
        yield from self.m_status.write(np.asarray([status_word(tx_id, Status[r.status], r.steps)], np.uint64))
        self.status_times.append(self.env.now)  # a job's interval in pysim: the record, not the RTL
        if r.status != "OK":
            self.halted = True


@dataclass
class SimdCore(_Part):
    """The SIMD core: a job's program on ``s_prog``; blocks in on ``s_in<p>`` and out on ``s_out<p>``;
    a status per job on ``m_status`` (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "simdcore"

    def __post_init__(self) -> None:
        super().__post_init__()
        cfg = self.cfg
        iports = sorted({w.port for w in cfg.windows if w.direction == "in"})
        oports = sorted({w.port for w in cfg.windows if w.direction == "out"})
        self.loads = [SimdLoad(name=f"{self.name}_load{p}", sim=self.sim, clk=self.clk, cfg=cfg, port=p)
                      for p in iports]
        self.exe = SimdExec(name=f"{self.name}_exec", sim=self.sim, clk=self.clk, cfg=cfg)
        self.stores = [SimdStore(name=f"{self.name}_store{p}", sim=self.sim, clk=self.clk, cfg=cfg, port=p)
                       for p in oports]
        for comp in (*self.loads, self.exe, *self.stores):
            self.add_comp(comp)
        for ld in self.loads:
            for i, ep in ld.q.items():
                self._link(f"qi{i}", ep, self.exe.qi[i])
        for st in self.stores:
            self._link(f"qo{st.port}", self.exe.qo[st.port], st.q)
        for p, ld in zip(iports, self.loads):
            setattr(self, f"s_in{p}", ld.s_in)
        for p, st in zip(oports, self.stores):
            setattr(self, f"s_out{p}", st.s_out)
        self.s_prog, self.m_status = self.exe.s_prog, self.exe.m_status
        self.boundary = ([f"s_in{p}" for p in iports] + ["s_prog", "m_status"]
                         + [f"s_out{p}" for p in oports])

    def _link(self, name, master, slave) -> StreamOfBlocksIF:
        iface = StreamOfBlocksIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                                 element_type=master.element_type, depth=2)
        iface.bind("master", master)
        iface.bind("slave", slave)
        self.add_if(iface)
        return iface

    # -- scenarios: an application subclasses the core and gives its jobs ---------------------------
    def jobs(self, kind: str = "test") -> list[Job]:
        raise ValueError(f"{type(self).__name__}: no workload {kind!r} (an application gives its jobs)")

    def workload(self, kind: str = "test") -> dict:
        return job_bursts(self.cfg, self.jobs(kind))

    def golden(self, kind: str = "test") -> dict:
        return golden_bursts(self.cfg, self.jobs(kind))
