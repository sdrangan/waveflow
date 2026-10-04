"""mm.py — the CG matrix-multiply block ``cg_mm`` and its per-block composite ``CgMmUnit``.

Step 4.5 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 4.0 decision record, §14).

The block
---------
:class:`CgMm` is register step 1 of the CG, ``S = q_S(A·P)``
(:func:`~examples.mimo_cg.mimo_cg_fixed.mm_step`), as an output-stationary **systolic array** of
``R × C`` complex processing elements: ``A`` values shift right along the rows and ``P`` values
shift down the columns, fed with the usual skew, and each element accumulates one entry of ``S``
exactly over ``k`` before the single quantize ``q_S``.  ``S`` (K × N) is covered in
``(K/R)·(N/C)`` tiles.  ``cmul`` picks the complex product: 4 real multiplies, or 3 (the Gauss
form ``k₁ = p_r(a_r + a_i)``, ``k₂ = a_r(p_i − p_r)``, ``k₃ = a_i(p_r + p_i)``, ``re = k₁ − k₃``,
``im = k₁ + k₂``), which is bit-exact too because every sum before ``q_S`` is exact.

Its command queue (:class:`~examples.mimo_cg.hw.common.CgIterCmd`) mirrors the vector unit's:
``INIT`` loads ``A`` from ``a_blk`` for the job; ``ITER`` and ``LAST`` each turn one ``P`` from
``p_blk`` into one ``S`` on ``s_blk`` (``LAST`` ends the job).  ``P`` and ``S`` use the vector
unit's lane-group blocks, so in the integrated detector (step 4.8) the two blocks connect directly;
``C`` must be a multiple of the lane count ``L``.  ``A`` is a block of ``K`` rows, one row per
element.

The per-block composite
-----------------------
:class:`CgMmUnit` runs the block from memory: ``cg_mm_rx`` frames ``1 + nit`` reads (``A``, then
``P₀ … P_{nit−1}``), ``cg_mm_load`` lands them and issues the commands, and ``cg_mm_store`` writes
``S₁ … S_nit``, the last write echoing the job's descriptor on ``s_done``.

Timing here is a placeholder (Phase 5 calibrates it); the Python body calls the golden.
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
from examples.mimo_cg.hw.vec import DEFAULT_FMT, DEFAULT_K, DEFAULT_L, _put, block_type
from examples.mimo_cg.mimo_cg_fixed import mm_step
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

#: Default array: R = K rows (set per instance), C = 4 columns; 4 real multiplies per product.
DEFAULT_C = 4
DEFAULT_CMUL = 4


def a_block_type(W: int, K: int) -> type[DataArray]:
    """``A`` (K × K) as ``K`` row elements of ``K`` complex ``W``-bit values (lane order)."""
    row = IntField.specialize(bitwidth=2 * W * K, signed=False)
    return DataArray.specialize(element_type=row, max_shape=(K,), member_name="rows")


def mm_cycles(
    K: int,
    N: int,
    R: int,
    C: int,
    L: int | None = None,
    cmul: int | None = None,
    fmt: int | None = None,
) -> float:
    """Cycles of one product, from "P there and block free" to the hand-over of S.

    With the lanes, the multiply form and a format of the Phase 5 space, the calibrated span of the
    block (:func:`examples.mimo_cg.hw.models.block_span`).  Otherwise a rough fallback: per tile,
    the skewed systolic sweep ``K + R + C − 2``.
    """
    if None not in (L, cmul, fmt):
        from examples.mimo_cg.hw.models import block_span

        span = block_span("mm.iter", fmt, K=K, R=R, C=C, L=L, cmul=cmul)
        if span is not None:
            return span
    return (K // R) * (N // C) * (K + R + C - 2) + 10


# --- the block -------------------------------------------------------------------------------


@dataclass
class CgMm(FreeRunMod):
    """The systolic matrix multiply: one job per firing, driven by its command queue."""

    cpp_kernel_name: ClassVar[str | None] = "cg_mm"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    L: HwParam[int] = DEFAULT_L
    #: Array rows; ``0`` means ``K`` (one tile covers a whole column of ``S``).
    R: HwParam[int] = 0
    C: HwParam[int] = DEFAULT_C
    cmul: HwParam[int] = DEFAULT_CMUL
    fmt: HwParam[int] = DEFAULT_FMT
    sob_depth: HwParam[int] = 2
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, K, N, L = int(self.mem_dwidth), int(self.K), int(self.N), int(self.L)
        self.rows = int(self.R) or K
        if K % self.rows or N % int(self.C) or int(self.C) % L:
            raise ValueError(
                f"need R | K, C | N and L | C (K={K}, N={N}, R={self.rows}, C={self.C}, L={L})"
            )
        if int(self.cmul) not in (3, 4):
            raise ValueError(f"cmul must be 3 or 4, got {self.cmul}")
        f = self.formats = hw_format(int(self.fmt))
        self.cmd_in = StreamIFSlave(
            name=f"{self.name}_cmd_in", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.a_blk = SobIFSlave(
            name=f"{self.name}_a_blk", sim=self.sim, element_type=a_block_type(f.A.W, K)
        )
        self.p_blk = SobIFSlave(
            name=f"{self.name}_p_blk",
            sim=self.sim,
            element_type=block_type(f.P.W, K, N, L),
        )
        self.s_blk = SobIFMaster(
            name=f"{self.name}_s_blk",
            sim=self.sim,
            element_type=block_type(f.S.W, K, N, L),
        )
        for ep in (self.cmd_in, self.a_blk, self.p_blk, self.s_blk):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_mm_task",
            "cg_mm_task.h",
            ("cmd_in", "a_blk", "p_blk", "s_blk"),
            template_args=(
                int(self.mem_dwidth),
                int(self.K),
                int(self.N),
                int(self.L),
                self.rows,
                int(self.C),
                int(self.cmul),
                int(self.sob_depth),
            ),
        )

    def run_iter(self) -> ProcessGen[None]:
        f = self.formats
        K, N, C = int(self.K), int(self.N), int(self.C)
        cmd = yield from self.cmd_in.get_schema(CgIterCmd)
        if IterOp(int(cmd.op)) != IterOp.INIT:
            raise RuntimeError(f"{self.name}: a job must start with INIT, got {cmd.op}")
        ab = yield from self.a_blk.acquire_read()
        ar, ai = ab.payload
        yield from self.a_blk.release_read()
        while True:
            cmd = yield from self.cmd_in.get_schema(CgIterCmd)
            pb = yield from self.p_blk.acquire_read()
            sr, si = mm_step(ar, ai, *pb.payload, f)
            yield from self.p_blk.release_read()
            cyc = mm_cycles(
                K, N, self.rows, C, int(self.L), int(self.cmul), int(self.fmt)
            )
            yield self.timeout(cyc * self.clk.period)
            sb = yield from self.s_blk.acquire_write()
            yield from self.s_blk.commit_write(_put(sb, sr, si))
            if IterOp(int(cmd.op)) == IterOp.LAST:
                break


# --- the per-block composite -----------------------------------------------------------------


class CgMmUnitCmd(DataList):
    """One ``CgMmUnit`` job (host → ``s_cmd``): ``A`` at ``a_off``; ``P₀ … P_{nit−1}`` back to
    back at ``p_off``; ``S₁ … S_nit`` written back to back at ``out_off``."""

    include_filename: ClassVar[str | None] = "cg_mm_unit_cmd.h"
    elements: ClassVar[dict] = {
        "a_off": {"schema": Word32, "description": "A word offset"},
        "p_off": {"schema": Word32, "description": "P_0..P_{nit-1} word offset"},
        "out_off": {"schema": Word32, "description": "S_1..S_nit word offset"},
        "nit": {"schema": Word32, "description": "CG iterations (1..K)"},
    }


@dataclass
class CgMmRx(FreeRunMod):
    """Framer: one ``CgMmUnitCmd`` → ``1 + nit`` reads; the first relays the job's ``CgDesc``."""

    cpp_kernel_name: ClassVar[str | None] = "cg_mm_rx"
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
            "cg_mm_rx_task",
            "cg_mm_rx_task.h",
            ("s_cmd", "cmd_out"),
            template_args=(int(self.mem_dwidth), int(self.K), int(self.N)),
        )

    def run_iter(self) -> ProcessGen[None]:
        w, K, N = int(self.mem_dwidth), int(self.K), int(self.N)
        nwa, nwp = nwords(K * K, w), nwords(K * N, w)
        cmd = yield from self.s_cmd.get_schema(CgMmUnitCmd)
        nit = min(max(int(cmd.nit), 1), int(self.K))  # clamped as in the C++ framer
        frames = [
            MemRCmd(addr=int(cmd.a_off), len=nwa, fwd_bursts=1),
            CgDesc(nit=nit, x_off=int(cmd.out_off)),
        ]
        frames += [
            MemRCmd(addr=int(cmd.p_off) + n * nwp, len=nwp, fwd_bursts=0)
            for n in range(nit)
        ]
        for fr in frames:
            yield from self.cmd_out.write(
                np.asarray(fr.serialize(word_bw=w), np.uint64)
            )


@dataclass
class CgMmLoad(FreeRunMod):
    """Lands ``A`` and each ``Pₙ`` in blocks, forwards the descriptor, issues the commands."""

    cpp_kernel_name: ClassVar[str | None] = "cg_mm_load"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    L: HwParam[int] = DEFAULT_L
    fmt: HwParam[int] = DEFAULT_FMT
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
        self.a_blk = SobIFMaster(
            name=f"{self.name}_a_blk", sim=self.sim, element_type=a_block_type(f.A.W, K)
        )
        self.p_blk = SobIFMaster(
            name=f"{self.name}_p_blk",
            sim=self.sim,
            element_type=block_type(f.P.W, K, N, L),
        )
        for ep in (self.s_in, self.desc_out, self.cmd_out, self.a_blk, self.p_blk):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_mm_load_task",
            "cg_mm_load_task.h",
            ("s_in", "desc_out", "cmd_out", "a_blk", "p_blk"),
            template_args=(
                int(self.mem_dwidth),
                int(self.K),
                int(self.N),
                int(self.L),
                int(self.sob_depth),
            ),
        )

    def _read_matrix(self, fmt, rows: int, cols: int):
        w = int(self.mem_dwidth)
        words = yield from self.s_in.get(nwords_max=nwords(rows * cols, w))
        re, im = from_words(words, rows * cols, fmt, w)
        return re.reshape(rows, cols), im.reshape(rows, cols)

    def _command(self, op: IterOp, it: int):
        w = int(self.mem_dwidth)
        words = CgIterCmd(op=int(op), it=it).serialize(word_bw=w)
        yield from self.cmd_out.write(np.asarray(words, np.uint64))

    def run_iter(self) -> ProcessGen[None]:
        f, w, K, N = self.formats, int(self.mem_dwidth), int(self.K), int(self.N)
        desc = yield from self.s_in.get_schema(CgDesc)
        nit = int(desc.nit)
        yield from self.desc_out.write(np.asarray(desc.serialize(word_bw=w), np.uint64))
        a = yield from self._read_matrix(f.A, K, K)
        ab = yield from self.a_blk.acquire_write()
        yield from self.a_blk.commit_write(_put(ab, *a))
        yield from self._command(IterOp.INIT, 0)
        for n in range(1, nit + 1):
            p = yield from self._read_matrix(f.P, K, N)
            pb = yield from self.p_blk.acquire_write()
            yield from self.p_blk.commit_write(_put(pb, *p))
            yield from self._command(IterOp.LAST if n == nit else IterOp.ITER, n)


@dataclass
class CgMmStore(FreeRunMod):
    """Writes ``S₁ … S_nit``, one block per write, then a zero-length write that echoes the
    descriptor, so each job has as many writes as reads (see ``cg_mm_store_task.h``)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_mm_store"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    L: HwParam[int] = DEFAULT_L
    fmt: HwParam[int] = DEFAULT_FMT
    sob_depth: HwParam[int] = 2
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, K, N, L = int(self.mem_dwidth), int(self.K), int(self.N), int(self.L)
        f = self.formats = hw_format(int(self.fmt))
        self.desc_in = StreamIFSlave(
            name=f"{self.name}_desc_in", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.s_blk = SobIFSlave(
            name=f"{self.name}_s_blk",
            sim=self.sim,
            element_type=block_type(f.S.W, K, N, L),
        )
        self.cmd_out = StreamIFMaster(
            name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w, has_tlast=True
        )
        for ep in (self.desc_in, self.s_blk, self.cmd_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_mm_store_task",
            "cg_mm_store_task.h",
            ("desc_in", "s_blk", "cmd_out"),
            template_args=(
                int(self.mem_dwidth),
                int(self.K),
                int(self.N),
                int(self.L),
                int(self.sob_depth),
            ),
        )

    def run_iter(self) -> ProcessGen[None]:
        f, w = self.formats, int(self.mem_dwidth)
        nw = nwords(int(self.K) * int(self.N), w)
        desc = yield from self.desc_in.get_schema(CgDesc)
        nit, out = int(desc.nit), int(desc.x_off)
        for n in range(nit):
            sb = yield from self.s_blk.acquire_read()
            words = to_words(*sb.payload, f.S, w)
            cmd = MemWCmd(addr=out + n * nw, len=len(words), fwd_bursts=0)
            yield from self.cmd_out.write(
                np.asarray(cmd.serialize(word_bw=w), np.uint64)
            )
            yield from self.cmd_out.write(words)
            yield from self.s_blk.release_read()
        # The echo rides on a zero-length write, so a job writes as often as it reads (nit + 1):
        # the RTL's pointer FIFOs couple the reader's and writer's firing counts (cg_mm_store_task.h).
        echo = MemWCmd(addr=out, len=0, fwd_bursts=1)
        yield from self.cmd_out.write(np.asarray(echo.serialize(word_bw=w), np.uint64))
        yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), np.uint64))


@dataclass
class CgMmUnit(FreeRunMod):
    """The matrix multiply run from memory (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_mm_unit"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    L: HwParam[int] = DEFAULT_L
    R: HwParam[int] = 0
    C: HwParam[int] = DEFAULT_C
    cmul: HwParam[int] = DEFAULT_CMUL
    fmt: HwParam[int] = DEFAULT_FMT
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
        self.rx = CgMmRx(name=f"{self.name}_rx", sim=self.sim, **kw)
        self.rstream = MemRStream(
            name=f"{self.name}_memr",
            sim=self.sim,
            mem_dwidth=w,
            inband=True,
            clk=self.clk,
        )
        self.load = CgMmLoad(name=f"{self.name}_load", sim=self.sim, **kwf)
        self.mm = CgMm(
            name=f"{self.name}_mm",
            sim=self.sim,
            R=int(self.R),
            C=int(self.C),
            cmul=int(self.cmul),
            **kwf,
        )
        self.store = CgMmStore(name=f"{self.name}_store", sim=self.sim, **kwf)
        self.wstream = MemWStream(
            name=f"{self.name}_memw",
            sim=self.sim,
            mem_dwidth=w,
            inband=True,
            emit_done=True,
            clk=self.clk,
        )
        for c in (self.rx, self.rstream, self.load, self.mm, self.store, self.wstream):
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
        _sif("iter", self.load.cmd_out, self.mm.cmd_in, depth=int(self.cmd_depth))
        _sif("wdata", self.store.cmd_out, self.wstream.s_in)
        _sobif("a_blk", self.load.a_blk, self.mm.a_blk)
        _sobif("p_blk", self.load.p_blk, self.mm.p_blk)
        _sobif("s_blk", self.mm.s_blk, self.store.s_blk)

        self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
        self.s_cmd = self.rx.s_cmd
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done


# --- the testbench graph and the procedure ---------------------------------------------------


@dataclass
class CgMmUnitTB(FreeRunMod):
    """The testbench graph: a driver, a sink, one arena behind both ``m_axi`` bundles, and the
    :class:`CgMmUnit`.  ``jobs`` gives each job's ``nit``."""

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    jobs: tuple = (4,)
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    N: HwParam[int] = DEFAULT_N
    L: HwParam[int] = DEFAULT_L
    R: HwParam[int] = 0
    C: HwParam[int] = DEFAULT_C
    cmul: HwParam[int] = DEFAULT_CMUL
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
        nwa, nwp = nwords(K * K, w), nwords(K * N, w)
        cur, self.layout = 0, []
        for nit in self.jobs:  # (nit, a_off, p_off, out_off)
            nit = int(nit)
            self.layout.append((nit, cur, cur + nwa, cur + nwa + nit * nwp))
            cur += nwa + 2 * nit * nwp
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
        self.dut = CgMmUnit(
            name=f"{self.name}_dut",
            sim=self.sim,
            mem_dwidth=w,
            K=K,
            N=N,
            L=int(self.L),
            R=int(self.R),
            C=int(self.C),
            cmul=int(self.cmul),
            fmt=int(self.fmt),
            cmd_depth=int(self.cmd_depth),
            sob_depth=int(self.sob_depth),
            clk=self.clk,
        )
        self.cmds = [
            CgMmUnitCmd(a_off=a, p_off=p, out_off=o, nit=nit)
            for (nit, a, p, o) in self.layout
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


def mm_unit_golden(A, B, scale, nit: int, formats):
    """Per job: the stored ``A`` and ``P₀ … P_{nit−1}`` the unit reads (the golden CG
    trajectory), and the ``S₁ … S_nit`` it must write."""
    from examples.mimo_cg.mimo_cg_fixed import cg_init, quantize_inputs, vec_step

    ar, ai, br, bi = quantize_inputs(A, B, formats, scale)
    state = cg_init(br, bi, formats)
    p_seq, s_seq = [], []
    for _ in range(nit):
        p_seq.append((state.pr, state.pi))
        sr, si = mm_step(ar, ai, state.pr, state.pi, formats)
        s_seq.append((sr, si))
        state, _ = vec_step(state, sr, si, formats)
    return (ar, ai), p_seq, s_seq


class CgMmUnitSim:
    """The procedure around a :class:`CgMmUnitTB`: write the scenario, run, and check every
    ``S`` word against the golden.  ``problems`` are ``(A, B, scale)`` per job."""

    def __init__(self, problems, jobs, **tb_kw) -> None:
        self.problems = list(problems)
        self.tb = CgMmUnitTB(name="tb", sim=Simulation(), jobs=tuple(jobs), **tb_kw)
        self.expected: list[tuple[int, np.ndarray]] = []

    def scenario(self) -> dict:
        """Command words, the starting memory image and the golden image, as arrays."""
        tb = self.tb
        w = int(tb.mem_dwidth)
        f = hw_format(int(tb.fmt))
        mem_in = np.zeros(int(tb.mem.nwords_tot), np.uint64)
        golden = np.zeros_like(mem_in)
        self.expected = []
        for (nit, a_off, p_off, out_off), (A, B, scale) in zip(
            tb.layout, self.problems, strict=True
        ):
            a, p_seq, s_seq = mm_unit_golden(A, B, scale, nit, f)
            words = to_words(*a, f.A, w)
            mem_in[a_off : a_off + len(words)] = words
            p_words = np.concatenate([to_words(*p, f.P, w) for p in p_seq])
            mem_in[p_off : p_off + len(p_words)] = p_words
            exp = np.concatenate([to_words(*s, f.S, w) for s in s_seq])
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

    def run(self) -> CgMmUnitTB:
        with tempfile.TemporaryDirectory() as root:
            self.write_scenario(root)
            self.tb.sim.run_sim()
        return self.check()

    def check(self) -> CgMmUnitTB:
        tb = self.tb
        bpw = int(tb.mem_dwidth) // 8
        for j, (off, exp) in enumerate(self.expected):
            got = tb.mem._mem.read(off * bpw, len(exp)).astype(np.uint64)
            if not np.array_equal(got, exp):
                bad = int(np.argmax(got != exp))
                raise AssertionError(
                    f"cg_mm_unit job {j} word {bad}: 0x{int(got[bad]):016x} != "
                    f"golden 0x{int(exp[bad]):016x}"
                )
        assert len(tb.done_sink.words) == len(
            tb.layout
        ), f"expected one done per job ({len(tb.layout)}), got {len(tb.done_sink.words)}"
        return tb
