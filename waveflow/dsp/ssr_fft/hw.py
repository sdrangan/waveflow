"""``SsrFft`` -- the full-rate SSR FFT as a Waveflow composite (F3 of ``plans/ssr_fft.md``).

One child per ``hls::task``, in the order :func:`.hls.task_instances` lists them::

    lanes_in -> tp0 .. tp{S-2} -> st0 -> cm0 -> ... -> st{S-1} -> rc -> rw =SOB=> rr -> lanes_out

Every arrow but one is a ``StreamIF`` carrying one ``RadixWord`` per beat (``R`` complex samples);
the one between ``rw`` and ``rr`` is a ``StreamOfBlocksIF`` (the reorder's frame buffer).  The
boundary is ``VitisFft``'s port group -- ``R`` input lanes, ``R`` output lanes, one sample a word --
so the two modules are interchangeable in a design, and share a testbench and a golden.

**The pysim is per frame, per child.**  Each child reads a frame, transforms it with the model's own
function for that block (:mod:`.model`), and writes it *cut-through*: the output starts ``lat``
cycles after the frame's **first** word arrived (``write_pipelined``), not after its last, because
the hardware streams -- a store-and-forward pysim would charge every child the frame transfer and
put ``3S`` frames of latency on a chain that has a handful.  ``lat`` is analytic here
(:func:`latency_cycles`); F5 calibrates it.

The C++ of each child is a wrapper over the generic body (:mod:`.hls`), so ``kernel_task`` names the
wrapper and the build writes it.  No arithmetic lives in this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import simpy

from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import (SobIFMaster, SobIFSlave, StreamIF, StreamIFMaster, StreamIFSlave,
                                   StreamOfBlocksIF)
from waveflow.hw.mem_stream import KernelTask
from waveflow.utils import complexutils as cx

from . import hls
from . import model as m
from .types import EdgeType, edge_types

#: In-flight frames a child may hold: read, transformed, not yet written.  Two is enough for the
#: largest latency here to overlap the next frame's intake (lat < L/R for every block but none).
MAX_INFLIGHT = 4


def block_type(geo: m.Geometry) -> type[DataArray]:
    """The reorder's block: one frame of raw ``rc`` words (``hls::stream_of_blocks<ap_uint<W>[L/R]>``)."""
    rc = {e.name: e for e in edge_types(geo)}["rc"]
    return DataArray.specialize(IntField.specialize(bitwidth=rc.bitwidth, signed=False),
                                max_shape=(geo.n_words,))


def latency_cycles(t: hls.TaskInstance) -> int:
    """First word in -> first word out, per task -- analytic, trimmed once against RTL (F5).

    The fixed terms were first csynth's pipeline depths (commutator +5, stage 5, lanes 2), which
    put the first frame ~1.9 cycles per task late against XSI at L = 16, 64, 1024 -- the pysim's own
    channel hop already charges some of it.  Trimmed by 2 each; the interval was exact before and
    after.

    A commutator delays every sample ``(R-1)*D`` ticks (its definition), plus its pipeline; a stage
    and a lane adaptor are their pipeline depth (csynth: 3-5); the SOB reader starts a frame a few
    cycles after the writer commits it, which is what ``reorder_write``'s ``n_words`` charges.
    """
    if t.kind == "commutator":
        return 3 * t.d + 3
    if t.kind == "reorder_pingpong":
        return t.d + 1                       # a whole frame in before its first word can leave
    if t.kind == "pass":
        return 0
    # reorder_read: the SOB reader re-enters (and re-acquires its lock) once a frame; 5 is what XSI
    # measures on the RadixWord boundary -- an interval of L/R + 5 (L/R + 4 with lanes=True).
    return {"stage": 3, "lanes_in": 0, "lanes_out": 0, "reorder_write": 0, "reorder_read": 5}[t.kind]


@dataclass
class SsrTask(FreeRunMod):
    """One task of an :class:`SsrFft`: its ports, its wrapper, and its frame-level pysim."""

    cpp_kernel_name: ClassVar[str | None] = None

    geo: m.Geometry = None
    task: hls.TaskInstance = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))
    timed: bool = True
    #: Which of this task's ports are the FFT's boundary: those carry frames as TLAST-delimited
    #: bursts in pysim, as the testbench's drivers and sinks do.
    boundary_ports: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        self._edges = {e.name: e for e in edge_types(self.geo) + hls.lane_edges(self.geo)}
        self._slots = simpy.Container(self.sim.env, init=MAX_INFLIGHT, capacity=MAX_INFLIGHT)
        self._wlock = simpy.Resource(self.sim.env, capacity=1)
        self.lat = latency_cycles(self.task)
        for name, ty in self.task.params:
            ep = self._make_endpoint(name, ty)
            setattr(self, name, ep)
            self.add_endpoint(ep)

    # -- structure ----------------------------------------------------------------------------
    def _edge_of(self, ty: str) -> EdgeType:
        return self._edges[ty.split("::e_")[1].split("::")[0]]

    def _make_endpoint(self, name: str, ty: str):
        if "stream_of_blocks" in ty:
            cls = SobIFMaster if name.startswith("m_") else SobIFSlave
            return cls(name=f"{self.name}_{name}", sim=self.sim, element_type=block_type(self.geo))
        cls = StreamIFMaster if name.startswith("m_") else StreamIFSlave
        e = self._edge_of(ty)
        return cls(name=f"{self.name}_{name}", sim=self.sim, bitwidth=e.bitwidth,
                   has_tlast=name in self.boundary_ports)

    def kernel_task(self) -> KernelTask:
        return KernelTask(f"{hls.config_namespace(self.geo)}_{self.task.inst}",
                          hls.wrappers_header(self.geo),
                          tuple(name for name, _ in self.task.params))

    # -- pysim helpers ------------------------------------------------------------------------
    def _get_words(self, ep, n: int):
        """Exactly *n* words from *ep* (``get`` may deliver them in pieces), and when the first
        arrived: the anchor a cut-through write is timed from."""
        parts, have, t_first = [], 0, None
        while have < n:
            got = np.asarray((yield from ep.get(nwords_max=n - have)))
            if got.shape[0] == 0:
                raise RuntimeError(f"{self.name}: {ep.name} stopped mid-frame")
            if t_first is None:
                t_first = self.now - (got.shape[0] - 1) * self.clk.period
            parts.append(got)
            have += got.shape[0]
        return np.concatenate(parts, axis=0), t_first

    def _unpack(self, words, e: EdgeType) -> tuple[np.ndarray, np.ndarray]:
        v = read_array(words, elem_type=e.elem, word_bw=e.bitwidth, shape=words.shape[0] * e.R).val
        return (np.asarray(v["re"], dtype=np.int64).reshape(-1, e.R),
                np.asarray(v["im"], dtype=np.int64).reshape(-1, e.R))

    def _pack(self, re: np.ndarray, im: np.ndarray, e: EdgeType):
        return write_array(cx.make_complex(re.reshape(-1), im.reshape(-1), e.fmt),
                           elem_type=e.elem, word_bw=e.bitwidth)

    @staticmethod
    def _store(ep, words, start: float, lock: simpy.Resource):
        """The deferred write: a pure function of its arguments, as ``call_after`` requires."""
        with lock.request() as req:
            yield req
            yield from ep.write_pipelined(words, start)

    def _emit(self, ep, words, t_first: float):
        start = t_first + self.lat * self.clk.period if self.timed else self.now
        self.call_after(0, self._store, ep, words, start, self._wlock, slot=self._slots)


@dataclass
class SsrStream(SsrTask):
    """A word-in, word-out child: a commutator, a stage, or the ping-pong reorder."""

    def run_iter(self):
        n = self.geo.n_words
        yield self._slots.get(1)
        e_in = self._edge_of(dict(self.task.params)["s_in"])
        e_out = self._edge_of(dict(self.task.params)["m_out"])
        words, t0 = yield from self._get_words(self.s_in, n)
        if self.task.kind == "pass":
            self._emit(self.m_out, words, t0)
            return
        if self.task.kind == "reorder_pingpong":
            out = np.empty_like(words)
            out[m.reorder_word_perm(self.geo)] = words      # whole words: no unpacking needed
            self._emit(self.m_out, out, t0)
            return
        re, im = self._unpack(words, e_in)
        if self.task.kind == "stage":
            re, im = m.stage(self.geo, self.task.s, re, im)
        else:
            re, im = m.commute(re, self.task.d), m.commute(im, self.task.d)
        self._emit(self.m_out, self._pack(re, im, e_out), t0)


@dataclass
class SsrLanes(SsrTask):
    """The boundary: ``R`` sample lanes joined into words (``lanes_in``), or split (``lanes_out``)."""

    def run_iter(self):
        from waveflow.vitis_l1.hw import _pack_complex, _unpack_complex
        n, R = self.geo.n_words, self.geo.R
        yield self._slots.get(1)
        if self.task.kind == "lanes_in":
            got: dict = {}

            def lane(j):
                got[j] = yield from self._get_words(getattr(self, f"s_in_{j}"), n)

            yield self.env.all_of([self.env.process(lane(j)) for j in range(R)])
            t0 = min(got[j][1] for j in range(R))
            re = np.zeros((n, R), dtype=np.int64)
            im = np.zeros((n, R), dtype=np.int64)
            for j in range(R):
                re[:, j], im[:, j] = _unpack_complex(got[j][0], self.geo.in_w)
            self._emit(self.m_out, self._pack(re, im, self._edges["in"]), t0)
        else:
            e_in = self._edge_of(dict(self.task.params)["s_in"])
            words, t0 = yield from self._get_words(self.s_in, n)
            re, im = self._unpack(words, e_in)
            w = self.geo.out_fmt.W
            lanes = [_pack_complex(re[:, j], im[:, j], w) for j in range(R)]
            start = t0 + self.lat * self.clk.period if self.timed else self.now
            self.call_after(0, self._store_lanes, [getattr(self, f"m_out_{j}") for j in range(R)],
                            lanes, start, self._wlock, slot=self._slots)

    @staticmethod
    def _store_lanes(eps, lanes, start: float, lock: simpy.Resource):
        """All ``R`` lanes at once: they are parallel ports, and one after another would cost ``R``
        frame transfers instead of one."""
        with lock.request() as req:
            yield req
            env = eps[0].env

            def one(ep, w):
                yield from ep.write_pipelined(w, start)

            yield env.all_of([env.process(one(ep, w)) for ep, w in zip(eps, lanes)])


@dataclass
class SsrReorder(SsrTask):
    """The reorder's frame buffer: the writer fills a block at the natural word addresses, the
    reader streams it out in order (``hls::stream_of_blocks``, depth 2)."""

    def run_iter(self):
        n = self.geo.n_words
        if self.task.kind == "reorder_write":
            block = yield from self.m_blk.acquire_write()        # the C++ locks before it reads
            words, _ = yield from self._get_words(self.s_in, n)
            buf = np.empty_like(words)
            buf[m.reorder_word_perm(self.geo)] = words
            block.val = buf
            yield from self.m_blk.commit_write(block)
        else:
            block = yield from self.s_blk.acquire_read()
            start = self.now + (self.lat * self.clk.period if self.timed else 0)
            yield from self.m_out.write_pipelined(np.asarray(block.val), start)
            yield from self.s_blk.release_read()


_KIND_CLASS = {"commutator": SsrStream, "stage": SsrStream, "lanes_in": SsrLanes,
               "lanes_out": SsrLanes, "reorder_write": SsrReorder, "reorder_read": SsrReorder,
               "reorder_pingpong": SsrStream, "pass": SsrStream}


@dataclass
class SsrFft(FreeRunMod):
    """A full-rate SSR FFT: ``R`` samples a cycle in and out, a new frame every ``L/R`` cycles.

    Bit-exact with ``VitisFft`` (and so with AMD's library) for the same parameters.  Its ports are
    one ``RadixWord`` stream each way -- ``s_in`` and ``m_out``, ``R`` complex samples a beat, the
    unit every internal edge carries -- or, with ``lanes=True``, ``VitisFft``'s ``R``-lane port group
    (``s_in_0 .. s_in_{R-1}``, ``m_out_0 ..``, one sample a word, as lists ``s_in`` / ``m_out``).
    """

    cpp_kernel_name: ClassVar[str | None] = "ssr_fft"
    cpp_namespace: ClassVar[str | None] = "ssr_fft_impl"

    L: HwParam[int] = 64
    in_w: HwParam[int] = 16
    in_i: HwParam[int] = 2
    tw_w: HwParam[int] = 18
    tw_i: HwParam[int] = 2
    #: The reorder's frame buffer: ``"sob"`` -- a ``stream_of_blocks`` between a writer and a reader
    #: task -- or ``"pingpong"``, both halves inside one task.
    reorder: str = "sob"
    #: ``False`` (default): one ``RadixWord`` port each way.  ``True``: ``VitisFft``'s ``R``-lane port
    #: group, joined and split by two adaptor tasks -- a drop-in for ``VitisFft``.
    lanes: bool = False
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))
    timed: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        self.geo = m.Geometry(int(self.L), int(self.in_w), int(self.in_i), int(self.tw_w),
                              int(self.tw_i))
        self.R = self.geo.R
        self.out_fmt = self.geo.out_fmt
        self.tasks: list[SsrTask] = []
        tis = hls.task_instances(self.geo, reorder=self.reorder, lanes=self.lanes)
        for k, t in enumerate(tis):
            ports = tuple(n for n, _ in t.params
                          if (k == 0 and n.startswith("s_in")) or
                          (k == len(tis) - 1 and n.startswith("m_out")))
            c = _KIND_CLASS[t.kind](name=f"{self.name}_{t.inst}", sim=self.sim, geo=self.geo, task=t,
                                    clk=self.clk, timed=self.timed, boundary_ports=ports)
            self.tasks.append(c)
            self.add_comp(c)
        self._wire()
        if self.lanes:
            self.s_in = [getattr(self.tasks[0], f"s_in_{j}") for j in range(self.R)]
            self.m_out = [getattr(self.tasks[-1], f"m_out_{j}") for j in range(self.R)]
            for j in range(self.R):
                setattr(self, f"s_in_{j}", self.s_in[j])
                setattr(self, f"m_out_{j}", self.m_out[j])
            self.boundary = ([f"s_in_{j}" for j in range(self.R)]
                             + [f"m_out_{j}" for j in range(self.R)])
        else:
            self.s_in = self.tasks[0].s_in
            self.m_out = self.tasks[-1].m_out
            self.boundary = ["s_in", "m_out"]
        self.extra_includes = ("hls_streamofblocks.h",)

    def _wire(self) -> None:
        """One interface per edge: each child's master to the next child's slave."""
        for a, b in zip(self.tasks, self.tasks[1:]):
            if a.task.kind == "reorder_write":
                i = StreamOfBlocksIF(name=f"{self.name}_blk_if", sim=self.sim, clk=self.clk,
                                     element_type=block_type(self.geo))
                i.bind("master", a.m_blk)
                i.bind("slave", b.s_blk)
            else:
                out_ty = dict(a.task.params)["m_out"]
                e = a._edge_of(out_ty)
                i = StreamIF(name=f"{self.name}_{a.task.inst}_if", sim=self.sim, clk=self.clk,
                             bitwidth=e.bitwidth)
                i.bind("master", a.m_out)
                i.bind("slave", b.s_in)
            self.add_if(i)
