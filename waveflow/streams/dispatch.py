"""dispatch.py -- ``StreamDispatch``: one framed job stream out to a pool of ``n_out`` identical workers,
each job whole to a worker holding a credit.

The other direction from :class:`~waveflow.streams.arbiter.StreamArbiter` (``plans/stream_dispatch.md``):
the arbiter merges several clients into one kernel; the dispatcher spreads one client's jobs over several
copies of a kernel, so jobs of data-dependent length keep every copy busy where a fixed split would not.

* **A frame** (a job) is ``frame_bursts`` consecutive TLAST bursts, as the arbiter's and the demux's.  The
  block counts TLASTs and reads nothing else, so it knows no message format.
* **Credits.**  Each worker starts with ``credits`` of them, the jobs it may hold at once.  A job sent to
  worker ``k`` takes one of ``k``'s; a word ``k`` on ``s_free`` gives one back.  A word naming no worker,
  or a worker with nothing outstanding, is ignored and counted in :attr:`bad_credits`, so the block is never
  pushed past ``credits`` jobs a worker.
* **One action a firing, a returned credit first**: a word on ``s_free`` is taken back; else, if a job
  waits and some worker holds a credit, the job goes whole to the worker the policy picks; else nothing.
* **The policy** is fixed at build time: ``"first_free"`` -- the lowest-numbered worker holding a credit;
  ``"round_robin"`` -- the search starts one past the last worker chosen.

``s_free`` carries a worker's index, which is what a ``StreamArbiter`` merging the workers' results writes
on its ``m_sel``: ``dispatch -> workers -> arbiter``, the arbiter's ``m_sel`` into ``s_free``, closes the
loop.  Three rules a design keeps (``docs/guide/streams/dispatch.md``): the credit link holds every
outstanding credit (``n_out * credits`` words); a job is announced only when it is all there; a credit means
room for a whole job.

Which worker takes a job depends on when credits come back, so a run's assignment is a fact about its
timing: pysim and RTL agree on what comes out, not on which worker did it.  The HLS body
(``src/stream_dispatch_task.h``) tests ``s_free`` then ``s_in`` with ``empty()``; the model waits with
``wait_any`` on the same inputs in the same order, and on ``s_in`` only while some worker can take a job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave
from waveflow.streams.body_step import StreamBodyStep
from waveflow.utils.burst_io import StreamBurst

__all__ = ["StreamDispatch", "StreamDispatchStep", "POLICIES"]

#: The policies and their code in the HLS body's ``RR`` template argument.
POLICIES = {"first_free": 0, "round_robin": 1}


@dataclass
class StreamDispatch(FreeRunMod):
    """One framed input (``s_in``) out to ``n_out`` workers (``m_out_0`` .. ``m_out_<n_out-1>``), a whole
    frame at a time, each to a worker holding a credit; credits back on ``s_free``, a worker's index a word.

    Build parameters: ``n_out`` (2 to 16), ``bitwidth`` (every port's), ``frame_bursts`` (TLAST bursts a
    job, 1 to 255), ``credits`` (jobs a worker may hold at once, 1 to 255) and ``policy``
    (``"first_free"`` or ``"round_robin"``)."""

    cpp_kernel_name: ClassVar[str | None] = "stream_dispatch"
    #: Build inputs that are not template parameters: a fresh copy (the harness's DUT) keeps them.
    build_fields: ClassVar[tuple[str, ...]] = ("policy",)

    n_out: HwParam[int] = 2
    bitwidth: HwParam[int] = 32
    frame_bursts: HwParam[int] = 1
    credits: HwParam[int] = 1
    #: Not an ``HwParam`` (those are integers); it reaches the body as its ``RR`` template argument.
    policy: str = "first_free"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        n, w = int(self.n_out), int(self.bitwidth)
        if not 2 <= n <= 16:
            raise ValueError(f"{self.name}: n_out must be 2 to 16, got {n}")
        if not 1 <= int(self.frame_bursts) <= 255:
            raise ValueError(f"{self.name}: frame_bursts must be 1 to 255, got {self.frame_bursts}")
        if not 1 <= int(self.credits) <= 255:
            raise ValueError(f"{self.name}: credits must be 1 to 255, got {self.credits}")
        if self.policy not in POLICIES:
            raise ValueError(f"{self.name}: policy must be one of {sorted(POLICIES)}, got {self.policy!r}")
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=w)
        self.s_free = FramedStreamIFSlave(name=f"{self.name}_s_free", sim=self.sim, bitwidth=w)
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.s_free)
        self.m_out: list[FramedStreamIFMaster] = []
        for k in range(n):
            ep = FramedStreamIFMaster(name=f"{self.name}_m_out_{k}", sim=self.sim, bitwidth=w)
            setattr(self, f"m_out_{k}", ep)
            self.add_endpoint(ep)
            self.m_out.append(ep)
        #: Jobs each worker holds now (sent, not yet given back).  State across firings, as the body's.
        self.outstanding = [0] * n
        #: Round-robin: where the next search starts.
        self.next_k = 0
        #: Words on ``s_free`` that named no worker, or one with nothing outstanding: ignored.
        self.bad_credits = 0
        #: Jobs sent to each worker over the run, and the most each held at once.
        self.sent = [0] * n
        self.peak = [0] * n

    @property
    def ports(self) -> tuple[str, ...]:
        """The body's signature: the inputs, then the outputs in order (the pack last)."""
        return ("s_in", "s_free", *(f"m_out_{k}" for k in range(int(self.n_out))))

    def kernel_task(self):
        """The framework body, templated on the width, the frame, the credits, the policy and every port's
        word type."""
        from waveflow.hw.mem_stream import KernelTask

        return KernelTask("stream_dispatch_task", "stream_dispatch_task.h", self.ports,
                          template_args=(int(self.bitwidth), int(self.frame_bursts), int(self.credits),
                                         POLICIES[self.policy]),
                          framed=self.ports)

    def free_workers(self) -> list[int]:
        """The workers holding a credit, in the order this firing's search checks them."""
        n, c = int(self.n_out), int(self.credits)
        start = self.next_k if self.policy == "round_robin" else 0
        order = [(start + j) % n for j in range(n)]
        return [k for k in order if self.outstanding[k] < c]

    def take_back(self, k: int) -> None:
        """A word ``k`` on ``s_free``: worker ``k``'s credit back -- or, naming none outstanding, counted."""
        if 0 <= k < int(self.n_out) and self.outstanding[k] > 0:
            self.outstanding[k] -= 1
        else:
            self.bad_credits += 1

    def run_iter(self):
        """One action: a returned credit if one waits; else a job to the first free worker in the
        policy's order, whole; waiting on ``s_in`` only while some worker can take it."""
        free = self.free_workers()
        if free:
            i = yield from self.wait_any(self.s_free, self.s_in)
        else:
            i = yield from self.wait_any(self.s_free)
        if i == 0:
            words = yield from self.s_free.get()
            self.take_back(int(np.asarray(words, dtype=np.uint64).reshape(-1)[0]))
            return
        k = free[0]
        if self.policy == "round_robin":
            self.next_k = (k + 1) % int(self.n_out)
        self.outstanding[k] += 1
        self.sent[k] += 1
        self.peak[k] = max(self.peak[k], self.outstanding[k])
        for _ in range(int(self.frame_bursts)):
            words = yield from self.s_in.get()
            yield from self.m_out[k].write(words)

    def workload(self, kind: str = "test") -> dict:
        """The block's own scenario: jobs whose words name the job and burst (job ``f``, burst ``b``,
        word ``j``: ``f << 12 | b << 8 | j``), 1 to 9 words a burst -- and nothing on ``s_free``.  Its
        harness (the block alone, ``rtl.make("dispatch", ...)``) returns no credits, so each worker
        takes at most ``credits`` jobs; a pool (:class:`~waveflow.streams.rtl.DispatchPool`) returns
        them."""
        return {"s_in": jobs_of(int(self.frame_bursts), kind), "s_free": []}


def jobs_of(frame_bursts: int, kind: str = "test", n_jobs: int | None = None) -> list[StreamBurst]:
    """A job stream for a pool's scenario (ports of 32 bits or more): job ``f``, burst ``b``, word ``j`` is
    ``f << 12 | b << 8 | j``, so a job lost, doubled, split or merged is visible.  ``"test"``: 14 jobs of
    irregular lengths; ``"long"``: 40 jobs of 8 words a burst."""
    n = n_jobs if n_jobs is not None else {"test": 14, "long": 40}[kind]
    out = []
    for f in range(n):
        for b in range(frame_bursts):
            n_b = 8 if kind == "long" else 1 + (f * 5 + b * 3) % 9
            out.append(StreamBurst(np.asarray([(f << 12) | (b << 8) | j for j in range(n_b)],
                                              dtype=np.uint64), True))
    return out


class StreamDispatchStep(StreamBodyStep):
    """Copy ``stream_dispatch_task.h`` into a build (``output_dir`` relative to the build root)."""

    KEY = "stream_dispatch_task"
