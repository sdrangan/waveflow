"""ring_stub.py -- a stub composite with a cycle: the Stage 1 de-risk of ``plans/shared_kernel.md``.

The smallest design with the shape the shared-kernel pattern needs: one unit ``K`` used twice per job, its
output fed back to its own input, frame by frame, with a tag beside it and credit on the loop::

    s_in  ->  Mux  --m_k-->  K (Inc)  --m_out-->  Post  -> m_out, m_status
               ^ ^                                  |
               | +------------ s_crd (a token a job)-+
               +-------------- s_fb  (pass-0 frames) +
          Mux --m_tag (one tag a frame, beside K)--> Post

Per job: ``Job(tx_id, n)`` then ``n`` 64-bit words.  ``K`` adds 1 to each word.  ``Post`` reads the tag
and the frame; a pass-0 frame goes back to ``Mux`` (store and forward: the whole frame first, then its tag
and words); a pass-1 frame is the answer -- ``Resp(tx_id, n)``, the words (each the input plus 2), a
``Status`` -- and a credit token back to ``Mux``.

The rules it exercises (the shared-kernels page will state them):

* **K never blocks on its output.**  ``Mux`` admits a new job only with a credit; ``FMAX`` jobs are in the
  loop at most, each with at most one frame in flight, and every link in the loop holds what they can
  put there.  So ``Post`` never blocks on ``m_fb`` or ``m_crd``.
* **Feedback first.**  ``Mux`` takes a feedback frame before a new job, a whole frame at a time.
* **A feedback frame is announced only when complete** (``Post`` buffers it), so ``Mux`` never commits to
  a frame still being produced.
* **The tag travels beside K** on its own link, one a frame, in the order the frames enter K.

Output order does not depend on timing: pass-1 frames enter K in the order their pass-0 frames left it,
which is the order the jobs arrived.  So RTL and pysim record the same words.

In pysim ``Mux`` is three processes sharing its output under a priority lock (no polling: a harness runs
to the end of its events).  Its HLS body is one task that polls with ``empty()``, feedback first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np
import simpy

from waveflow.hw.arrayutils import array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave, StreamIF
from waveflow.utils.burst_io import StreamBurst

HERE = Path(__file__).resolve().parent
INCLUDE_DIR = "include"

#: The stream word: one element.
W = 64
#: The longest job, in words.
MAXN = 64
#: Jobs in the loop at once: the credit Mux starts with.
FMAX = 4

# ---------------------------------------------------------------------------
# 1. Schemas
# ---------------------------------------------------------------------------

U8 = IntField.specialize(bitwidth=8, signed=False, include_dir=INCLUDE_DIR)
U16 = IntField.specialize(bitwidth=16, signed=False, include_dir=INCLUDE_DIR)
UInt64 = IntField.specialize(bitwidth=64, signed=False, include_dir=INCLUDE_DIR)



class StatusCode(IntEnum):
    OK = 0


StatusCodeField = EnumField.specialize(enum_type=StatusCode)


class Job(DataList):
    """One job on ``s_in``; its ``n`` words follow, a burst."""
    elements = {
        "tx_id": {"schema": U16, "description": "the sender's id, echoed in the response and the status"},
        "n":     {"schema": U16, "description": "words in the job, 1 to 64"},
    }


class Tag(DataList):
    """Mux -> Post beside K, one a frame; also the head of a feedback frame, Post -> Mux."""
    elements = {
        "tx_id": {"schema": U16, "description": "the job"},
        "n":     {"schema": U16, "description": "words in the frame"},
        "npass": {"schema": U8,  "description": "0: the first pass through K; 1: the second"},
    }


class Crd(DataList):
    """Post -> Mux: a job has left the loop."""
    elements = {
        "tx_id": {"schema": U16, "description": "the job that left"},
    }


class Resp(DataList):
    """The header of a job's results on ``m_out``."""
    elements = {
        "tx_id": {"schema": U16, "description": "echo of the job's tx_id"},
        "n":     {"schema": U16, "description": "echo of n"},
    }


class Status(DataList):
    """One a job on ``m_status``, after its results."""
    elements = {
        "tx_id":  {"schema": U16,             "description": "the job it reports on"},
        "status": {"schema": StatusCodeField, "description": "OK"},
    }


#: In dependency order.
SCHEMA_CLASSES = [StatusCodeField, Job, Tag, Crd, Resp, Status]

# ---------------------------------------------------------------------------
# 2. The golden and the scenarios
# ---------------------------------------------------------------------------


def k_golden(x: np.ndarray) -> np.ndarray:
    """K: each word plus 1, modulo 2^64."""
    return (np.asarray(x, dtype=np.uint64) + np.uint64(1)).astype(np.uint64)


def _hdr(m) -> np.ndarray:
    return np.asarray(m.serialize(word_bw=W), dtype=np.uint64)


def jobs(kind: str = "test") -> list[tuple[int, np.ndarray]]:
    """``(tx_id, words)`` per job.  ``test``: more jobs than credits, every length from 1 to MAXN among
    them, so the loop runs full and Mux waits on credit."""
    rng = np.random.default_rng({"test": 5, "long": 6}[kind])
    lens = [1, 7, MAXN, 2, 33, 16, 5, MAXN, 3, 1] if kind == "test" else list(rng.integers(1, MAXN + 1, 40))
    out = []
    for i, n in enumerate(lens):
        x = rng.integers(0, 1 << 62, int(n), dtype=np.uint64)
        if i == 2:
            x[-1] = np.uint64(0xFFFFFFFFFFFFFFFF)      # wraps: K's add is modulo 2^64
        out.append((100 + i, x))
    return out


def stimulus(js) -> list[StreamBurst]:
    out = []
    for tx, x in js:
        out.append(StreamBurst(_hdr(Job(tx_id=tx, n=len(x))), True))
        out.append(StreamBurst(np.asarray(x, dtype=np.uint64), True))
    return out


def expected(js) -> dict[str, list[np.ndarray]]:
    """The composite's outputs: per job, in arrival order, ``Resp`` then the words plus 2; a ``Status``."""
    out, st = [], []
    for tx, x in js:
        out.append(_hdr(Resp(tx_id=tx, n=len(x))))
        out.append(k_golden(k_golden(x)))
        st.append(_hdr(Status(tx_id=tx, status=StatusCode.OK)))
    return {"m_out": out, "m_status": st}


def post_stimulus(js) -> dict[str, list[StreamBurst]]:
    """Post alone: each job's two passes back to back, as K would hand them over."""
    tags, data = [], []
    for tx, x in js:
        for p, words in ((0, k_golden(x)), (1, k_golden(k_golden(x)))):
            tags.append(StreamBurst(_hdr(Tag(tx_id=tx, n=len(x), npass=p)), True))
            data.append(StreamBurst(words, True))
    return {"s_tag": tags, "s_k": data}


def post_expected(js) -> dict[str, list[np.ndarray]]:
    fb, crd = [], []
    for tx, x in js:
        fb.append(_hdr(Tag(tx_id=tx, n=len(x), npass=1)))
        fb.append(k_golden(x))
        crd.append(_hdr(Crd(tx_id=tx)))
    return {**expected(js), "m_fb": fb, "m_crd": crd}


# ---------------------------------------------------------------------------
# 3. The three kernels
# ---------------------------------------------------------------------------


def _framed(name, sim, master: bool):
    cls = FramedStreamIFMaster if master else FramedStreamIFSlave
    return cls(name=name, sim=sim, bitwidth=W)


@dataclass
class Mux(FreeRunMod):
    """The only way into K: a feedback frame (``s_fb``) first, else a new job (``s_in``) when it holds a
    credit; a whole frame at a time.  Writes the frame's tag on ``m_tag`` and its words on ``m_k``."""

    cpp_kernel_name: ClassVar[str | None] = "ring_mux"
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = _framed(f"{self.name}_s_in", self.sim, False)
        self.s_fb = _framed(f"{self.name}_s_fb", self.sim, False)
        self.s_crd = _framed(f"{self.name}_s_crd", self.sim, False)
        self.m_k = _framed(f"{self.name}_m_k", self.sim, True)
        self.m_tag = _framed(f"{self.name}_m_tag", self.sim, True)
        for ep in (self.s_in, self.s_fb, self.s_crd, self.m_k, self.m_tag):
            self.add_endpoint(ep)

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        ports = ("s_in", "s_fb", "s_crd", "m_k", "m_tag")
        return KernelTask("ring_mux_task", "ring_mux_task.h", ports, template_args=(W, FMAX), framed=ports)

    # -- pysim: three processes, one priority lock on the output (feedback = 0 wins) ----------------
    def pre_sim(self) -> None:
        super().pre_sim()
        self._lock = simpy.PriorityResource(self.env, capacity=1)
        self._credit = simpy.Container(self.env, init=FMAX, capacity=FMAX)

    def run_proc(self):
        if self._sim_passive:
            return None
        return self._start()

    def _start(self):
        self.env.process(self._feedback())
        self.env.process(self._new_jobs())
        self.env.process(self._credits())
        yield self.env.timeout(0)

    def _send(self, prio: int, tag: Tag, words):
        with self._lock.request(priority=prio) as req:
            yield req
            yield from self.m_tag.write(tag)
            yield from self.m_k.write(np.asarray(words, dtype=np.uint64))

    def _feedback(self):
        while True:
            tag = yield from self.s_fb.get_schema(Tag)
            words = yield from self.s_fb.get()
            yield from self._send(0, tag, words)

    def _new_jobs(self):
        while True:
            yield self._credit.get(1)
            job = yield from self.s_in.get_schema(Job)
            words = yield from self.s_in.get()
            yield from self._send(1, Tag(tx_id=job.tx_id, n=job.n, npass=0), words)

    def _credits(self):
        while True:
            yield from self.s_crd.get_schema(Crd)
            yield self._credit.put(1)

    def run_iter(self):
        """Never called in pysim (:meth:`run_proc` runs the three processes); it marks this a leaf.  The
        HLS body (``src/ring_mux_task.h``) is one firing: absorb a credit, then a feedback frame or a
        new job."""
        raise NotImplementedError("Mux runs as three processes in pysim; see run_proc")
        yield  # pragma: no cover


@dataclass
class Inc(FreeRunMod):
    """The unit K: one frame in, the same frame out, each word plus 1.  Knows nothing of jobs or passes."""

    cpp_kernel_name: ClassVar[str | None] = "ring_inc"
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = _framed(f"{self.name}_s_in", self.sim, False)
        self.m_out = _framed(f"{self.name}_m_out", self.sim, True)
        for ep in (self.s_in, self.m_out):
            self.add_endpoint(ep)

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("ring_inc_task", "ring_inc_task.h", ("s_in", "m_out"), template_args=(W,),
                          framed=("s_in", "m_out"))

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": [StreamBurst(np.asarray(x, dtype=np.uint64), True) for _tx, x in jobs(kind)]}

    def golden(self, kind: str = "test") -> dict:
        return {"m_out": [k_golden(x) for _tx, x in jobs(kind)]}

    def run_iter(self):
        x = yield from self.s_in.get_array(UInt64, MAXN)
        y = k_golden(np.asarray(x.val, dtype=np.uint64))
        yield from self.m_out.write_array(UInt64, len(y), array(UInt64, y))


@dataclass
class Post(FreeRunMod):
    """After K: per frame, its tag then its words.  Pass 0: back to Mux, the whole frame buffered first.
    Pass 1: the answer (``Resp``, the words, ``Status``), then a credit to Mux."""

    cpp_kernel_name: ClassVar[str | None] = "ring_post"
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_tag = _framed(f"{self.name}_s_tag", self.sim, False)
        self.s_k = _framed(f"{self.name}_s_k", self.sim, False)
        self.m_fb = _framed(f"{self.name}_m_fb", self.sim, True)
        self.m_out = _framed(f"{self.name}_m_out", self.sim, True)
        self.m_status = _framed(f"{self.name}_m_status", self.sim, True)
        self.m_crd = _framed(f"{self.name}_m_crd", self.sim, True)
        for ep in (self.s_tag, self.s_k, self.m_fb, self.m_out, self.m_status, self.m_crd):
            self.add_endpoint(ep)

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        ports = ("s_tag", "s_k", "m_fb", "m_out", "m_status", "m_crd")
        return KernelTask("ring_post_task", "ring_post_task.h", ports, template_args=(W,), framed=ports)

    def workload(self, kind: str = "test") -> dict:
        return post_stimulus(jobs(kind))

    def golden(self, kind: str = "test") -> dict:
        return post_expected(jobs(kind))

    def run_iter(self):
        tag = yield from self.s_tag.get_schema(Tag)
        n = int(tag.n)
        x = yield from self.s_k.get_array(UInt64, n)
        words = np.asarray(x.val, dtype=np.uint64)
        if int(tag.npass) == 0:
            yield from self.m_fb.write(Tag(tx_id=tag.tx_id, n=n, npass=1))
            yield from self.m_fb.write_array(UInt64, n, array(UInt64, words))
            return
        yield from self.m_out.write(Resp(tx_id=tag.tx_id, n=n))
        yield from self.m_out.write_array(UInt64, n, array(UInt64, words))
        yield from self.m_status.write(Status(tx_id=tag.tx_id, status=StatusCode.OK))
        yield from self.m_crd.write(Crd(tx_id=tag.tx_id))


# ---------------------------------------------------------------------------
# 4. The composite: the loop
# ---------------------------------------------------------------------------

#: Link depths, in words.  Each is what the credit lets into it at most, so no write in the loop
#: blocks for good: at most FMAX jobs, each with one frame (a tag word + MAXN words) in flight.
DEPTH = {
    "k_in": MAXN,
    "k_out": MAXN,
    "tag": FMAX + 1,
    "fb": FMAX * (MAXN + 1),
    "crd": FMAX + 1,
}


@dataclass
class RingStub(FreeRunMod):
    """``s_in -> Mux -> Inc -> Post -> (m_out, m_status)``, with ``Post -> Mux`` twice (``fb``, ``crd``)
    and ``Mux -> Post`` beside K (``tag``)."""

    cpp_kernel_name: ClassVar[str | None] = "ring_stub"
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.mux = Mux(name=f"{self.name}_mux", sim=self.sim, clk=self.clk)
        self.k = Inc(name=f"{self.name}_k", sim=self.sim, clk=self.clk)
        self.post = Post(name=f"{self.name}_post", sim=self.sim, clk=self.clk)
        for c in (self.mux, self.k, self.post):
            self.add_comp(c)

        def link(name, master, slave):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=W,
                             framed=True, depth=DEPTH[name])
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        link("k_in", self.mux.m_k, self.k.s_in)
        link("k_out", self.k.m_out, self.post.s_k)
        link("tag", self.mux.m_tag, self.post.s_tag)
        link("fb", self.post.m_fb, self.mux.s_fb)       # the cycle: K's output back to its input
        link("crd", self.post.m_crd, self.mux.s_crd)    # and the credit, the second backward edge

        self.boundary = ["s_in", "m_out", "m_status"]
        self.s_in = self.mux.s_in
        self.m_out = self.post.m_out
        self.m_status = self.post.m_status

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": stimulus(jobs(kind))}

    def golden(self, kind: str = "test") -> dict:
        return expected(jobs(kind))
