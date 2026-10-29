"""sched.py -- the host's scheduler: job graphs to the engines' Job FIFOs (spec §5.6; plan §1.4).

``plans/ltbf_dse/ltbf_dse.md`` step 4.4.  Supporting infrastructure, not a contribution of the paper.
:class:`Scheduler` is the scheduler's state and decisions, with no simulator in it: the pysim host
(step 4.5) and the job-level timing simulator (step 5.5) drive it with the same calls --

* :meth:`Scheduler.add` a graph (:mod:`~examples.ltbf_dse.hw.graphs`); its jobs without
  dependencies become ready;
* :meth:`Scheduler.take` the jobs to write into an engine's ``qjob`` now (with a Ctrl write first when
  the engine's configuration changes), :meth:`Scheduler.take_host` a CPU or Python job for the host;
* :meth:`Scheduler.on_resp` each record popped from ``qresp`` -- an engine's job done, or an Ingest DMA
  chunk -- and :meth:`Scheduler.host_done` a host job done; each releases its successors;
* :attr:`Scheduler.credit` the cumulative count of ring chunks to return to the Ingest DMA, and
  :meth:`Scheduler.on_status` the DMA's status when idle (a drop after the last record).

**Policies.** ``dynamic``: a ready list an engine, ordered by class (real time first) then earliest
deadline (``prio = class << 24 | deadline in µs``), the head dispatched while the engine has room.
``static``: each engine takes its jobs in the graphs' order, waiting for the next one's dependencies
(the deterministic baseline and the RTL gate's policy).

**Flow control.** Every engine job in flight and every ring chunk can hold one record in ``qresp`` at
once, so jobs in flight stay at most ``qresp_depth - ring_depth``: the RespHub never blocks.  The ring
is returned in order (the DMA reuses its slots in order): a chunk's slot is free once every job reading
it is done, and the credit counts the oldest chunks freed without a gap.  A record's ``seq`` that jumps
means the DMA dropped symbols: their chunks' jobs and every job after them are dropped, and the
symbols counted (``overruns``).

**Errors.** An engine's error status is fatal (``docs/guide/systems/command_response.md``): the
scheduler stops dispatching and keeps the job and status in :attr:`Scheduler.error`.

**Cost.** Every decision is recorded in :attr:`Scheduler.trace` with the counters the A53's calibrated
families price: ``sched_ops.add`` / ``sched_ops.delete`` (the real ready list, through
``waveflow.cpu.calib.kernels.sched_ops``), ``dispatch`` (``n_dispatch``), ``release`` (``n_succ``,
``n_ready``; the new kernel of step 4.6, whose twin is :func:`release`) and ``ctrl``.

:func:`plan_memory` places a graph's buffers in Shared Mem from their lifetimes: two buffers share
space only when the dependencies order every use of one before every write of the other, so the plan
holds under any order a policy picks.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from examples.ltbf_dse.hw.graphs import Graph
from examples.ltbf_dse.hw.records import SRC_INGEST, SRC_SYS, SRC_VEC
from waveflow.cpu.calib.kernels.sched_ops import _Counters, tg_insert, tg_remove

#: Engines with a Job FIFO, and the record source of each.
HW = ("sys", "vec")
SRC_ENGINE = {SRC_SYS: "sys", SRC_VEC: "vec"}
#: The host's own job kinds: CPU work (on the Processor) and zero-time Python stages.
HOST = ("py", "cpu")
#: ``Resp.seq`` and ``tx_id`` are 16 bits.
SEQ_MOD = 1 << 16
#: Deadlines in the priority word: µs, 24 bits.
DL_BITS = 24


class MemoryPlanError(ValueError):
    """The graph's buffers do not fit in the Shared Mem given."""


def release(succ: list[int], pending: list[int]) -> tuple[list[int], dict[str, float]]:
    """A finished job's successor bookkeeping (the twin of step 4.6's ``release`` kernel): decrement
    each successor's count of unfinished dependencies; return those that reached zero, and the
    counters the calibrated model reads."""
    ready = []
    for s in succ:
        pending[s] -= 1
        if pending[s] == 0:
            ready.append(s)
    return ready, {"n_succ": float(len(succ)), "n_ready": float(len(ready))}


@dataclass
class _Graph:
    g: Graph
    base: int
    succ: list
    dsucc: list  # successors through data edges (a drop follows these)
    pending: list
    #: Each ring chunk's readers still to finish, by the chunk root's job id.
    ring_left: dict = field(default_factory=dict)


@dataclass
class Scheduler:
    """The host's scheduler (see the module docstring)."""

    policy: str = "dynamic"
    qresp_depth: int = 64
    ring_depth: int = 0
    #: Chunks a symbol (a dropped symbol's chunks are a gap of this many in ``seq``).
    n_chunks: int = 1

    def __post_init__(self) -> None:
        if self.policy not in ("dynamic", "static"):
            raise ValueError(f"policy {self.policy!r}")
        self.max_in_flight = int(self.qresp_depth) - int(self.ring_depth)
        if self.max_in_flight < 1:
            raise ValueError(f"qresp depth {self.qresp_depth} leaves no room for a job beside a "
                             f"ring of {self.ring_depth} chunks")  # fmt: skip
        self.graphs: list[_Graph] = []
        self.jobs: list = []  # global id -> Job
        self.where: list[tuple[int, int]] = []  # global id -> (graph index, local id)
        self.state: list[str] = []  # wait, ready, run, done, dropped
        self.ready: dict[str, list] = {
            e: [] for e in (*HW, *HOST)
        }  # dynamic: (gid, prio)
        self.order: dict[str, deque] = {e: deque() for e in (*HW, *HOST)}  # static
        self.cfg_now: dict[str, object] = {e: None for e in HW}
        #: (engine, tx_id) -> gid.  Each engine numbers its own jobs, so under the static policy an
        #: engine's ids (and so its Job FIFO words) do not depend on the other engine's timing.
        self.in_flight: dict[tuple[str, int], int] = {}
        self._tx = {e: 0 for e in HW}
        self._chunks: deque = deque()  # chunk roots (gid) in arrival order
        self._next_seq = 0
        self._arrived = 0  # chunks the DMA wrote (arrival index)
        self._slot_of: dict[int, int] = {}  # chunk root gid -> arrival index
        self._retired: set = set()
        #: Cumulative ring chunks returned (write it into the Ingest DMA's credit view).
        self.credit = 0
        self.trace: list[dict] = []
        self.error: tuple | None = None
        self.dispatched = {e: 0 for e in (*HW, *HOST)}
        self.ctrl_writes = 0
        self.misses = {0: 0, 1: 0}
        self.overruns = 0
        self.dropped = 0
        #: Jobs a drop reached: no-ops that still complete in dependency order (so the jobs after
        #: them through an order edge, and the ring slots they read, are released).
        self.skip: set = set()
        self.t_done: dict[int, float] = {}

    # -- graphs ---------------------------------------------------------------------------------

    def add(self, g: Graph) -> int:
        """Take a graph; its jobs without dependencies become ready.  Returns its global id base."""
        base = len(self.jobs)
        gi = len(self.graphs)
        succ = g.successors()
        pending = [len(j.deps) + len(j.after) for j in g.jobs]
        rec = _Graph(g, base, succ, g.successors(data_only=True), pending)
        self.graphs.append(rec)
        for j in g.jobs:
            self.jobs.append(j)
            self.where.append((gi, j.jid))
            self.state.append("wait")
        for j in g.jobs:
            gid = base + j.jid
            if j.engine == "ingest":
                rec.ring_left[j.jid] = sum(
                    1 for s in succ[j.jid] if set(g.jobs[s].reads) & set(j.writes)
                )
            elif self.policy == "static":
                self.order[j.engine].append(gid)
        for ch in g.chunks:
            self._chunks.append(base + ch)
        for j in g.jobs:
            if not j.deps and not j.after and j.engine != "ingest":
                self._make_ready(base + j.jid)
        return base

    def _prio(self, gid: int) -> int:
        j = self.jobs[gid]
        dl = (
            0xFFFFFF if math.isinf(j.deadline) else min(int(j.deadline * 1e6), 0xFFFFFF)
        )
        return (j.cls << DL_BITS) | dl

    def _make_ready(self, gid: int) -> None:
        self.state[gid] = "ready"
        if self.policy == "dynamic":
            e = self.jobs[gid].engine
            c = _Counters()
            n = len(self.ready[e])
            tg_insert(self.ready[e], (gid, self._prio(gid)), c)
            self.trace.append({"op": "sched_ops.add", "engine": e, "n_tasks": n,
                               "n_scanned": c.scanned, "n_moved": c.moved})  # fmt: skip

    def _next(self, e: str) -> int | None:
        """The engine's next job under the policy, without taking it."""
        if self.policy == "dynamic":
            return self.ready[e][0][0] if self.ready[e] else None
        q = self.order[e]
        while q and self.state[q[0]] == "dropped":
            q.popleft()
        return q[0] if q and self.state[q[0]] == "ready" else None

    def _pop(self, e: str, gid: int) -> None:
        if self.policy == "dynamic":
            c = _Counters()
            n = len(self.ready[e])
            tg_remove(self.ready[e], gid, c)
            self.trace.append({"op": "sched_ops.delete", "engine": e, "n_tasks": n,
                               "n_scanned": c.scanned, "n_moved": c.moved})  # fmt: skip
        else:
            self.order[e].popleft()

    # -- dispatch -------------------------------------------------------------------------------

    def take(self, engine: str, room: int | None = None) -> list[tuple]:
        """The writes for ``engine``'s ``qjob`` now: ``("ctrl", cfg)`` when the job needs another
        configuration, then ``("job", gid, tx_id)``; at most ``room`` jobs, within flow control.
        """
        out: list[tuple] = []
        while self.error is None and (room is None or room > 0):
            if len(self.in_flight) >= self.max_in_flight:
                break
            gid = self._next(engine)
            if gid is None:
                break
            self._pop(engine, gid)
            j = self.jobs[gid]
            if j.cfg is not None and j.cfg != self.cfg_now[engine]:
                self.cfg_now[engine] = j.cfg
                self.ctrl_writes += 1
                self.trace.append({"op": "ctrl", "engine": engine})
                out.append(("ctrl", j.cfg))
            while (engine, self._tx[engine]) in self.in_flight:
                self._tx[engine] = (self._tx[engine] + 1) % SEQ_MOD
            tx = self._tx[engine]
            self._tx[engine] = (tx + 1) % SEQ_MOD
            self.in_flight[engine, tx] = gid
            self.state[gid] = "run"
            self.dispatched[engine] += 1
            self.trace.append({"op": "dispatch", "engine": engine, "n_dispatch": 1})
            out.append(("job", gid, tx))
            if room is not None:
                room -= 1
        return out

    def take_host(self) -> int | None:
        """A ready host job (Python stages first, they take no time), or ``None``."""
        if self.error is not None:
            return None
        for e in HOST:
            gid = self._next(e)
            if gid is not None:
                self._pop(e, gid)
                self.state[gid] = "run"
                self.dispatched[e] += 1
                return gid
        return None

    # -- completions ----------------------------------------------------------------------------

    def on_resp(
        self, src: int, status: int, tx_id: int, seq: int, now: float
    ) -> int | None:
        """One ``qresp`` record.  Returns the finished job's id (``None`` for an error)."""
        if src == SRC_INGEST:
            return self._chunk(int(seq), now)
        gid = self.in_flight.pop((SRC_ENGINE[int(src)], int(tx_id)))
        if int(status):
            self.error = (gid, int(status))
            return None
        self._complete(gid, now)
        return gid

    def host_done(self, gid: int, now: float) -> None:
        self._complete(gid, now)

    def _chunk(self, seq: int, now: float) -> int:
        gap = (seq - self._next_seq) % SEQ_MOD
        if gap % max(1, self.n_chunks):
            raise ValueError(f"chunk seq {seq} after {self._next_seq - 1}: a gap of {gap} is not "
                             f"whole symbols of {self.n_chunks} chunks")  # fmt: skip
        for _ in range(gap):
            self._drop(self._chunks.popleft(), now)
        self.overruns += gap // max(1, self.n_chunks)
        self._next_seq = (seq + 1) % SEQ_MOD
        gid = self._chunks.popleft()
        self._slot_of[gid] = self._arrived
        self._arrived += 1
        self.state[gid] = "run"
        self._complete(gid, now)
        return gid

    def on_status(self, chunks: int, overruns: int, now: float = 0.0) -> None:
        """The Ingest DMA's status (``IngestStatus``), read when the scheduler is idle: a drop after
        the last chunk seen leaves no gap in ``seq`` until the next record, maybe never.  Once every
        chunk the DMA wrote has been seen, the overruns beyond those counted are the next symbols.
        """
        if chunks != self._arrived:
            return  # records still on their way; their seq gaps will tell
        for _ in range(int(overruns) - self.overruns):
            for _c in range(self.n_chunks):
                if self._chunks:
                    self._drop(self._chunks.popleft(), now)
            self._next_seq = (self._next_seq + self.n_chunks) % SEQ_MOD
            self.overruns += 1

    def slot(self, chunk_gid: int) -> int:
        """The ring slot the DMA wrote this chunk into."""
        return self._slot_of[chunk_gid] % max(1, self.ring_depth)

    def _complete(self, gid: int, now: float) -> None:
        """A job finished (or a dropped one reached its turn): release its successors; dropped ones
        among them complete at once, as no-ops."""
        work = [gid]
        while work:
            for s in self._done(work.pop(), now):
                if s in self.skip:
                    work.append(s)
                else:
                    self._make_ready(s)

    def _done(self, gid: int, now: float) -> list[int]:
        j = self.jobs[gid]
        noop = gid in self.skip
        self.state[gid] = "dropped" if noop else "done"
        self.t_done[gid] = now
        if noop:
            self.dropped += 1
        elif (
            now > j.deadline and j.engine != "ingest"
        ):  # a chunk is an arrival, not work
            self.misses[j.cls] += 1
        gi, lid = self.where[gid]
        rec = self.graphs[gi]
        # This job's reads of ring chunks: a chunk whose last reader finished frees its slot.
        for d in j.deps:
            if d in rec.ring_left and set(j.reads) & set(rec.g.jobs[d].writes):
                rec.ring_left[d] -= 1
                if rec.ring_left[d] == 0:
                    self._retire(rec.base + d)
        ready, counters = release(rec.succ[lid], rec.pending)
        self.trace.append({"op": "release", **counters})
        if j.engine == "ingest" and rec.ring_left.get(lid, 0) == 0:
            self._retire(gid)  # a chunk nobody reads
        return [rec.base + s for s in ready]

    def _retire(self, chunk_gid: int) -> None:
        if chunk_gid not in self._slot_of:
            return  # a chunk the DMA dropped holds no slot
        self._retired.add(self._slot_of[chunk_gid])
        while self.credit in self._retired:
            self._retired.discard(self.credit)
            self.credit += 1

    def _drop(self, gid: int, now: float) -> None:
        """A chunk the DMA dropped: it and every job its data reaches become no-ops, completing in
        dependency order; a job after one only through an order edge still runs."""
        stack = [gid]
        while stack:
            g = stack.pop()
            if g in self.skip:
                continue
            self.skip.add(g)
            gi, lid = self.where[g]
            rec = self.graphs[gi]
            stack.extend(rec.base + s for s in rec.dsucc[lid])
        self._complete(gid, now)  # the chunk itself has no predecessors

    # -- state ----------------------------------------------------------------------------------

    @property
    def finished(self) -> bool:
        return all(s in ("done", "dropped") for s in self.state)

    @property
    def idle(self) -> bool:
        """Nothing in flight and nothing ready: only arrivals can move it on."""
        return not self.in_flight and all(self._next(e) is None for e in (*HW, *HOST))


def ancestors(g: Graph) -> list[int]:
    """Each job's ancestors as a bit set (the graph is in topological order)."""
    anc = [0] * len(g.jobs)
    for j in g.jobs:
        a = 0
        for d in (*j.deps, *j.after):
            a |= anc[d] | (1 << d)
        anc[j.jid] = a
    return anc


def plan_memory(
    g: Graph, capacity: int, *, base: int = 0, align: int = 1
) -> dict[str, int]:
    """Place the graph's own buffers (not its ``external`` ones) in ``capacity`` words from ``base``:
    first fit in order of birth, two buffers sharing space only when every use of the earlier one is
    an ancestor of every writer of the later one.  Returns ``{buffer: word address}``; raises
    :class:`MemoryPlanError` when it does not fit."""
    anc = ancestors(g)
    users: dict[str, int] = {}
    writers: dict[str, list[int]] = {}
    for j in g.jobs:
        for b in j.reads:
            users[b] = users.get(b, 0) | (1 << j.jid)
        for b in j.writes:
            users[b] = users.get(b, 0) | (1 << j.jid)
            writers.setdefault(b, []).append(j.jid)
    own = [b for b in g.buffers if b not in g.external and b in writers]
    own.sort(key=lambda b: min(writers[b]))
    placed: list[tuple[int, int, int]] = []  # (offset, end, users)
    out: dict[str, int] = {}
    for b in own:
        before = ~0
        for w in writers[b]:
            before &= anc[w]  # jobs certainly finished before b's first write
        live = sorted((o, e) for o, e, u in placed if u & ~before)
        words = -(-g.buffers[b] // align) * align
        off = 0
        for o, e in live:
            if off + words <= o:
                break
            off = max(off, e)
        if off + words > capacity:
            raise MemoryPlanError(
                f"{g.name}: {b} ({words} words) does not fit in {capacity} words"
            )
        placed.append((off, off + words, users[b]))
        out[b] = base + off
    return out


def footprint(g: Graph, plan: dict[str, int], *, base: int = 0) -> int:
    """Words from ``base`` the plan uses."""
    return max((plan[b] - base + g.buffers[b] for b in plan), default=0)
