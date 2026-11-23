"""cg_pool.py -- the B1 CG engine: a pool of M dedicated CG workers (``plans/dse_matrix_sched.md`` Section
7a, Stages E0-E1).

The engine solves a batch of CG systems ``(I + H Hᴴ) X = B`` in the factored form (:mod:`.cg_golden`).  It
knows nothing of users or whitening.  The shape is the guide's pool of workers
(``docs/guide/systems/shared_kernels.md``, *A pool of workers*)::

    s_in --> [ StreamDispatch ] --job_k--> CgWorker_k --res_k--> [ StreamArbiter ] --> [ CgSplit ] --> m_out
                 ^ s_free  (first-free, 1 credit a worker)            | m_sel                       --> m_status
                 +----------------------------------------------------+

**The protocol** (command-response, ``docs/guide/systems/command_response.md``), every port 128 bits::

    s_in     : Job(tx_id, m, c, t) | H (N_rx x m) then B (N_rx x c), column by column, 4 samples a word
    m_out    : Resp(tx_id, n)      | X (N_rx x c), column by column         (n = N_rx * c samples)
    m_status : Status(tx_id, status)                                        (one a job, after its X)

Control and data share ``s_in``: each job carries its own sizes and its matrix **inline**, so a worker
needs nothing from anywhere else (B1: no bus, no shared memory).  Results leave in **completion order**;
``tx_id`` says whose.

**Why the worker's answer is one frame.**  A worker writes ``Resp``, ``X`` and ``Status`` as one 3-burst
frame on one port, so a single ``StreamArbiter`` merges the workers and its ``m_sel`` -- the worker that
finished -- goes straight back as the dispatcher's credit.  ``CgSplit`` after the arbiter puts the
``Status`` on its own stream.  Statuses on a second arbiter instead would leave that arbiter's ``m_sel``
with no reader.

**Errors are fatal** (E0): a job whose ``m``, ``c`` or ``t`` is out of range (``BAD_ARGS``, its payload left
unread), or whose payload burst is not ``N_rx (m + c) / 4`` words (``BAD_LEN``), is answered by ``Resp(tx_id,
0)``, one zero word closed short, and the error ``Status``; then that worker stops taking input until a
reset.  Behind the dispatcher that stops the engine: the dispatcher blocks on the stopped worker.

**Link depths** (the pool's rules, ``docs/guide/streams/dispatch.md``): a worker's input link holds a whole
job (``JOB_DEPTH`` words), so its one credit is room for the job and the dispatcher never stalls mid-job;
a worker's result link holds its largest burst (``RES_DEPTH``), so no link on the loop carries a burst
longer than its depth; the credit link holds every outstanding credit (M).

The HLS twins: ``src/cg_worker_task.h`` (:class:`CgWorker`) and ``src/cg_split_task.h`` (:class:`CgSplit`).
The build DAG is ``cg_pool_build.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np

from examples.cg_pool import cg_golden as cgg
from waveflow.dsp.ssr_fft.types import EdgeType
from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave, StreamIF
from waveflow.streams.arbiter import StreamArbiter, copy_burst
from waveflow.streams.dispatch import StreamDispatch
from waveflow.utils import complexutils as cx
from waveflow.utils.burst_io import StreamBurst

HERE = Path(__file__).resolve().parent
INCLUDE_DIR = "include"

# ---------------------------------------------------------------------------
# 0. The numbers
# ---------------------------------------------------------------------------

F = cgg.FORMATS
N_RX, M_MAX, C_MAX, T_MAX = cgg.N_RX, cgg.M_MAX, cgg.C_MAX, cgg.T_MAX
#: Complex samples a word, and the word.
LANES = 4
E_H = EdgeType("h", F.H, LANES)          # H and B (both Q4.12) on s_in
E_X = EdgeType("x", F.Xo, LANES)         # X on m_out
W = E_H.bitwidth                         # 128
assert E_X.bitwidth == W
#: Words a column of N_rx samples.
COL_WORDS = N_RX // LANES

# ---------------------------------------------------------------------------
# 1. Schemas
# ---------------------------------------------------------------------------

U8 = IntField.specialize(bitwidth=8, signed=False, include_dir=INCLUDE_DIR)
U16 = IntField.specialize(bitwidth=16, signed=False, include_dir=INCLUDE_DIR)


class StatusCode(IntEnum):
    OK = 0
    BAD_ARGS = 1      # m, c or t out of range: fatal, the payload is left unread
    BAD_LEN = 2       # the payload burst is not N_rx (m + c) / 4 words: fatal


StatusCodeField = EnumField.specialize(enum_type=StatusCode)


class Job(DataList):
    """One CG solve; its payload burst follows: ``H`` then ``B``, column by column."""
    elements = {
        "tx_id": {"schema": U16, "description": "the sender's id, echoed in the Resp and the Status"},
        "m":     {"schema": U8,  "description": "columns of H, 1 to M_MAX"},
        "c":     {"schema": U8,  "description": "right-hand sides (columns of B and X), 1 to C_MAX"},
        "t":     {"schema": U8,  "description": "CG iterations, 1 to T_MAX"},
    }


class Resp(DataList):
    """The head of a job's answer on ``m_out``: ``n`` samples of ``X`` follow (``N_rx * c``; 0 on an
    error, when one zero word closes the burst short)."""
    elements = {
        "tx_id": {"schema": U16, "description": "echo of the job's tx_id"},
        "n":     {"schema": U16, "description": "samples of X that follow, column by column"},
    }


class Status(DataList):
    """One a job, after its ``X``."""
    elements = {
        "tx_id":  {"schema": U16,             "description": "the job it reports on"},
        "status": {"schema": StatusCodeField, "description": "OK, BAD_ARGS or BAD_LEN (both fatal)"},
    }


#: In dependency order, each with the word widths it travels at.
SCHEMAS = [(StatusCodeField, [W]), (Job, [W]), (Resp, [W]), (Status, [W])]

# ---------------------------------------------------------------------------
# 2. Packing (the schema's, never by hand), the stimulus and the golden
# ---------------------------------------------------------------------------


def _chunks(bw: int) -> int:
    return -(-bw // 64)


def pack(re: np.ndarray, im: np.ndarray, e: EdgeType) -> np.ndarray:
    """Stored samples, column by column (``N_rx x k`` arrays), -> words of edge *e* (2 chunks each)."""
    re = np.asarray(re).T.reshape(-1)          # column-major: column 0's N_rx samples first
    im = np.asarray(im).T.reshape(-1)
    words = write_array(cx.make_complex(re, im, e.fmt), elem_type=e.elem, word_bw=e.bitwidth)
    return np.asarray(words, dtype=np.uint64).reshape(-1, _chunks(e.bitwidth))


def unpack(words: np.ndarray, e: EdgeType, ncols: int) -> tuple[np.ndarray, np.ndarray]:
    """Words of edge *e* -> ``N_rx x ncols`` stored samples, re and im (the inverse of :func:`pack`)."""
    words = np.asarray(words, dtype=np.uint64).reshape(-1, _chunks(e.bitwidth))
    v = read_array(words, elem_type=e.elem, word_bw=e.bitwidth, shape=N_RX * ncols).val
    re = np.asarray(v["re"], dtype=np.int64).reshape(ncols, N_RX).T
    im = np.asarray(v["im"], dtype=np.int64).reshape(ncols, N_RX).T
    return re, im


def hdr(msg) -> np.ndarray:
    """A message as one 128-bit word."""
    return np.asarray(msg.serialize(word_bw=W), dtype=np.uint64).reshape(-1, _chunks(W))


def read_msg(cls, words):
    """One message off a recorded burst, by its schema."""
    return cls().deserialize(np.asarray(words, dtype=np.uint64).reshape(-1, _chunks(W)), word_bw=W)


def payload_words(m: int, c: int) -> int:
    return COL_WORDS * (int(m) + int(c))


@dataclass
class JobSpec:
    """A job as sent: its problem, and what goes wrong with it (``None``: nothing).  ``bad_args`` sends
    the header's ``m``, ``c``, ``t`` as given in ``hdr_override``; ``short`` drops the payload's last
    ``short`` words."""
    p: cgg.Problem
    hdr_override: dict | None = None
    short: int = 0

    @property
    def header(self) -> Job:
        f = {"tx_id": self.p.tx_id, "m": self.p.m, "c": self.p.c, "t": self.p.t}
        f.update(self.hdr_override or {})
        return Job(**f)

    @property
    def code(self) -> StatusCode:
        h = self.header
        if not (1 <= int(h.m) <= M_MAX and 1 <= int(h.c) <= C_MAX and 1 <= int(h.t) <= T_MAX):
            return StatusCode.BAD_ARGS
        if self.short or (int(h.m), int(h.c)) != (self.p.m, self.p.c):
            return StatusCode.BAD_LEN
        return StatusCode.OK


def job_bursts(js: JobSpec) -> list[StreamBurst]:
    """A job on ``s_in``: its header burst, then its payload burst (``H`` then ``B``)."""
    p = js.p
    words = np.concatenate([pack(p.hr, p.hi, E_H), pack(p.br, p.bi, E_H)])
    if js.short:
        words = words[:-js.short]
    return [StreamBurst(hdr(js.header), True), StreamBurst(words, True)]


def answer_bursts(js: JobSpec) -> tuple[list[np.ndarray], StatusCode]:
    """What a worker writes for *js*: ``Resp``, ``X`` (or one zero word on an error), and the code."""
    code = js.code
    tx = js.p.tx_id
    if code != StatusCode.OK:
        return [hdr(Resp(tx_id=tx, n=0)), np.zeros((1, _chunks(W)), dtype=np.uint64)], code
    xr, xi, _ = cgg.fixed_solve(js.p)
    return [hdr(Resp(tx_id=tx, n=N_RX * js.p.c)), pack(xr, xi, E_X)], code


def worker_frame(js: JobSpec) -> list[np.ndarray]:
    """The 3-burst frame a worker writes on its ``m_out``: ``Resp``, ``X``, ``Status``."""
    ans, code = answer_bursts(js)
    return ans + [hdr(Status(tx_id=js.p.tx_id, status=code))]


def expected(jobs: list[JobSpec]) -> dict[str, list[np.ndarray]]:
    """One worker's outputs for *jobs* in order (its harness): every job's frame, up to and including
    the first error -- fatal, so nothing after it."""
    out: list[np.ndarray] = []
    for js in jobs:
        out += worker_frame(js)
        if js.code != StatusCode.OK:
            break
    return {"m_out": out}


# --- the scenarios ----------------------------------------------------------------------------------
def jobs(kind: str = "test", n: int | None = None, seed: int | None = None) -> list[JobSpec]:
    """``"test"``: 6 jobs of 1-4 users, t 1-4 (one each size); ``"mixed"``: ``n`` (default 12) random jobs,
    set sizes 1-4, ``t = 4``; ``"bad_args"`` / ``"bad_len"``: two good jobs, then a bad one, then one more
    (never answered)."""
    rng = np.random.default_rng({"test": 11, "mixed": 12, "bad_args": 13, "bad_len": 14}[kind]
                                if seed is None else seed)
    if kind == "test":
        sizes = [(1, 4), (4, 4), (2, 1), (3, 2), (1, 8 // 2), (4, 3)]
        cs = [8, 8, 1, 4, 8, 3]
        return [JobSpec(cgg.make_problem(rng, 100 + i, users=u, t=t, c=c))
                for i, ((u, t), c) in enumerate(zip(sizes, cs))]
    if kind == "mixed":
        return [JobSpec(cgg.make_problem(rng, 200 + i, t=4)) for i in range(n or 12)]
    good = [JobSpec(cgg.make_problem(rng, 300 + i, users=2, t=2)) for i in range(2)]
    bad_p = cgg.make_problem(rng, 302, users=1, t=2)
    bad = (JobSpec(bad_p, hdr_override={"t": 0}) if kind == "bad_args"
           else JobSpec(bad_p, short=3))
    return good + [bad, JobSpec(cgg.make_problem(rng, 303, users=1, t=1))]


def stimulus(js: list[JobSpec]) -> list[StreamBurst]:
    return [b for j in js for b in job_bursts(j)]


# ---------------------------------------------------------------------------
# 3. The worker's timing: its HLS body's loops, cycle for cycle
# ---------------------------------------------------------------------------

#: Lanes of the matrix loops (P = 16 complex MACs a cycle, E0) and of the vector loops.
P_MAC = 16
V_LANES = 4
#: Cycles a loop spends beyond its trip count (entry, pipeline fill and drain, exit), per loop of the
#: body (``src/cg_worker_task.h``).  Seeds from the csynth schedule, refit from the traced RTL.
CYC = {
    "hdr": 4,        # the Job read -> the LOAD loop's first read
    "load": 6,       # LOAD: N_rx (m + c) / 4 words, one a cycle
    "init": 4,       # INIT: rz per column (part of LOAD) -> the first iteration
    "t": 12,         # TMUL: m c N_rx / P cycles
    "u": 12,         # UMUL: m c N_rx / P cycles
    "ps": 8,         # PS:   N_rx c / V cycles
    "alpha": 40,     # ALPHA: c divides, pipelined
    "xr": 10,        # XR:   N_rx c / V cycles
    "beta": 40,      # BETA: c divides, pipelined
    "d": 8,          # DUPD: N_rx c / V cycles
    "out": 4,        # the last iteration -> the Resp
    "store": 4,      # STORE: N_rx c / 4 words
}


def iteration_cycles(m: int, c: int) -> int:
    k = CYC
    mat = int(m) * int(c) * N_RX // P_MAC
    vec = N_RX * int(c) // V_LANES
    return (mat + k["t"]) + (mat + k["u"]) + (vec + k["ps"]) + (int(c) + k["alpha"]) \
        + (vec + k["xr"]) + (int(c) + k["beta"]) + (vec + k["d"])


def compute_cycles(m: int, c: int, t: int) -> int:
    """From the payload's last word to the ``Resp``: ``t`` iterations and the loops around them."""
    return CYC["init"] + int(t) * iteration_cycles(m, c) + CYC["out"]


def job_cycles(m: int, c: int, t: int) -> int:
    """A job alone, from its header to its ``Status``: the baseline's per-job number."""
    return (CYC["hdr"] + payload_words(m, c) + CYC["load"] + compute_cycles(m, c, t) + 1
            + N_RX * int(c) // LANES + CYC["store"] + 1)


# ---------------------------------------------------------------------------
# 4. The modules
# ---------------------------------------------------------------------------


def _ep(cls, owner, name: str):
    ep = cls(name=f"{owner.name}_{name}", sim=owner.sim, bitwidth=W)
    setattr(owner, name, ep)
    owner.add_endpoint(ep)
    return ep


def _cycles(mod, n: int):
    if n > 0:
        yield mod.env.timeout(n * mod.clk.period)


@dataclass
class CgWorker(FreeRunMod):
    """One CG worker: a job in, its answer frame out (``Resp``, ``X``, ``Status``).  One firing = one job:
    read the header, check it, load ``H`` and ``B`` at a word a cycle into its own memory, run ``t``
    iterations, write the answer.  The arithmetic is :func:`.cg_golden.fixed_solve`; the time, the body's
    loops (:func:`compute_cycles`)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_worker"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        _ep(FramedStreamIFSlave, self, "s_in")
        _ep(FramedStreamIFMaster, self, "m_out")
        self.jobs_done = 0
        self.halted = False

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("cg_worker_task", "cg_worker_task.h", ("s_in", "m_out"),
                          framed=("s_in", "m_out"))

    def _fail(self, tx_id: int, code: StatusCode):
        yield from self.m_out.write(Resp(tx_id=tx_id, n=0))
        yield from self.m_out.write(np.zeros((1, _chunks(W)), dtype=np.uint64))
        yield from self.m_out.write(Status(tx_id=tx_id, status=code))
        self.halted = True
        yield self.event()                      # fatal: never triggered, only a reset recovers

    def run_iter(self):
        job = yield from self.s_in.get_schema(Job)
        m, c, t, tx = int(job.m), int(job.c), int(job.t), int(job.tx_id)
        if not (1 <= m <= M_MAX and 1 <= c <= C_MAX and 1 <= t <= T_MAX):
            yield from self._fail(tx, StatusCode.BAD_ARGS)
        yield from _cycles(self, CYC["hdr"])
        x = yield from self.s_in.begin_read(pending_ok=True)
        yield from self.s_in.end_read(x, charge_beats=True)
        words = np.asarray(x.data, dtype=np.uint64).reshape(-1, _chunks(W))
        if len(words) != payload_words(m, c):
            yield from self._fail(tx, StatusCode.BAD_LEN)
        hw = words[: COL_WORDS * m]
        bw = words[COL_WORDS * m:]
        hr, hi = unpack(hw, E_H, m)
        br, bi = unpack(bw, E_H, c)
        xr, xi, _ = cgg.fixed_solve(cgg.Problem(tx, m, c, t, hr, hi, br, bi))
        yield from _cycles(self, CYC["load"] + compute_cycles(m, c, t))
        yield from self.m_out.write(Resp(tx_id=tx, n=N_RX * c))
        yield from self.m_out.write(pack(xr, xi, E_X))
        yield from _cycles(self, CYC["store"])
        yield from self.m_out.write(Status(tx_id=tx, status=StatusCode.OK))
        self.jobs_done += 1

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": stimulus(jobs(kind))}

    def golden(self, kind: str = "test") -> dict:
        return expected(jobs(kind))


@dataclass
class CgSplit(FreeRunMod):
    """The pool's last stage: each worker frame off the arbiter -- ``Resp``, ``X``, ``Status`` -- split, the
    answer on ``m_out`` and the status on ``m_status``.  ``X`` is copied as its words arrive."""

    cpp_kernel_name: ClassVar[str | None] = "cg_split"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        _ep(FramedStreamIFSlave, self, "s_in")
        _ep(FramedStreamIFMaster, self, "m_out")
        _ep(FramedStreamIFMaster, self, "m_status")

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("cg_split_task", "cg_split_task.h", ("s_in", "m_out", "m_status"),
                          framed=("s_in", "m_out", "m_status"))

    def run_iter(self):
        r = yield from self.s_in.get_schema(Resp)
        yield from self.m_out.write(r)
        yield from copy_burst(self.s_in, self.m_out)
        st = yield from self.s_in.get_schema(Status)
        yield from self.m_status.write(st)

    def workload(self, kind: str = "test") -> dict:
        frames = [w for js in jobs(kind) for w in worker_frame(js)]
        return {"s_in": [StreamBurst(f, True) for f in frames]}

    def golden(self, kind: str = "test") -> dict:
        out, st = [], []
        for js in jobs(kind):
            f = worker_frame(js)
            out += f[:2]
            st.append(f[2])
        return {"m_out": out, "m_status": st}


@dataclass
class CgPool(FreeRunMod):
    """The B1 engine: ``n_workers`` :class:`CgWorker` behind a first-free :class:`StreamDispatch` (one
    credit a worker), their frames merged by a round-robin :class:`StreamArbiter` whose ``m_sel`` is the
    credit, then :class:`CgSplit`.  At one worker there is no pool: ``s_in -> worker -> split``.
    ``platform_dir`` times the pool's blocks on a calibrated platform (their tables must hold this
    width and frame, or they refuse)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_pool"
    #: One top per pool size, so the sizes share a build: ``cg_pool_n_workers2`` ...
    kernel_name_params: ClassVar[tuple[str, ...]] = ("n_workers",)
    build_fields: ClassVar[tuple[str, ...]] = ("platform_dir",)

    n_workers: HwParam[int] = 2
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    platform_dir: str | Path | None = None

    #: A worker's input link holds a whole job (1 + 640 words at E0's largest); its result link its
    #: largest burst (N_rx C_MAX / 4 = 128 words) and the frame's two one-word bursts.
    JOB_DEPTH: ClassVar[int] = 1024
    RES_DEPTH: ClassVar[int] = 256
    OUT_DEPTH: ClassVar[int] = 16

    def __post_init__(self) -> None:
        super().__post_init__()
        n = int(self.n_workers)
        if not 1 <= n <= 16:
            raise ValueError(f"{self.name}: n_workers must be 1 to 16, got {n}")
        self.workers = [CgWorker(name=f"{self.name}_w{k}", sim=self.sim, clk=self.clk) for k in range(n)]
        self.split = CgSplit(name=f"{self.name}_split", sim=self.sim, clk=self.clk)
        for w in self.workers:
            self.add_comp(w)
        links = []
        if n == 1:
            self.dsp = self.arb = None
            links.append(("res0", self.workers[0].m_out, self.split.s_in, self.OUT_DEPTH))
            self.s_in = self.workers[0].s_in
        else:
            self.dsp = StreamDispatch(name=f"{self.name}_dsp", sim=self.sim, n_out=n, bitwidth=W,
                                      frame_bursts=2, credits=1, policy="first_free", clk=self.clk,
                                      platform_dir=self.platform_dir)
            self.arb = StreamArbiter(name=f"{self.name}_arb", sim=self.sim, n_in=n, bitwidth=W,
                                     frame_bursts=3, policy="round_robin", clk=self.clk,
                                     platform_dir=self.platform_dir)
            self.add_comp(self.dsp)
            self.add_comp(self.arb)
            for k, w in enumerate(self.workers):
                links.append((f"job{k}", self.dsp.m_out[k], w.s_in, self.JOB_DEPTH))
                links.append((f"res{k}", w.m_out, self.arb.s_in[k], self.RES_DEPTH))
            links.append(("credit", self.arb.m_sel, self.dsp.s_free, max(2, n)))
            links.append(("merged", self.arb.m_out, self.split.s_in, self.OUT_DEPTH))
            self.s_in = self.dsp.s_in
        self.add_comp(self.split)
        for name, master, slave, depth in links:
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=W,
                             framed=True, depth=depth)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)
        self.m_out = self.split.m_out
        self.m_status = self.split.m_status
        self.boundary = ["s_in", "m_out", "m_status"]

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": stimulus(jobs(kind))}

    def expected_answers(self, kind: str = "test") -> dict[int, tuple[list, np.ndarray]]:
        """Per ``tx_id``: the answer's two bursts and its status word -- what the pool must produce for
        each good job, in any order (completion order is timing's)."""
        out = {}
        for js in jobs(kind):
            f = worker_frame(js)
            out[js.p.tx_id] = (f[:2], f[2])
        return out


def answers_by_tx(m_out: list, m_status: list) -> tuple[dict, dict]:
    """A recording's answers and statuses keyed by ``tx_id`` (each read through its schema)."""
    ans, st = {}, {}
    if len(m_out) % 2:
        raise ValueError(f"m_out has {len(m_out)} bursts: not whole answers")
    for i in range(0, len(m_out), 2):
        r = read_msg(Resp, m_out[i])
        tx = int(r.tx_id)
        if tx in ans:
            raise AssertionError(f"tx_id {tx} answered twice")
        ans[tx] = [np.asarray(m_out[i], dtype=np.uint64), np.asarray(m_out[i + 1], dtype=np.uint64)]
    for b in m_status:
        s = read_msg(Status, b)
        tx = int(s.tx_id)
        if tx in st:
            raise AssertionError(f"tx_id {tx} has two statuses")
        st[tx] = int(s.status)
    return ans, st
