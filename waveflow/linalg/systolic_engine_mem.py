"""The systolic engine behind memory: the outer loop for matrices larger than the engine.

``SystolicEngineMem`` multiplies an ``M × K`` matrix ``A`` by a ``K × N`` matrix ``B`` held in memory,
writing ``C = q(A·B)`` (or ``q(Aᴴ·B)``) back to memory, one job descriptor per product on ``s_job``
and one :class:`EngineDone` per job on ``s_done`` (``plans/systolic_characterization.md``, Part B):

    s_job -> EngineSeq -> MemRStream (in-band) -> SystolicEngine -> EngineWriter -> MemWStream -> s_done

The loop is output-stationary: ``C`` is covered in tiles of ``Mmax × Nmax``, and each tile is one engine
command of ``npanels = K / Kmax`` panels, accumulated exactly on chip and rounded once.  The sequencer
sends the memory reader ``MemRCmd(A_0, fwd 1) | EngineCmd | MemRCmd(B_0) | MemRCmd(A_1) | ...`` -- the
reader relays the command header ahead of the first operand -- and the writer turns each tile's
``Resp | C`` into ``MemWCmd(C tile) | C``, with the job's ``EngineDone`` echoed on its last tile.

**Memory layout: block-major** (:func:`block_major`), so every panel and tile is one burst:
``A``'s panel ``(i, p)`` (``Mmax × Kmax``; ``Kmax × Mmax`` for ``MUL_AH``) at ``a_base + (i·P + p)·wa``,
``B``'s panel ``(p, j)`` (``Kmax × Nmax``) at ``b_base + (p·NT + j)·wb``, ``C``'s tile ``(i, j)``
(``Mmax × Nmax``) at ``c_base + (i·NT + j)·wc``, row-major memory elements, word addresses.  A job needs
``M``, ``N``, ``K`` multiples of ``Mmax``, ``Nmax``, ``Kmax`` and ``K / Kmax <= Pmax`` (pad with zeros).

**The pointer-FIFO rule** (``tests/linalg/cpp/unit_bench_tasks.h``): an in-band reader and writer in one
generated top must fire equally often, so per tile the writer adds ``2·npanels - 1`` empty writes to
its one data write, and a refused job costs one empty read for its one done.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import FramedStreamIFSlave, StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask, MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.linalg import matmul as mm
from waveflow.linalg.lanes import DEFAULT_WORD_BITS, from_words, nwords, to_words
from waveflow.linalg.message import U8, U16, U32, Status
from waveflow.linalg.systolic import MatmulOp
from waveflow.linalg.systolic_engine import (CTRL_BITS, EngineCmd, EngineResp, EngineStatus,
                                             SystolicEngine, command)
from waveflow.simulation.simobj import ProcessGen
from waveflow.utils.fixputils import Format

#: The task bodies, in ``waveflow/build/``.
MEM_HEADERS = ("systolic_engine_mem_task.h",)


class EngineJobDesc(DataList):
    """A product on ``s_job``: ``C = q(A·B)`` (or ``Aᴴ``), the operands and the result in memory."""

    include_filename: ClassVar[str | None] = "wf_engine_job_desc.h"
    elements: ClassVar[dict] = {
        "tx_id": {"schema": U16, "description": "the job's id, echoed in its EngineDone"},
        "op": {"schema": U8, "description": "a MatmulOp: MUL or MUL_AH"},
        "M": {"schema": U16, "description": "rows of C (a multiple of Mmax)"},
        "K": {"schema": U16, "description": "the inner dimension (a multiple of Kmax, at most Pmax·Kmax)"},
        "N": {"schema": U16, "description": "columns of C (a multiple of Nmax)"},
        "a_base": {"schema": U32, "description": "A's first word (block-major panels)"},
        "b_base": {"schema": U32, "description": "B's first word (block-major panels)"},
        "c_base": {"schema": U32, "description": "C's first word (block-major tiles)"},
    }


class EngineDone(DataList):
    """One per job on ``s_done``, after its last tile is in memory."""

    include_filename: ClassVar[str | None] = "wf_engine_done.h"
    elements: ClassVar[dict] = {
        "tx_id": {"schema": U16, "description": "the job's"},
        "status": {"schema": U8, "description": "OK, or BAD_OP / BAD_DIMS (the job was refused)"},
    }


class EngineTile(DataList):
    """The sequencer's word to the writer, per tile (or per refused job)."""

    include_filename: ClassVar[str | None] = "wf_engine_tile.h"
    elements: ClassVar[dict] = {
        "c_addr": {"schema": U32, "description": "the tile's first word"},
        "tx_id": {"schema": U16, "description": "the job's"},
        "status": {"schema": U8, "description": "OK, or the refused job's status (no tile)"},
        "last": {"schema": U8, "description": "1 on the job's last tile: echo its EngineDone"},
        "empties": {"schema": U16, "description": "empty writes to balance the tile's reads"},
    }


MEM_SCHEMAS = (EngineJobDesc, EngineDone, EngineTile, MemRCmd, MemWCmd)


def job_status(d, *, Mmax: int, Kmax: int, Nmax: int, Pmax: int) -> Status:
    """Whether the wrapper can run job *d* (``systolic_engine_mem_task.h``)."""
    if int(d.op) not in (MatmulOp.MUL, MatmulOp.MUL_AH):
        return Status.BAD_OP
    M, K, N = int(d.M), int(d.K), int(d.N)
    if M < Mmax or K < Kmax or N < Nmax or M % Mmax or K % Kmax or N % Nmax or K // Kmax > Pmax:
        return Status.BAD_DIMS
    return Status.OK


def block_major(re, im, fmt: Format, rows: int, cols: int, *, lane_bits: int, word_bits: int,
                order: str = "row") -> np.ndarray:
    """A matrix as consecutive ``rows × cols`` blocks, each row-major memory elements: blocks in
    row-major order of the block grid (``order="row"``) or column-major (``"col"``)."""
    re, im = np.asarray(re), np.asarray(im)
    br, bc = re.shape[0] // rows, re.shape[1] // cols
    grid = [(i, j) for i in range(br) for j in range(bc)] if order == "row" else \
        [(i, j) for j in range(bc) for i in range(br)]
    parts = [to_words(re[i * rows:(i + 1) * rows, j * cols:(j + 1) * cols],
                      im[i * rows:(i + 1) * rows, j * cols:(j + 1) * cols], fmt, lane_bits, word_bits)
             for i, j in grid]
    return np.concatenate(parts).astype(np.uint64)


@dataclass
class _MemPart(FreeRunMod):
    word_bits: HwParam[int] = DEFAULT_WORD_BITS
    Mmax: HwParam[int] = 8
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    Pmax: HwParam[int] = 4
    lane_bits: HwParam[int] = 16
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def _ep(self, cls, name: str, bits: int, tlast: bool):
        ep = cls(name=f"{self.name}_{name}", sim=self.sim, bitwidth=int(bits), has_tlast=tlast)
        self.add_endpoint(ep)
        return ep

    def words(self, n_elems: int) -> int:
        return nwords(int(n_elems), int(self.lane_bits), int(self.word_bits))

    def _targs(self) -> tuple:
        return (int(self.word_bits), int(self.Mmax), int(self.Kmax), int(self.Nmax), int(self.Pmax),
                self.words(int(self.Mmax) * int(self.Kmax)),
                self.words(int(self.Kmax) * int(self.Nmax)),
                self.words(int(self.Mmax) * int(self.Nmax)))


@dataclass
class EngineSeq(_MemPart):
    """The output-stationary loop: per ``C`` tile, the engine command and its panels' reads."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_engine_seq"

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.word_bits)
        self.s_job = self._ep(FramedStreamIFSlave, "s_job", w, True)
        self.cmd_out = self._ep(StreamIFMaster, "cmd_out", w, True)
        self.tile_out = self._ep(StreamIFMaster, "tile_out", CTRL_BITS, False)

    def kernel_task(self) -> KernelTask:
        return KernelTask("systolic_engine_seq_task", MEM_HEADERS[0], ("s_job", "cmd_out", "tile_out"),
                          framed=("s_job",), template_args=self._targs())

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.word_bits)
        d = yield from self.s_job.get_schema(EngineJobDesc)
        Mm, Km, Nm = int(self.Mmax), int(self.Kmax), int(self.Nmax)
        st = job_status(d, Mmax=Mm, Kmax=Km, Nmax=Nm, Pmax=int(self.Pmax))

        def send(msg) -> ProcessGen[None]:
            yield from self.cmd_out.write(np.asarray(msg.serialize(word_bw=w), np.uint64))

        if st != Status.OK:
            t = EngineTile()
            t.c_addr, t.tx_id, t.status, t.last, t.empties = 0, int(d.tx_id), int(st), 1, 0
            yield from self.tile_out.write(np.asarray(t.serialize(word_bw=CTRL_BITS), np.uint64))
            r = MemRCmd()
            r.addr, r.len, r.fwd_bursts = 0, 0, 0      # one empty read for the done's one write
            yield from send(r)
            return
        MT, NT, P = int(d.M) // Mm, int(d.N) // Nm, int(d.K) // Km
        wa, wb, wc = self.words(Mm * Km), self.words(Km * Nm), self.words(Mm * Nm)
        for i in range(MT):
            for j in range(NT):
                t = EngineTile()
                t.c_addr = int(d.c_base) + (i * NT + j) * wc
                t.tx_id, t.status, t.last = int(d.tx_id), int(Status.OK), int(i == MT - 1 and j == NT - 1)
                t.empties = 2 * P - 1
                yield from self.tile_out.write(np.asarray(t.serialize(word_bw=CTRL_BITS), np.uint64))
                for p in range(P):
                    ra = MemRCmd()
                    ra.addr, ra.len, ra.fwd_bursts = int(d.a_base) + (i * P + p) * wa, wa, int(p == 0)
                    yield from send(ra)
                    if p == 0:
                        yield from send(command(int(d.tx_id), int(d.op), Mm, Km, Nm, npanels=P))
                    rb = MemRCmd()
                    rb.addr, rb.len, rb.fwd_bursts = int(d.b_base) + (p * NT + j) * wb, wb, 0
                    yield from send(rb)


@dataclass
class EngineWriter(_MemPart):
    """Per tile, the engine's ``C`` into a memory write; the job's ``EngineDone`` on its last."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_engine_writer"

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.word_bits)
        self.tile_in = self._ep(StreamIFSlave, "tile_in", CTRL_BITS, False)
        self.c_in = self._ep(StreamIFSlave, "c_in", w, True)
        self.st_in = self._ep(StreamIFSlave, "st_in", CTRL_BITS, True)
        self.w_out = self._ep(StreamIFMaster, "w_out", w, True)

    def kernel_task(self) -> KernelTask:
        return KernelTask("systolic_engine_writer_task", MEM_HEADERS[0],
                          ("tile_in", "c_in", "st_in", "w_out"), template_args=self._targs())

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.word_bits)
        t = yield from self.tile_in.get_schema(EngineTile)

        def send(msg) -> ProcessGen[None]:
            yield from self.w_out.write(np.asarray(msg.serialize(word_bw=w), np.uint64))

        done = EngineDone()
        done.tx_id, done.status = int(t.tx_id), int(t.status)
        if int(t.status) != Status.OK:                 # a refused job: its done, no tile
            c = MemWCmd()
            c.addr, c.len, c.fwd_bursts = 0, 0, 1
            yield from send(c)
            yield from send(done)
            return
        yield from self.c_in.get_schema(EngineResp)
        cw = yield from self.c_in.get()
        yield from self.st_in.get_schema(EngineStatus)
        c = MemWCmd()
        c.addr, c.len, c.fwd_bursts = int(t.c_addr), len(cw), int(t.last)
        yield from send(c)
        if int(t.last):
            yield from send(done)
        yield from self.w_out.write(np.asarray(cw))
        for _ in range(int(t.empties)):
            e = MemWCmd()
            e.addr, e.len, e.fwd_bursts = 0, 0, 0
            yield from send(e)


@dataclass
class SystolicEngineMem(_MemPart):
    """The engine between an in-band memory reader and writer (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_engine_mem"
    L: HwParam[int] = 4
    R: HwParam[int] = 4
    C: HwParam[int] = 8
    form: HwParam[int] = 4
    sob_depth: HwParam[int] = 2
    a: Format | None = None
    b: Format | None = None
    c: Format | None = None
    build_fields: ClassVar[tuple[str, ...]] = ("a", "b", "c")

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.word_bits)
        base = {f: getattr(self, f) for f in ("word_bits", "Mmax", "Kmax", "Nmax", "Pmax",
                                              "lane_bits")}
        base = {**base, "sim": self.sim, "clk": self.clk}
        self.seq = EngineSeq(name=f"{self.name}_seq", **base)
        self.rstream = MemRStream(name=f"{self.name}_memr", sim=self.sim, mem_dwidth=w, inband=True,
                                  clk=self.clk)
        self.engine = SystolicEngine(name=f"{self.name}_engine", L=self.L, R=self.R, C=self.C,
                                     form=self.form, sob_depth=self.sob_depth, a=self.a, b=self.b,
                                     c=self.c, **base)
        self.writer = EngineWriter(name=f"{self.name}_writer", **base)
        self.wstream = MemWStream(name=f"{self.name}_memw", sim=self.sim, mem_dwidth=w, inband=True,
                                  emit_done=True, clk=self.clk)
        for comp in (self.seq, self.rstream, self.engine, self.writer, self.wstream):
            self.add_comp(comp)
        links = (("rd_cmd", self.seq.cmd_out, self.rstream.s_cmd, w, True, None),
                 ("rd_data", self.rstream.m_out, self.engine.s_in, w, True, None),
                 ("tile", self.seq.tile_out, self.writer.tile_in, CTRL_BITS, False, 4),
                 ("cout", self.engine.s_out, self.writer.c_in, w, True, None),
                 ("cst", self.engine.m_status, self.writer.st_in, CTRL_BITS, True, None),
                 ("wr", self.writer.w_out, self.wstream.s_in, w, True, None))
        for name, m, s, bits, framed, depth in links:
            kw = {"framed": True} if framed else {}
            if depth:
                kw["depth"] = depth
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                             bitwidth=bits, **kw)
            iface.bind("master", m)
            iface.bind("slave", s)
            self.add_if(iface)
        self.boundary = ["s_job", "m_in", "m_out", "s_done"]
        self.s_job = self.seq.s_job
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done
        self.extra_includes = ["hls_streamofblocks.h"]

    def linalg_parts(self):
        from waveflow.linalg.build import LinalgParts

        return LinalgParts((), MEM_HEADERS, MEM_SCHEMAS)

    # -- scenarios ----------------------------------------------------------------------------------
    def jobs(self, kind: str = "test") -> list[dict]:
        Mm, Km, Nm, P = int(self.Mmax), int(self.Kmax), int(self.Nmax), int(self.Pmax)
        if kind == "test":
            return [dict(op=MatmulOp.MUL, M=2 * Mm, K=2 * Km, N=2 * Nm),
                    dict(op=MatmulOp.MUL_AH, M=Mm, K=P * Km, N=Nm),
                    dict(op=MatmulOp.MUL, M=Mm, K=Km, N=Nm)]
        if kind == "big":
            return [dict(op=MatmulOp.MUL, M=4 * Mm, K=P * Km, N=2 * Nm)]
        raise ValueError(f"no workload {kind!r}")

    def _layout(self, kind: str):
        """Per job: its descriptor, the operands, and its words in the memory image."""
        rng = np.random.default_rng(13)
        lb, w = int(self.lane_bits), int(self.word_bits)
        Mm, Km, Nm = int(self.Mmax), int(self.Kmax), int(self.Nmax)
        cur, out = 0, []
        for j, kw in enumerate(self.jobs(kind)):
            op, M, K, N = kw["op"], kw["M"], kw["K"], kw["N"]
            ah = op == MatmulOp.MUL_AH
            ash = (K, M) if ah else (M, K)
            lo = lambda f: -(1 << (f.W - 1))  # noqa: E731
            ar, ai = (rng.integers(lo(self.a), -lo(self.a), ash) for _ in range(2))
            br, bi = (rng.integers(lo(self.b), -lo(self.b), (K, N)) for _ in range(2))
            # A's panels (i, p): Mmax x Kmax blocks, i outer -- for A^H the stored k x m blocks, the
            # panel (p, i) of the stored matrix taken in the same (i, p) order
            if ah:
                aw = np.concatenate([to_words(ar[p * Km:(p + 1) * Km, i * Mm:(i + 1) * Mm],
                                              ai[p * Km:(p + 1) * Km, i * Mm:(i + 1) * Mm],
                                              self.a, lb, w)
                                     for i in range(M // Mm) for p in range(K // Km)])
            else:
                aw = block_major(ar, ai, self.a, Mm, Km, lane_bits=lb, word_bits=w, order="row")
            bw = block_major(br, bi, self.b, Km, Nm, lane_bits=lb, word_bits=w, order="row")
            wc = self.words(M * N)
            d = EngineJobDesc()
            d.tx_id, d.op, d.M, d.K, d.N = 100 + j, int(op), M, K, N
            d.a_base, d.b_base, d.c_base = cur, cur + len(aw), cur + len(aw) + len(bw)
            cur = int(d.c_base) + wc
            out.append((d, (ar, ai), (br, bi), aw, bw))
        return out, cur

    def workload(self, kind: str = "test") -> dict:
        from waveflow.utils.burst_io import StreamBurst

        w = int(self.word_bits)
        jobs, size = self._layout(kind)
        image = np.zeros(size + 16, np.uint64)
        bursts = []
        for d, _a, _b, aw, bw in jobs:
            image[int(d.a_base):int(d.a_base) + len(aw)] = aw
            image[int(d.b_base):int(d.b_base) + len(bw)] = bw
            bursts.append(StreamBurst(np.asarray(d.serialize(word_bw=w), np.uint64), True))
        return {"s_job": bursts, "m_in": image}

    def golden(self, kind: str = "test") -> dict:
        jobs, _size = self._layout(kind)
        out = []
        for d, _a, _b, _aw, _bw in jobs:
            done = EngineDone()
            done.tx_id, done.status = int(d.tx_id), int(Status.OK)
            out.append(np.asarray(done.serialize(word_bw=int(self.word_bits)), np.uint64))
        return {"s_done": out}

    def expected_c(self, kind: str = "test") -> list[tuple[int, np.ndarray]]:
        """``(c_base, words)`` per job: ``C`` from the bit-exact model, block-major tiles."""
        lb, w = int(self.lane_bits), int(self.word_bits)
        Mm, Nm = int(self.Mmax), int(self.Nmax)
        jobs, _size = self._layout(kind)
        out = []
        for d, (ar, ai), (br, bi), _aw, _bw in jobs:
            cr, ci = mm.matmul(ar, ai, self.a, br, bi, self.b, self.c,
                               adjoint=int(d.op) == MatmulOp.MUL_AH, form=int(self.form))
            out.append((int(d.c_base), block_major(cr, ci, self.c, Mm, Nm, lane_bits=lb,
                                                    word_bits=w, order="row")))
        return out


def read_c(words: np.ndarray, fmt: Format, M: int, N: int, Mmax: int, Nmax: int, *,
           lane_bits: int, word_bits: int):
    """``C`` (stored integers) from its block-major tiles in *words*."""
    per = nwords(Mmax * Nmax, lane_bits, word_bits)
    cr, ci = np.zeros((M, N), np.int64), np.zeros((M, N), np.int64)
    k = 0
    for i in range(M // Mmax):
        for j in range(N // Nmax):
            r, im = from_words(words[k * per:(k + 1) * per], Mmax * Nmax, fmt, lane_bits, word_bits)
            cr[i * Mmax:(i + 1) * Mmax, j * Nmax:(j + 1) * Nmax] = r.reshape(Mmax, Nmax)
            ci[i * Mmax:(i + 1) * Mmax, j * Nmax:(j + 1) * Nmax] = im.reshape(Mmax, Nmax)
            k += 1
    return cr, ci
