"""Block C: correlator -- templates and jobs on two streams, as the user asked.

Two input streams have no order between them, so the order is carried in the messages: every template
carries an id, every job names the template it needs, the correlator waits for that template, and the
response echoes the id it used (the config-id pattern of the ``mm_fir`` example)::

    s_tmpl   : TmplHdr(tmpl_id, ntaps) | h[0] .. h[L-1]
    s_job    : JobCmd(tx_id, tmpl_id, n) | x[0] .. x[n-1]
    m_out    : JobResp(tx_id, tmpl_id, nout) | y[0] .. y[nout-1]
    m_status : JobStatus(tx_id, code)            one per job, after its results

Templates are taken only when a job asks for one, so a template sent while earlier jobs wait stays in
``s_tmpl`` until its first job.  Template ids increase; a job naming an id already replaced is never
answered (the sender's bug).  Errors are fatal: ``n < L``, or a sample burst that ends early.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave
from waveflow.utils.burst_io import StreamBurst

W = 32
MAX_TAPS = 32

S16 = IntField.specialize(bitwidth=16, signed=True)
S32 = IntField.specialize(bitwidth=32, signed=True)
U8 = IntField.specialize(bitwidth=8, signed=False)
U16 = IntField.specialize(bitwidth=16, signed=False)


class Code(IntEnum):
    OK = 0
    TOO_SHORT = 1        # n < L: fatal
    SHORT_BURST = 2      # the samples ended before n: fatal


CodeField = EnumField.specialize(enum_type=Code)


class TmplHdr(DataList):
    elements = {"tmpl_id": {"schema": U16}, "ntaps": {"schema": U8}}


class JobCmd(DataList):
    elements = {"tx_id": {"schema": U16}, "tmpl_id": {"schema": U16}, "n": {"schema": U16}}


class JobResp(DataList):
    elements = {"tx_id": {"schema": U16}, "tmpl_id": {"schema": U16}, "nout": {"schema": U16}}


class JobStatus(DataList):
    elements = {"tx_id": {"schema": U16}, "code": {"schema": CodeField}}


# --- the golden ---------------------------------------------------------------------------------

def correlate(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    y = np.correlate(x.astype(np.int64), h.astype(np.int64), "valid") >> 8
    return np.clip(y, -(2**31), 2**31 - 1)


def _burst(words) -> StreamBurst:
    return StreamBurst(np.asarray(words, np.uint64), True)


def tmpl_bursts(tmpls: dict[int, np.ndarray]) -> list[StreamBurst]:
    out = []
    for tid, h in tmpls.items():
        out.append(_burst(TmplHdr(tmpl_id=tid, ntaps=len(h)).serialize(word_bw=W)))
        out.append(_burst(write_array(h, elem_type=S16, word_bw=W)))
    return out


def job_bursts(jobs) -> list[StreamBurst]:
    """*jobs*: (tx_id, tmpl_id, x, sent)."""
    out = []
    for tx, tid, x, sent in jobs:
        out.append(_burst(JobCmd(tx_id=tx, tmpl_id=tid, n=len(x)).serialize(word_bw=W)))
        out.append(_burst(write_array(x[:sent], elem_type=S16, word_bw=W)))
    return out


def expected(tmpls, jobs):
    out, st = [], []
    for tx, tid, x, sent in jobs:
        h = tmpls[tid]
        if len(x) < len(h):
            st.append(JobStatus(tx_id=tx, code=Code.TOO_SHORT).serialize(word_bw=W))
            break
        xs = x[:sent]
        y = correlate(xs, h) if len(xs) >= len(h) else np.zeros(0, np.int64)
        out.append(JobResp(tx_id=tx, tmpl_id=tid, nout=len(x) - len(h) + 1).serialize(word_bw=W))
        out.append(write_array(y, elem_type=S32, word_bw=W) if len(y) else [])
        code = Code.OK if sent == len(x) else Code.SHORT_BURST
        st.append(JobStatus(tx_id=tx, code=code).serialize(word_bw=W))
        if code != Code.OK:
            break
    return ([np.asarray(b, np.uint64) for b in out if len(b)], [np.asarray(b, np.uint64) for b in st])


def scenario(kind: str):
    rng = np.random.default_rng(5)
    tmpls = {1: rng.integers(-2000, 2000, 5), 2: rng.integers(-2000, 2000, 12)}

    def job(tx, tid, n, sent=None):
        x = rng.integers(-30000, 30000, n)
        return (tx, tid, x, n if sent is None else sent)

    # Both templates are sent at once; jobs 21 and 22 must still use template 1.
    jobs = [job(21, 1, 40), job(22, 1, 5), job(23, 2, 100), job(24, 2, 12)]
    if kind == "too_short":
        jobs = jobs[:3] + [job(25, 2, 7), job(26, 2, 50)]
    elif kind == "short_burst":
        jobs = jobs[:1] + [job(25, 1, 30, sent=20), job(26, 1, 50)]
    elif kind != "test":
        raise KeyError(kind)
    return tmpls, jobs


# --- the module -----------------------------------------------------------------------------------

@dataclass
class Correlator(FreeRunMod):
    """One firing = one job."""

    cpp_kernel_name: ClassVar[str | None] = "correlator"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        mk_in = lambda n: FramedStreamIFSlave(name=f"{self.name}_{n}", sim=self.sim, bitwidth=W)  # noqa: E731
        mk_out = lambda n: FramedStreamIFMaster(name=f"{self.name}_{n}", sim=self.sim, bitwidth=W)  # noqa: E731
        self.s_tmpl, self.s_job = mk_in("s_tmpl"), mk_in("s_job")
        self.m_out, self.m_status = mk_out("m_out"), mk_out("m_status")
        for ep in (self.s_tmpl, self.s_job, self.m_out, self.m_status):
            self.add_endpoint(ep)
        self.tmpl_id: int | None = None          # the template held, and its taps
        self.taps = None

    def workload(self, kind: str = "test") -> dict:
        tmpls, jobs = scenario(kind)
        return {"s_tmpl": tmpl_bursts(tmpls), "s_job": job_bursts(jobs)}

    def golden(self, kind: str = "test") -> dict:
        out, st = expected(*scenario(kind))
        return {"m_out": out, "m_status": st}

    def run_iter(self):
        job = yield from self.s_job.get_schema(JobCmd)
        while self.tmpl_id != job.tmpl_id:                       # wait for the template it names
            hdr = yield from self.s_tmpl.get_schema(TmplHdr)
            self.taps = (yield from self.s_tmpl.get_array(S16, hdr.ntaps)).val
            self.tmpl_id = hdr.tmpl_id
        L = len(self.taps)
        if job.n < L:
            yield from self.m_status.write(JobStatus(tx_id=job.tx_id, code=Code.TOO_SHORT))
            yield self.event()                                   # fatal
        nout = job.n - L + 1
        yield from self.m_out.write(JobResp(tx_id=job.tx_id, tmpl_id=self.tmpl_id, nout=nout))
        x = yield from self.s_job.get_array(S16, job.n)
        if len(x.val) >= L:
            y = correlate(x.val, self.taps)
            yield from self.m_out.write_array(S32, nout, array(S32, y))
        code = Code.OK if len(x.val) == job.n else Code.SHORT_BURST
        yield from self.m_status.write(JobStatus(tx_id=job.tx_id, code=code))
        if code != Code.OK:
            yield self.event()
