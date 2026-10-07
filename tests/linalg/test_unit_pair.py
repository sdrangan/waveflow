"""Step 9.3: the two standalone units composed message to message, in pysim (no Vitis).

A relay host plays CG jobs through two :class:`~waveflow.linalg.cg_vector.CgVectorUnit` and one
:class:`~waveflow.linalg.systolic.SystolicUnit`.  Each ``P`` a vector unit replies with goes to the
systolic unit as the ``B`` of a ``MUL`` request whose ``A`` is the job's, and each ``C`` that unit
replies with goes back to the vector unit as the next ``STEP``'s ``S``.  The reply payloads are
forwarded word for word, never decoded: the two units share the memory format (lane width,
row-major order, each register's integer bits), so one unit's reply is the other's request.

The two vector units share the systolic unit, so their requests interleave there and its replies
are routed back by tag; and each vector unit's next ``START`` goes out right after its previous
job's last ``STEP``, before that job's ``X`` has returned.  So several jobs are in flight, and no
core is coupled to another.  Every ``X`` equals :func:`~waveflow.linalg.cg.cg_solve` on the job's
stored ``A`` and ``B``, and every reply carries its request's tag and ``OK``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import pytest
import simpy

from tests.linalg._cg_core_bench import cg_formats
from tests.linalg._cg_unit_bench import random_job
from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwModule
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.linalg.cg import cg_solve
from waveflow.linalg.cg_vector import CgOp, CgVectorUnit
from waveflow.linalg.lanes import from_words, to_words
from waveflow.linalg.message import LinalgHeader, Status, header
from waveflow.linalg.systolic import MatmulOp, SystolicUnit
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation

N, L, LANE = 32, 4, 16


def _words(h: LinalgHeader, w: int) -> np.ndarray:
    return np.asarray(h.serialize(word_bw=w), np.uint64)


@dataclass
class PairHost(HwModule):
    """The relay: plays each vector unit's jobs, and routes the systolic unit's replies by tag."""

    jobs: tuple = ()  # per vector unit, its jobs (``CgJob``), in order
    formats: object = None
    word_bits: int = 64

    def __post_init__(self) -> None:
        super().__post_init__()

        def ep(cls, name):
            e = cls(
                name=f"{self.name}_{name}",
                sim=self.sim,
                bitwidth=self.word_bits,
                has_tlast=True,
            )
            self.add_endpoint(e)
            return e

        n = len(self.jobs)
        self.cg_req = [ep(StreamIFMaster, f"cg{u}_req") for u in range(n)]
        self.cg_rep = [ep(StreamIFSlave, f"cg{u}_rep") for u in range(n)]
        self.mm_req = ep(StreamIFMaster, "mm_req")
        self.mm_rep = ep(StreamIFSlave, "mm_rep")
        self.x: dict = {}  # (unit, job) -> (xr, xi)
        self.tags: list = []  # (request tag, reply tag, reply status) of every reply
        self.mm_jobs: list = []  # the (unit, job) of each matmul request, in the order sent

    def run_proc(self) -> ProcessGen[None]:
        env = self.sim.env
        self.cg_box = [simpy.Store(env) for _ in self.jobs]
        self.c_box = [simpy.Store(env) for _ in self.jobs]
        self.mm_out = simpy.Store(env)
        self.process(self._mm_sender())
        self.process(self._mm_router())
        for u in range(len(self.jobs)):
            self.process(self._cg_router(u))
            self.process(self._job_loop(u))
        yield self.timeout(0)

    def _reply(self, port) -> ProcessGen[tuple]:
        h = yield from port.get_schema(LinalgHeader)
        words = (yield from port.get()) if int(h.length) else np.zeros(0, np.uint64)
        return h, np.asarray(words, np.uint64)

    def _mm_sender(self) -> ProcessGen[None]:
        while True:
            bursts = yield self.mm_out.get()
            for b in bursts:
                yield from self.mm_req.write(b)

    def _mm_router(self) -> ProcessGen[None]:
        while True:
            h, words = yield from self._reply(self.mm_rep)
            yield self.c_box[int(h.tag) >> 16].put((h, words))

    def _cg_router(self, u: int) -> ProcessGen[None]:
        while True:
            reply = yield from self._reply(self.cg_rep[u])
            yield self.cg_box[u].put(reply)

    def _send(self, u: int, h: LinalgHeader, payload: np.ndarray) -> ProcessGen[None]:
        yield from self.cg_req[u].write(_words(h, self.word_bits))
        yield from self.cg_req[u].write(payload)

    def _start(self, u: int, j: int) -> ProcessGen[None]:
        job, f, w = self.jobs[u][j], self.formats, self.word_bits
        b = to_words(*job.b, f.B, LANE, w)
        tag = (u << 16) | (j << 8)
        h = header(
            tag, CgOp.START, k=job.k, n=job.n, length=len(b), nfollow=job.nit
        )
        yield from self._send(u, h, b)
        return tag

    def _expect(self, h: LinalgHeader, tag: int) -> None:
        self.tags.append((tag, int(h.tag), int(h.status)))
        if int(h.status) != Status.OK or int(h.tag) != tag:
            raise AssertionError(f"reply {int(h.tag):#x} {Status(int(h.status)).name}")

    def _job_loop(self, u: int) -> ProcessGen[None]:
        f, w = self.formats, self.word_bits
        jobs = self.jobs[u]
        tag = yield from self._start(u, 0)
        for j, job in enumerate(jobs):
            a = to_words(*job.a, f.A, LANE, w)
            for it in range(1, job.nit + 1):
                h, p = yield self.cg_box[u].get()  # P of the previous step
                self._expect(h, tag)
                mtag = (u << 16) | (j << 8) | it
                mh = header(
                    mtag,
                    MatmulOp.MUL,
                    m=job.k,
                    k=job.k,
                    n=job.n,
                    length=len(a) + len(p),
                )
                self.mm_jobs.append((u, j))
                yield self.mm_out.put([_words(mh, w), a, p])  # P forwarded as B
                h, s = yield self.c_box[u].get()
                self._expect(h, mtag)
                tag = (u << 16) | (j << 8) | it
                sh = header(
                    tag,
                    CgOp.STEP,
                    k=job.k,
                    n=job.n,
                    length=len(s),
                    nfollow=job.nit - it,
                )
                yield from self._send(u, sh, s)  # C forwarded as S
            last = tag
            if j + 1 < len(jobs):  # the next job starts before this one's X returns
                tag = yield from self._start(u, j + 1)
            h, x = yield self.cg_box[u].get()
            self._expect(h, last)
            xr, xi = from_words(x, job.k * job.n, f.X, LANE, w)
            self.x[(u, j)] = (xr.reshape(job.k, job.n), xi.reshape(job.k, job.n))


@dataclass
class PairTB(FreeRunMod):
    """The host, two vector units and a systolic unit, wired by framed streams."""

    cpp_kernel_name: ClassVar[str | None] = None
    K: int = 4
    jobs: tuple = ()
    formats: object = None
    word_bits: int = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        K, w, f = self.K, self.word_bits, self.formats
        self.host = PairHost(
            name="host", sim=self.sim, jobs=self.jobs, formats=f, word_bits=w
        )
        self.cg = [
            CgVectorUnit(
                name=f"cg{u}",
                sim=self.sim,
                word_bits=w,
                Kmax=K,
                Nmax=N,
                nitmax=K,
                L=L,
                lane_bits=LANE,
                formats=f,
                clk=self.clk,
            )
            for u in range(len(self.jobs))
        ]
        self.mm = SystolicUnit(
            name="mm",
            sim=self.sim,
            word_bits=w,
            Mmax=K,
            Kmax=K,
            Nmax=N,
            L=L,
            R=4,
            C=8,
            form=4,
            lane_bits=LANE,
            a=f.A,
            b=f.P,
            c=f.S,
            clk=self.clk,
        )
        for c in (self.host, *self.cg, self.mm):
            self.add_comp(c)
        links = [(self.host.mm_req, self.mm.s_in), (self.mm.s_out, self.host.mm_rep)]
        for u, unit in enumerate(self.cg):
            links += [
                (self.host.cg_req[u], unit.s_in),
                (unit.s_out, self.host.cg_rep[u]),
            ]
        for i, (master, slave) in enumerate(links):
            iface = StreamIF(
                name=f"link{i}", sim=self.sim, clk=self.clk, bitwidth=w, framed=True
            )
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)


def pair_jobs(K: int, seed: int):
    """Per vector unit, three jobs: one iteration, a middle count and K; the first job of unit 1
    has a zero column of ``B``."""
    f = cg_formats(12, 8)
    rng = np.random.default_rng([93, K, seed])
    nits = (1, max(2, K // 2), K)
    return f, tuple(
        tuple(
            random_job(rng, f, nit, K, N, zero_column=(u, i) == (1, 0))
            for i, nit in enumerate(nits if u == 0 else nits[::-1])
        )
        for u in range(2)
    )


@pytest.mark.parametrize("K", [4, 8, 16])
def test_units_solve_cg_message_to_message(K):
    f, jobs = pair_jobs(K, seed=0)
    tb = PairTB(name="tb", sim=Simulation(), K=K, jobs=jobs, formats=f)
    tb.sim.run_sim()
    host = tb.host
    assert sorted(host.x) == [(u, j) for u in range(2) for j in range(3)]
    for (u, j), (xr, xi) in host.x.items():
        job = jobs[u][j]
        want = cg_solve(*job.a, *job.b, job.nit, f)
        assert np.array_equal(xr, want.xr) and np.array_equal(xi, want.xi), (u, j)
    # every request was answered OK under its own tag
    n_req = sum(2 * job.nit + 1 for js in jobs for job in js)
    assert len(host.tags) == n_req
    assert all(t == r and s == Status.OK for t, r, s in host.tags)
    # several jobs in flight: the two units' matmul requests interleave at the systolic unit
    units = [u for u, _ in host.mm_jobs]
    assert any(a != b for a, b in itertools.pairwise(units))
