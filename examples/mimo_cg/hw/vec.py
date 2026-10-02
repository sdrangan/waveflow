"""vec.py — the CG vector unit ``cg_vec`` and its per-block composite ``CgVecUnit``.

Step 4.2 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 4.0 decision record, §14).

The block
---------
:class:`CgVec` is register steps 2–9 of the CG (:func:`~examples.mimo_cg.mimo_cg_fixed.vec_step`)
plus the start (:func:`~examples.mimo_cg.mimo_cg_fixed.cg_init`).  It holds X, R, P and rz for
one job and takes its orders from a command queue (:class:`~examples.mimo_cg.hw.common.CgIterCmd`):

* ``INIT`` — read ``B`` from ``b_blk``, start the state, and emit ``P₀`` on ``p_blk``;
* ``ITER`` — read ``Sₙ`` from ``s_blk``, run one iteration, and emit ``Pₙ`` on ``p_blk``;
* ``LAST`` — the same iteration, then emit ``X`` on ``x_blk`` instead of ``P``.

The same leaf, with the same ports, sits in the integrated detector (step 4.8), where
``cg_ctrl`` fills the queue and ``cg_mm`` turns each ``P`` into the next ``S``.

The per-block composite
-----------------------
:class:`CgVecUnit` runs the block from memory, which is what its RTL gate (step 4.4) and Phase 5's
per-block calibration need: ``cg_vec_rx`` frames ``1 + nit`` reads (``B``, then ``S₁ … S_nit``,
computed beforehand by the golden matmul), ``cg_vec_load`` lands them in blocks and plays
``cg_ctrl``'s part, and ``cg_vec_store`` writes ``P₀ … P_{nit−1}`` and ``X`` back, one write per
block, the last one echoing the job's descriptor on ``s_done``::

    s_cmd → cg_vec_rx → MemRStream → cg_vec_load ─b_blk/s_blk/cmds→ cg_vec ─p_blk/x_blk→ cg_vec_store
                                                                               → MemWStream → s_done

Blocks
------
A stream-of-blocks block holds one K×N register matrix as ``K·N/L`` **lane groups**: row ``k``,
columns ``c·L … c·L+L−1``, lane ``l`` at bits ``[2W·l, 2W·l+2W)`` with ``re`` in the low ``W`` bits
(the ``ComplexField`` bit order).  One block read feeds all ``L`` lanes.  In the Python simulation a
block carries the stored register integers on ``block.payload`` as ``(re, im)``, shape ``(K, N)``.

Timing here is a placeholder (Phase 5 calibrates it); the Python bodies call the golden, so the
Python simulation checks the plumbing (packing, ordering, commands, layout) end to end.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from examples.mimo_cg.hw.common import (
    DEFAULT_MEM_DW,
    DEFAULT_N,
    CgDesc,
    CgIterCmd,
    IterOp,
    Word32,
    from_words,
    hw_format,
    nwords,
    to_words,
)
from examples.mimo_cg.mimo_cg_fixed import cg_init, vec_step
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.dataschema import DataArray, DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import (
    SobIFMaster,
    SobIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
    StreamOfBlocksIF,
)
from waveflow.hw.mem_stream import KernelTask, MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemoryMod, MemSeg
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle

DEFAULT_K = 4
DEFAULT_L = 4
#: The default format id, ``HW_FORMAT_NAMES[0]`` = W12g8.
DEFAULT_FMT = 0
#: Placeholder pipeline model (cycles), to be replaced by Phase 5's fit: a divider's latency and
#: the fixed overhead of one firing.
_DIV_LATENCY = 40
_OVERHEAD = 10


def block_type(W: int, K: int, N: int, L: int) -> type[DataArray]:
    """One K×N register matrix as ``K·N/L`` lane groups of ``L`` complex ``W``-bit values."""
    if N % L:
        raise ValueError(f"N = {N} is not a multiple of the lane count L = {L}")
    group = IntField.specialize(bitwidth=2 * W * L, signed=False)
    return DataArray.specialize(
        element_type=group, max_shape=(K * N // L,), member_name="groups"
    )


def _put(block, re: np.ndarray, im: np.ndarray):
    block.payload = (np.array(re, np.int64), np.array(im, np.int64))
    return block


def vec_cycles(op: IterOp, K: int, N: int, L: int) -> float:
    """Placeholder cycles of one command: a lane-parallel pass over the K·N block for ``INIT``;
    three passes and two divisions per column group for an iteration."""
    if op == IterOp.INIT:
        return _OVERHEAD + K * N / L
    return _OVERHEAD + (N / L) * (3 * K + 2 * _DIV_LATENCY)


# --- the block -------------------------------------------------------------------------------


@dataclass
class CgVec(FreeRunMod):
    """The vector unit: one job per firing, driven by its command queue (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_vec"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    L: HwParam[int] = DEFAULT_L
    fmt: HwParam[int] = DEFAULT_FMT
    #: Blocks per stream-of-blocks edge (part of the C++ type, so a template argument).
    sob_depth: HwParam[int] = 2
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, K, N, L = int(self.mem_dwidth), int(self.K), int(self.N), int(self.L)
        f = self.formats = hw_format(int(self.fmt))
        self.cmd_in = StreamIFSlave(
            name=f"{self.name}_cmd_in", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.b_blk = SobIFSlave(
            name=f"{self.name}_b_blk",
            sim=self.sim,
            element_type=block_type(f.B.W, K, N, L),
        )
        self.s_blk = SobIFSlave(
            name=f"{self.name}_s_blk",
            sim=self.sim,
            element_type=block_type(f.S.W, K, N, L),
        )
        self.p_blk = SobIFMaster(
            name=f"{self.name}_p_blk",
            sim=self.sim,
            element_type=block_type(f.P.W, K, N, L),
        )
        self.x_blk = SobIFMaster(
            name=f"{self.name}_x_blk",
            sim=self.sim,
            element_type=block_type(f.X.W, K, N, L),
        )
        for ep in (self.cmd_in, self.b_blk, self.s_blk, self.p_blk, self.x_blk):
            self.add_endpoint(ep)
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_vec_task",
            "cg_vec_task.h",
            ("cmd_in", "b_blk", "s_blk", "p_blk", "x_blk"),
            template_args=(
                int(self.mem_dwidth),
                int(self.K),
                int(self.N),
                int(self.L),
                int(self.sob_depth),
            ),
        )

    def _wait(self, op: IterOp):
        cyc = vec_cycles(op, int(self.K), int(self.N), int(self.L))
        return self.timeout(cyc * self.clk.period)

    def run_iter(self) -> ProcessGen[None]:
        f = self.formats
        cmd = yield from self.cmd_in.get_schema(CgIterCmd)
        t0 = self.now
        if IterOp(int(cmd.op)) != IterOp.INIT:
            raise RuntimeError(f"{self.name}: a job must start with INIT, got {cmd.op}")
        bb = yield from self.b_blk.acquire_read()
        state = cg_init(*bb.payload, f)
        yield from self.b_blk.release_read()
        yield self._wait(IterOp.INIT)
        pb = yield from self.p_blk.acquire_write()
        yield from self.p_blk.commit_write(_put(pb, state.pr, state.pi))
        while True:
            cmd = yield from self.cmd_in.get_schema(CgIterCmd)
            op = IterOp(int(cmd.op))
            sb = yield from self.s_blk.acquire_read()
            state, _ = vec_step(state, *sb.payload, f)
            yield from self.s_blk.release_read()
            yield self._wait(op)
            if op == IterOp.LAST:
                xb = yield from self.x_blk.acquire_write()
                yield from self.x_blk.commit_write(_put(xb, state.xr, state.xi))
                break
            pb = yield from self.p_blk.acquire_write()
            yield from self.p_blk.commit_write(_put(pb, state.pr, state.pi))
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


# --- the per-block composite -----------------------------------------------------------------


class CgVecUnitCmd(DataList):
    """One ``CgVecUnit`` job (host → ``s_cmd``): ``B`` at ``b_off``; ``S₁ … S_nit`` back to back
    at ``s_off``; ``P₀ … P_{nit−1}`` and then ``X`` written back to back at ``out_off``.
    """

    include_filename: ClassVar[str | None] = "cg_vec_unit_cmd.h"
    elements: ClassVar[dict] = {
        "b_off": {"schema": Word32, "description": "B word offset"},
        "s_off": {"schema": Word32, "description": "S_1..S_nit word offset"},
        "out_off": {"schema": Word32, "description": "P_0..P_{nit-1}, X word offset"},
        "nit": {"schema": Word32, "description": "CG iterations (1..K)"},
    }


@dataclass
class CgVecRx(FreeRunMod):
    """Framer: one ``CgVecUnitCmd`` → ``1 + nit`` reads; the first relays the job's ``CgDesc``."""

    cpp_kernel_name: ClassVar[str | None] = "cg_vec_rx"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.s_cmd = StreamIFSlave(
            name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=w, has_tlast=False
        )
        self.cmd_out = StreamIFMaster(
            name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w, has_tlast=True
        )
        for ep in (self.s_cmd, self.cmd_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_vec_rx_task",
            "cg_vec_rx_task.h",
            ("s_cmd", "cmd_out"),
            template_args=(int(self.mem_dwidth), int(self.K), int(self.N)),
        )

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        nw = nwords(int(self.K) * int(self.N), w)
        cmd = yield from self.s_cmd.get_schema(CgVecUnitCmd)
        nit = min(max(int(cmd.nit), 1), int(self.K))  # clamped as in the C++ framer
        frames = [
            MemRCmd(addr=int(cmd.b_off), len=nw, fwd_bursts=1),
            CgDesc(nit=nit, x_off=int(cmd.out_off)),
        ]
        frames += [
            MemRCmd(addr=int(cmd.s_off) + n * nw, len=nw, fwd_bursts=0)
            for n in range(nit)
        ]
        for fr in frames:
            yield from self.cmd_out.write(
                np.asarray(fr.serialize(word_bw=w), np.uint64)
            )


@dataclass
class CgVecLoad(FreeRunMod):
    """Lands ``B`` and each ``Sₙ`` in blocks, forwards the descriptor to the store, and issues the
    vector unit's commands (``INIT``, then ``ITER`` … ``LAST``) — ``cg_ctrl``'s part here.
    """

    cpp_kernel_name: ClassVar[str | None] = "cg_vec_load"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    L: HwParam[int] = DEFAULT_L
    fmt: HwParam[int] = DEFAULT_FMT
    #: Blocks per stream-of-blocks edge (part of the C++ type, so a template argument).
    sob_depth: HwParam[int] = 2
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, K, N, L = int(self.mem_dwidth), int(self.K), int(self.N), int(self.L)
        f = self.formats = hw_format(int(self.fmt))
        self.s_in = StreamIFSlave(
            name=f"{self.name}_s_in", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.desc_out = StreamIFMaster(
            name=f"{self.name}_desc_out", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.cmd_out = StreamIFMaster(
            name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.b_blk = SobIFMaster(
            name=f"{self.name}_b_blk",
            sim=self.sim,
            element_type=block_type(f.B.W, K, N, L),
        )
        self.s_blk = SobIFMaster(
            name=f"{self.name}_s_blk",
            sim=self.sim,
            element_type=block_type(f.S.W, K, N, L),
        )
        for ep in (self.s_in, self.desc_out, self.cmd_out, self.b_blk, self.s_blk):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_vec_load_task",
            "cg_vec_load_task.h",
            ("s_in", "desc_out", "cmd_out", "b_blk", "s_blk"),
            template_args=(
                int(self.mem_dwidth),
                int(self.K),
                int(self.N),
                int(self.L),
                int(self.sob_depth),
            ),
        )

    def _read_matrix(self, fmt):
        w, K, N = int(self.mem_dwidth), int(self.K), int(self.N)
        words = yield from self.s_in.get(nwords_max=nwords(K * N, w))
        re, im = from_words(words, K * N, fmt, w)
        return re.reshape(K, N), im.reshape(K, N)

    def _command(self, op: IterOp, it: int):
        w = int(self.mem_dwidth)
        words = CgIterCmd(op=int(op), it=it).serialize(word_bw=w)
        yield from self.cmd_out.write(np.asarray(words, np.uint64))

    def run_iter(self) -> ProcessGen[None]:
        f, w = self.formats, int(self.mem_dwidth)
        desc = yield from self.s_in.get_schema(CgDesc)
        nit = int(desc.nit)
        yield from self.desc_out.write(np.asarray(desc.serialize(word_bw=w), np.uint64))
        b = yield from self._read_matrix(f.B)
        bb = yield from self.b_blk.acquire_write()
        yield from self.b_blk.commit_write(_put(bb, *b))
        yield from self._command(IterOp.INIT, 0)
        for n in range(1, nit + 1):
            s = yield from self._read_matrix(f.S)
            sb = yield from self.s_blk.acquire_write()
            yield from self.s_blk.commit_write(_put(sb, *s))
            yield from self._command(IterOp.LAST if n == nit else IterOp.ITER, n)


@dataclass
class CgVecStore(FreeRunMod):
    """Writes ``P₀ … P_{nit−1}`` (no echo) and then ``X`` (echoing the descriptor), one block each.

    That is ``nit + 1`` writes, matching the ``nit + 1`` reads (``B``, ``S₁ … S_nit``) — which is
    load-bearing: HLS couples the reader's and writer's firing counts through the ``m_axi`` pointer
    FIFOs, and a job with fewer writes than reads deadlocks the RTL after a few jobs (plan §15, 4.7).
    """

    cpp_kernel_name: ClassVar[str | None] = "cg_vec_store"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    L: HwParam[int] = DEFAULT_L
    fmt: HwParam[int] = DEFAULT_FMT
    #: Blocks per stream-of-blocks edge (part of the C++ type, so a template argument).
    sob_depth: HwParam[int] = 2
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, K, N, L = int(self.mem_dwidth), int(self.K), int(self.N), int(self.L)
        f = self.formats = hw_format(int(self.fmt))
        self.desc_in = StreamIFSlave(
            name=f"{self.name}_desc_in", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.p_blk = SobIFSlave(
            name=f"{self.name}_p_blk",
            sim=self.sim,
            element_type=block_type(f.P.W, K, N, L),
        )
        self.x_blk = SobIFSlave(
            name=f"{self.name}_x_blk",
            sim=self.sim,
            element_type=block_type(f.X.W, K, N, L),
        )
        self.cmd_out = StreamIFMaster(
            name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w, has_tlast=True
        )
        for ep in (self.desc_in, self.p_blk, self.x_blk, self.cmd_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_vec_store_task",
            "cg_vec_store_task.h",
            ("desc_in", "p_blk", "x_blk", "cmd_out"),
            template_args=(
                int(self.mem_dwidth),
                int(self.K),
                int(self.N),
                int(self.L),
                int(self.sob_depth),
            ),
        )

    def _write(self, addr: int, re, im, fmt, echo=None):
        w = int(self.mem_dwidth)
        words = to_words(re, im, fmt, w)
        cmd = MemWCmd(addr=addr, len=len(words), fwd_bursts=0 if echo is None else 1)
        yield from self.cmd_out.write(np.asarray(cmd.serialize(word_bw=w), np.uint64))
        if echo is not None:
            yield from self.cmd_out.write(
                np.asarray(echo.serialize(word_bw=w), np.uint64)
            )
        yield from self.cmd_out.write(words)

    def run_iter(self) -> ProcessGen[None]:
        f, w = self.formats, int(self.mem_dwidth)
        nw = nwords(int(self.K) * int(self.N), w)
        desc = yield from self.desc_in.get_schema(CgDesc)
        nit, out = int(desc.nit), int(desc.x_off)
        for n in range(nit):
            pb = yield from self.p_blk.acquire_read()
            yield from self._write(out + n * nw, *pb.payload, f.P)
            yield from self.p_blk.release_read()
        xb = yield from self.x_blk.acquire_read()
        yield from self._write(out + nit * nw, *xb.payload, f.X, echo=desc)
        yield from self.x_blk.release_read()


@dataclass
class CgVecUnit(FreeRunMod):
    """The vector unit run from memory (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_vec_unit"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    L: HwParam[int] = DEFAULT_L
    fmt: HwParam[int] = DEFAULT_FMT
    #: The queue and shared-memory knobs: command FIFO depth and stream-of-blocks block count.
    cmd_depth: HwParam[int] = 2
    sob_depth: HwParam[int] = 2
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        kw = {"mem_dwidth": w, "K": int(self.K), "N": int(self.N), "clk": self.clk}
        kwf = {
            **kw,
            "L": int(self.L),
            "fmt": int(self.fmt),
            "sob_depth": int(self.sob_depth),
        }
        self.rx = CgVecRx(name=f"{self.name}_rx", sim=self.sim, **kw)
        self.rstream = MemRStream(
            name=f"{self.name}_memr",
            sim=self.sim,
            mem_dwidth=w,
            inband=True,
            clk=self.clk,
        )
        self.load = CgVecLoad(name=f"{self.name}_load", sim=self.sim, **kwf)
        self.vec = CgVec(name=f"{self.name}_vec", sim=self.sim, **kwf)
        self.store = CgVecStore(name=f"{self.name}_store", sim=self.sim, **kwf)
        self.wstream = MemWStream(
            name=f"{self.name}_memw",
            sim=self.sim,
            mem_dwidth=w,
            inband=True,
            emit_done=True,
            clk=self.clk,
        )
        for c in (self.rx, self.rstream, self.load, self.vec, self.store, self.wstream):
            self.add_comp(c)

        def _sif(name, master, slave, depth=None):
            iface = StreamIF(
                name=f"{self.name}_{name}_if",
                sim=self.sim,
                clk=self.clk,
                bitwidth=w,
                framed=True,
                **({} if depth is None else {"depth": depth}),
            )
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        def _sobif(name, master, slave):
            iface = StreamOfBlocksIF(
                name=f"{self.name}_{name}_if",
                sim=self.sim,
                clk=self.clk,
                element_type=master.element_type,
                depth=int(self.sob_depth),
            )
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        _sif("cmd_rd", self.rx.cmd_out, self.rstream.s_cmd)
        _sif("rdata", self.rstream.m_out, self.load.s_in)
        _sif("desc", self.load.desc_out, self.store.desc_in)
        _sif("iter", self.load.cmd_out, self.vec.cmd_in, depth=int(self.cmd_depth))
        _sif("wdata", self.store.cmd_out, self.wstream.s_in)
        _sobif("b_blk", self.load.b_blk, self.vec.b_blk)
        _sobif("s_blk", self.load.s_blk, self.vec.s_blk)
        _sobif("p_blk", self.vec.p_blk, self.store.p_blk)
        _sobif("x_blk", self.vec.x_blk, self.store.x_blk)

        self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
        self.s_cmd = self.rx.s_cmd
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done


# --- the testbench graph and the procedure ---------------------------------------------------


@dataclass
class CgVecUnitTB(FreeRunMod):
    """The testbench as a graph: a driver on ``s_cmd``, a sink on ``s_done``, one arena behind
    both ``m_axi`` bundles, and the :class:`CgVecUnit`.  ``jobs`` gives each job's ``nit``.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    jobs: tuple = (4,)
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    L: HwParam[int] = DEFAULT_L
    fmt: HwParam[int] = DEFAULT_FMT
    cmd_depth: HwParam[int] = 2
    sob_depth: HwParam[int] = 2
    n_cycles: int = 200_000
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        bad = [n for n in self.jobs if not 1 <= int(n) <= int(self.K)]
        if bad:
            raise ValueError(f"nit must lie in 1..K = {int(self.K)}; got {bad}")
        w, K, N = int(self.mem_dwidth), int(self.K), int(self.N)
        nw = nwords(K * N, w)
        cur, self.layout = 0, []
        for nit in self.jobs:  # (nit, b_off, s_off, out_off)
            self.layout.append((int(nit), cur, cur + nw, cur + nw + int(nit) * nw))
            cur += nw + int(nit) * nw + (int(nit) + 1) * nw
        self.arena_words = cur + 16
        self.mem = MemoryMod(
            name=f"{self.name}_mem",
            sim=self.sim,
            inline=False,
            clk=self.clk,
            word_size=w,
            addr_size=32,
            nwords_tot=self.arena_words * 4,
        )
        self.mem.alloc(int(self.mem.nwords_tot))
        self.mem.load_segs = [MemSeg(0, 0, "vectors/mem_in")]
        self.mem.dump_segs = [MemSeg(0, int(self.mem.nwords_tot), "vectors/out")]
        self.dut = CgVecUnit(
            name=f"{self.name}_dut",
            sim=self.sim,
            mem_dwidth=w,
            K=K,
            N=N,
            L=int(self.L),
            fmt=int(self.fmt),
            cmd_depth=int(self.cmd_depth),
            sob_depth=int(self.sob_depth),
            clk=self.clk,
        )
        self.cmds = [
            CgVecUnitCmd(b_off=b, s_off=s, out_off=o, nit=nit)
            for (nit, b, s, o) in self.layout
        ]
        self.cmd_words = [
            np.asarray(c.serialize(word_bw=w), np.uint64) for c in self.cmds
        ]
        self.driver = StreamDriver(sim=self.sim, bitwidth=w, in_bundle="vectors/s_cmd")
        self.done_sink = StreamSink(
            sim=self.sim, bitwidth=w, out_bundle="vectors/s_done", has_tlast=True
        )
        for c in (self.dut, self.driver, self.done_sink, self.mem):
            self.add_comp(c)
        cmd_if = StreamIF(
            name=f"{self.name}_cmd_if", sim=self.sim, clk=self.clk, bitwidth=w
        )
        cmd_if.bind(ep_name="master", endpoint=self.driver.stream_ep)
        cmd_if.bind(ep_name="slave", endpoint=self.dut.s_cmd)
        self.add_if(cmd_if)
        done_if = StreamIF(
            name=f"{self.name}_done_if", sim=self.sim, clk=self.clk, bitwidth=w
        )
        done_if.bind(ep_name="master", endpoint=self.dut.s_done)
        done_if.bind(ep_name="slave", endpoint=self.done_sink.stream_ep)
        self.add_if(done_if)
        xbar = AXIMMCrossBarIF(
            name=f"{self.name}_xbar",
            sim=self.sim,
            clk=self.clk,
            nports_master=2,
            nports_slave=1,
            bitwidth=w,
        )
        xbar.bind("master_0", self.dut.m_in)
        xbar.bind("master_1", self.dut.m_out)
        xbar.bind("slave_0", self.mem.s_mm)
        self.add_if(xbar)
        assign_address_ranges([self.mem.s_mm], [(0, self.arena_words * (w // 8))])


def vec_unit_golden(A, B, scale, nit: int, formats):
    """Per job: the stored ``B`` and ``S₁ … S_nit`` the unit reads, and the ``P₀ … P_{nit−1}``
    and ``X`` it must write, from the golden sub-steps."""
    from examples.mimo_cg.mimo_cg_fixed import mm_step, quantize_inputs

    ar, ai, br, bi = quantize_inputs(A, B, formats, scale)
    state = cg_init(br, bi, formats)
    s_seq, p_seq = [], [(state.pr, state.pi)]
    for _ in range(nit):
        sr, si = mm_step(ar, ai, state.pr, state.pi, formats)
        s_seq.append((sr, si))
        state, _ = vec_step(state, sr, si, formats)
        p_seq.append((state.pr, state.pi))
    return (br, bi), s_seq, p_seq[:nit], (state.xr, state.xi)


class CgVecUnitSim:
    """The procedure around a :class:`CgVecUnitTB`: write the scenario, run, and check every
    ``P`` and ``X`` word against the golden.  ``problems`` are ``(A, B, scale)`` per job.
    """

    def __init__(self, problems, jobs, **tb_kw) -> None:
        self.problems = list(problems)
        self.tb = CgVecUnitTB(name="tb", sim=Simulation(), jobs=tuple(jobs), **tb_kw)
        self.expected: list[tuple[int, np.ndarray]] = []

    def scenario(self) -> dict:
        """The scenario as arrays — the command words, the memory image the unit starts from, and
        the golden image — shared by the Python simulation, C-sim and the XSI run."""
        tb = self.tb
        w = int(tb.mem_dwidth)
        f = hw_format(int(tb.fmt))
        mem_in = np.zeros(int(tb.mem.nwords_tot), np.uint64)
        golden = np.zeros_like(mem_in)
        self.expected = []
        for (nit, b_off, s_off, out_off), (A, B, scale) in zip(
            tb.layout, self.problems, strict=True
        ):
            b, s_seq, p_seq, x = vec_unit_golden(A, B, scale, nit, f)
            words = to_words(*b, f.B, w)
            mem_in[b_off : b_off + len(words)] = words
            s_words = np.concatenate([to_words(*s, f.S, w) for s in s_seq])
            mem_in[s_off : s_off + len(s_words)] = s_words
            exp = np.concatenate(
                [to_words(*p, f.P, w) for p in p_seq] + [to_words(*x, f.X, w)]
            )
            golden[out_off : out_off + len(exp)] = exp
            self.expected.append((out_off, exp))
        return {
            "cmd_words": tb.cmd_words,
            "mem_in": mem_in,
            "golden": golden,
            "done_words": len(tb.layout) * CgDesc.nwords_per_inst(w),
            "expected": list(self.expected),
        }

    def write_scenario(self, root) -> None:
        tb = self.tb
        root = Path(root)
        sc = self.scenario()
        write_burst_bundle(sc["cmd_words"], root / "vectors" / "s_cmd")
        write_burst_bundle([sc["mem_in"]], root / "vectors" / "mem_in")
        write_burst_bundle([sc["golden"]], root / "vectors" / "golden")
        tb.driver.root = root
        tb.mem.root = root

    def run(self) -> CgVecUnitTB:
        with tempfile.TemporaryDirectory() as root:
            self.write_scenario(root)
            self.tb.sim.run_sim()
        return self.check()

    def check(self) -> CgVecUnitTB:
        tb = self.tb
        bpw = int(tb.mem_dwidth) // 8
        for j, (off, exp) in enumerate(self.expected):
            got = tb.mem._mem.read(off * bpw, len(exp)).astype(np.uint64)
            if not np.array_equal(got, exp):
                bad = int(np.argmax(got != exp))
                raise AssertionError(
                    f"cg_vec_unit job {j} word {bad}: 0x{int(got[bad]):016x} != "
                    f"golden 0x{int(exp[bad]):016x}"
                )
        assert len(tb.done_sink.words) == len(
            tb.layout
        ), f"expected one done per job ({len(tb.layout)}), got {len(tb.done_sink.words)}"
        return tb
