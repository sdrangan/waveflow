"""An adaptive peak picker as a pipeline of kernels: a broadcast, a statistics stage, a detect stage.

One input stream carries each job: its command, then its samples::

    s_in     : PkCmd(tx_id, n, mult) | x[0..n-1]          two bursts; int16 samples, two a word
    m_out    : PkResp(tx_id, count)  | idx[0..count-1]    the index burst only when count > 0
    m_status : PkStatus(tx_id, code)                       one per job, after its results

The composite::

    s_in --> [StreamBroadcast(2)] --copy 0--> [Stats] --PkThr--> [Detect] --> m_out, m_status
                                  --copy 1------------------------^

* **Stats** needs every sample before it knows the threshold, ``T = (sum |x| * mult) // (8 n)``; it
  sends one ``PkThr(tx_id, thr, code)`` per job -- **a token per job**, even for a bad command, so Detect
  is never left waiting.
* **Detect** needs ``T`` before it can compare a sample, and ``T`` comes only after Stats has read the
  whole job.  The broadcast's outputs move in lockstep, so if Detect waited for ``T`` with its copy of
  the samples unread, its link would fill, the broadcast would stall, Stats would never finish: a
  deadlock for any job longer than the link.  So **Detect drains its own copy first** (into a local
  buffer of ``NMAX`` samples), then waits for ``T``.  Its link then needs no particular depth.
* **The last stage answers**: Detect writes the response, the indices and the status.  The command
  travels with the data: Detect reads its own copy of it, and checks that ``PkThr`` names the same job.

Errors are fatal: ``n`` outside ``1..NMAX`` is reported by Stats in the token and by Detect in the
status, and both stop.  A short sample burst: Stats reports ``SHORT`` in the token, Detect answers
``count = 0`` and the same code, and both stop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import array  # noqa: F401  (tests use pk.array)
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave, StreamIF
from waveflow.streams import StreamBroadcast
from waveflow.utils.burst_io import StreamBurst

W = 32
NMAX = 1024
#: Internal links, in words.  Detect drains its copy before it waits, so none of them must hold a job:
#: the copies only smooth the stages' rates, the token link holds a few tokens.
COPY_DEPTH = 16
THR_DEPTH = 4

U16 = IntField.specialize(bitwidth=16, signed=False)
U32 = IntField.specialize(bitwidth=32, signed=False)
I16 = IntField.specialize(bitwidth=16, signed=True)


class Code(IntEnum):
    OK = 0
    BAD_LEN = 1     # n == 0 or n > NMAX
    SHORT = 2       # the samples ended before n


CodeField = EnumField.specialize(enum_type=Code)


class PkCmd(DataList):
    elements = {"tx_id": {"schema": U16}, "n": {"schema": U16},
                "mult": {"schema": U16, "description": "threshold multiplier, in eighths of the mean"}}


class PkThr(DataList):
    """Stats -> Detect, one per job."""
    elements = {"tx_id": {"schema": U16}, "thr": {"schema": U32}, "code": {"schema": CodeField}}


class PkResp(DataList):
    elements = {"tx_id": {"schema": U16}, "count": {"schema": U16}}


class PkStatus(DataList):
    elements = {"tx_id": {"schema": U16}, "code": {"schema": CodeField}}


# --- the golden ---------------------------------------------------------------------------------

def threshold(x: np.ndarray, mult: int) -> int:
    return int(np.abs(x.astype(np.int64)).sum() * int(mult) // (8 * len(x)))


def peaks(x: np.ndarray, thr: int) -> np.ndarray:
    return np.flatnonzero(np.abs(x.astype(np.int64)) > thr).astype(np.uint16)


def _halt(mod):
    yield mod.event()                 # never triggered: the stage stops until a reset


# --- the stages ---------------------------------------------------------------------------------

@dataclass
class Stats(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "pk_stats"
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W)
        self.m_thr = FramedStreamIFMaster(name=f"{self.name}_m_thr", sim=self.sim, bitwidth=W)
        for ep in (self.s_in, self.m_thr):
            self.add_endpoint(ep)

    def run_iter(self):
        cmd = yield from self.s_in.get_schema(PkCmd)
        n = int(cmd.n)
        if not 1 <= n <= NMAX:
            yield from self.m_thr.write(PkThr(tx_id=cmd.tx_id, thr=0, code=Code.BAD_LEN))
            yield from _halt(self)
        x = (yield from self.s_in.get_array(I16, n)).val
        code = Code.OK if len(x) == n else Code.SHORT
        thr = threshold(x, cmd.mult) if len(x) else 0
        yield from self.m_thr.write(PkThr(tx_id=cmd.tx_id, thr=thr, code=code))
        if code != Code.OK:
            yield from _halt(self)

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": stimulus(scenario(kind))}


@dataclass
class Detect(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "pk_detect"
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W)
        self.s_thr = FramedStreamIFSlave(name=f"{self.name}_s_thr", sim=self.sim, bitwidth=W)
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=W)
        self.m_status = FramedStreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=W)
        for ep in (self.s_in, self.s_thr, self.m_out, self.m_status):
            self.add_endpoint(ep)

    def run_iter(self):
        cmd = yield from self.s_in.get_schema(PkCmd)
        n = int(cmd.n)
        x = np.zeros(0, dtype=np.int16)
        if 1 <= n <= NMAX:
            # Drain this copy first (the broadcast's keep-consuming rule), then wait for the token.
            x = (yield from self.s_in.get_array(I16, n)).val
        tok = yield from self.s_thr.get_schema(PkThr)
        assert tok.tx_id == cmd.tx_id, "the token names another job: the stages are out of step"
        code = Code(int(tok.code))
        idx = peaks(x, int(tok.thr)) if code == Code.OK else np.zeros(0, dtype=np.uint16)
        yield from self.m_out.write(PkResp(tx_id=cmd.tx_id, count=len(idx)))
        if len(idx):
            yield from self.m_out.write_array(U16, len(idx), array(U16, idx))
        yield from self.m_status.write(PkStatus(tx_id=cmd.tx_id, code=code))
        if code != Code.OK:
            yield from _halt(self)

    def workload(self, kind: str = "test") -> dict:
        jobs = scenario(kind)
        return {"s_in": stimulus(jobs), "s_thr": [_burst(t) for t in tokens(jobs)]}


@dataclass
class PeakPicker(FreeRunMod):
    """The composite kernel: no body, three children, three internal links."""

    cpp_kernel_name: ClassVar[str | None] = "peak_picker"
    #: The detect stage's class (a test swaps in wrong ones to show what the counts catch).
    detect_cls: ClassVar[type] = Detect
    stats_cls: ClassVar[type] = Stats
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.bcast = StreamBroadcast(name=f"{self.name}_bcast", sim=self.sim, n_out=2, bitwidth=W,
                                     clk=self.clk)
        self.stats = self.stats_cls(name=f"{self.name}_stats", sim=self.sim, clk=self.clk)
        self.detect = self.detect_cls(name=f"{self.name}_detect", sim=self.sim, clk=self.clk)
        for c in (self.bcast, self.stats, self.detect):
            self.add_comp(c)
        for name, master, slave, depth in (
                ("copy_stats", self.bcast.m_out_0, self.stats.s_in, COPY_DEPTH),
                ("copy_detect", self.bcast.m_out_1, self.detect.s_in, COPY_DEPTH),
                ("thr", self.stats.m_thr, self.detect.s_thr, THR_DEPTH)):
            si = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=W,
                          framed=True, depth=depth)
            si.bind("master", master)
            si.bind("slave", slave)
            self.add_if(si)
        self.boundary = ["s_in", "m_out", "m_status"]
        self.s_in = self.bcast.s_in
        self.m_out = self.detect.m_out
        self.m_status = self.detect.m_status

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": stimulus(scenario(kind))}

    def golden(self, kind: str = "test") -> dict:
        out, status = expected(scenario(kind))
        return {"m_out": out, "m_status": status}


# --- scenarios ----------------------------------------------------------------------------------

@dataclass
class Job:
    tx_id: int
    x: np.ndarray
    mult: int
    n_claimed: int | None = None     # a command naming another n than the samples sent

    @property
    def n(self) -> int:
        return len(self.x) if self.n_claimed is None else self.n_claimed


def _burst(msg) -> StreamBurst:
    return StreamBurst(np.asarray(msg.serialize(word_bw=W), dtype=np.uint64))


def stimulus(jobs: list[Job]) -> list[StreamBurst]:
    out = []
    for j in jobs:
        out.append(_burst(PkCmd(tx_id=j.tx_id, n=j.n, mult=j.mult)))
        out.append(StreamBurst(np.asarray(array(I16, j.x).serialize(word_bw=W), dtype=np.uint64)))
    return out


def scenario(kind: str = "test") -> list[Job]:
    rng = np.random.default_rng({"test": 5, "bad_len": 6, "short": 7}[kind])

    def x(n, spikes=()):
        v = rng.integers(-200, 200, n).astype(np.int16)
        for i in spikes:
            v[i] = 9000 if i % 2 else -9000
        return v

    jobs = [Job(0, x(64, (3, 40)), mult=24),
            Job(1, x(NMAX, (0, 511, 1023)), mult=40),     # a whole job: far longer than any link
            Job(2, x(5), mult=255),                       # no peak: a response with count 0
            Job(3, x(1), mult=8)]
    if kind == "bad_len":
        jobs = [jobs[0], Job(1, x(4), mult=8, n_claimed=NMAX + 1), jobs[2]]
    if kind == "short":
        jobs = [jobs[0], Job(1, x(10, (2,)), mult=24, n_claimed=20), jobs[2]]
    return jobs


def tokens(jobs: list[Job]) -> list[PkThr]:
    out = []
    for j in jobs:
        if not 1 <= j.n <= NMAX:
            out.append(PkThr(tx_id=j.tx_id, thr=0, code=Code.BAD_LEN))
            break
        code = Code.OK if len(j.x) == j.n else Code.SHORT
        out.append(PkThr(tx_id=j.tx_id, thr=threshold(j.x, j.mult), code=code))
        if code != Code.OK:
            break
    return out


def expected(jobs: list[Job]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """``m_out`` and ``m_status`` bursts, up to and including the first error."""
    out, status = [], []
    for j, t in zip(jobs, tokens(jobs)):
        idx = peaks(j.x, t.thr) if t.code == Code.OK else np.zeros(0, dtype=np.uint16)
        out.append(PkResp(tx_id=j.tx_id, count=len(idx)).serialize(word_bw=W))
        if len(idx):
            out.append(array(U16, idx).serialize(word_bw=W))
        status.append(PkStatus(tx_id=j.tx_id, code=t.code).serialize(word_bw=W))
    return ([np.asarray(b, dtype=np.uint64) for b in out],
            [np.asarray(b, dtype=np.uint64) for b in status])


__all__ = ["Stats", "Detect", "PeakPicker", "Job", "scenario", "stimulus", "tokens", "expected",
           "PkCmd", "PkThr", "PkResp", "PkStatus", "Code", "W", "NMAX"]
