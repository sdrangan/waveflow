"""detector.py — the integrated CG detector ``CgDetector``: CG control, shared memory and queues.

Step 4.8 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 4.0 decision record, §14)::

    s_cmd → cg_cmd_rx → MemRStream → cg_load ─a_blk→ cg_mm ─s_blk→ cg_vec ─x_blk→ cg_store → MemWStream → s_done
                                        │  └─b_blk──────────────→ cg_vec ─p_blk→ cg_mm        ↑
                                        └─desc→ cg_ctrl ─(queues)→ cg_vec, cg_mm ─desc───────┘

* ``cg_cmd_rx`` reads one :class:`~examples.mimo_cg.hw.common.CgCmd` and frames two reads, ``A``
  (relaying the job's ``CgDesc``) and ``B``.
* ``cg_load`` lands ``A`` (K row elements, for the matmul) and ``B`` (lane groups, for the vector
  unit) in blocks and passes the descriptor to the control.
* ``cg_ctrl`` is **CG control**: per job it issues ``INIT``, then ``ITER`` … ``LAST`` (``nit``
  commands) into each block's **command queue** (a FIFO, depth ``cmd_depth``) and passes the
  descriptor on to the store.
* ``cg_vec`` and ``cg_mm`` (the step 4.2 and 4.5 leaves, unchanged) exchange ``P`` and ``S``
  through the ``p_blk`` / ``s_blk`` stream-of-blocks — the **shared memory**, ``sob_depth`` blocks
  each — in a feedback loop: ``P₀`` from ``INIT``, then each ``Sₙ`` makes the next ``P``, and
  ``LAST`` makes ``X`` instead.  Every firing of every task is one job, paced by its own command
  or descriptor, so the loop carries a token per job.
* ``cg_store`` writes ``X`` and then a zero-length write whose echo is the job's done on
  ``s_done``: a job must write as often as it reads (two each), because HLS couples the reader's
  and writer's firing counts through the ``m_axi`` pointer FIFOs (plan §15, step 4.7).

``nit`` is a runtime field (1 … K).  The Python bodies call the golden; timing is a placeholder.
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from examples.mimo_cg.hw.common import (
    DEFAULT_MEM_DW,
    DEFAULT_N,
    CgCmd,
    CgDesc,
    CgIterCmd,
    IterOp,
    from_words,
    hw_format,
    nwords,
    to_words,
)
from examples.mimo_cg.hw.mm import DEFAULT_C, DEFAULT_CMUL, CgMm, a_block_type
from examples.mimo_cg.hw.vec import (
    DEFAULT_FMT,
    DEFAULT_K,
    DEFAULT_L,
    CgVec,
    _put,
    block_type,
)
from examples.mimo_cg.mimo_cg_fixed import cg_fixed, quantize_inputs
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
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


@dataclass
class CgCmdRx(FreeRunMod):
    """Framer: one ``CgCmd`` → two reads, ``A`` (relaying the job's ``CgDesc``) and ``B``."""

    cpp_kernel_name: ClassVar[str | None] = "cg_cmd_rx"
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
            "cg_cmd_rx_task",
            "cg_cmd_rx_task.h",
            ("s_cmd", "cmd_out"),
            template_args=(int(self.mem_dwidth), int(self.K), int(self.N)),
        )

    def run_iter(self) -> ProcessGen[None]:
        w, K, N = int(self.mem_dwidth), int(self.K), int(self.N)
        cmd = yield from self.s_cmd.get_schema(CgCmd)
        nit = min(max(int(cmd.nit), 1), K)  # clamped as in cg_cmd_rx_task.h
        frames = [
            MemRCmd(addr=int(cmd.a_off), len=nwords(K * K, w), fwd_bursts=1),
            CgDesc(nit=nit, x_off=int(cmd.x_off)),
            MemRCmd(addr=int(cmd.b_off), len=nwords(K * N, w), fwd_bursts=0),
        ]
        for fr in frames:
            yield from self.cmd_out.write(
                np.asarray(fr.serialize(word_bw=w), np.uint64)
            )


@dataclass
class CgLoad(FreeRunMod):
    """Lands ``A`` and ``B`` in blocks and passes the job's descriptor to the control."""

    cpp_kernel_name: ClassVar[str | None] = "cg_load"
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
        self.a_blk = SobIFMaster(
            name=f"{self.name}_a_blk", sim=self.sim, element_type=a_block_type(f.A.W, K)
        )
        self.b_blk = SobIFMaster(
            name=f"{self.name}_b_blk",
            sim=self.sim,
            element_type=block_type(f.B.W, K, N, L),
        )
        for ep in (self.s_in, self.desc_out, self.a_blk, self.b_blk):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_load_task",
            "cg_load_task.h",
            ("s_in", "desc_out", "a_blk", "b_blk"),
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

    def run_iter(self) -> ProcessGen[None]:
        f, w, K, N = self.formats, int(self.mem_dwidth), int(self.K), int(self.N)
        desc = yield from self.s_in.get_schema(CgDesc)
        yield from self.desc_out.write(np.asarray(desc.serialize(word_bw=w), np.uint64))
        a = yield from self._read_matrix(f.A, K, K)
        ab = yield from self.a_blk.acquire_write()
        yield from self.a_blk.commit_write(_put(ab, *a))
        b = yield from self._read_matrix(f.B, K, N)
        bb = yield from self.b_blk.acquire_write()
        yield from self.b_blk.commit_write(_put(bb, *b))


@dataclass
class CgCtrl(FreeRunMod):
    """CG control: per job, ``INIT`` and then ``nit`` iteration commands into each block's
    command queue, and the descriptor on to the store."""

    cpp_kernel_name: ClassVar[str | None] = "cg_ctrl"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    K: HwParam[int] = DEFAULT_K
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.desc_in = StreamIFSlave(
            name=f"{self.name}_desc_in", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.vec_cmd = StreamIFMaster(
            name=f"{self.name}_vec_cmd", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.mm_cmd = StreamIFMaster(
            name=f"{self.name}_mm_cmd", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.desc_out = StreamIFMaster(
            name=f"{self.name}_desc_out", sim=self.sim, bitwidth=w, has_tlast=True
        )
        for ep in (self.desc_in, self.vec_cmd, self.mm_cmd, self.desc_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_ctrl_task",
            "cg_ctrl_task.h",
            ("desc_in", "vec_cmd", "mm_cmd", "desc_out"),
            template_args=(int(self.mem_dwidth), int(self.K)),
        )

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        desc = yield from self.desc_in.get_schema(CgDesc)
        nit = int(desc.nit)
        yield from self.desc_out.write(np.asarray(desc.serialize(word_bw=w), np.uint64))
        for n in range(nit + 1):
            op = IterOp.INIT if n == 0 else (IterOp.LAST if n == nit else IterOp.ITER)
            words = np.asarray(
                CgIterCmd(op=int(op), it=n).serialize(word_bw=w), np.uint64
            )
            yield from self.vec_cmd.write(words)
            yield from self.mm_cmd.write(words)


@dataclass
class CgStore(FreeRunMod):
    """Writes ``X``, then a zero-length write that echoes the job's descriptor on ``s_done``, so
    each job has as many writes as reads (see ``cg_store_task.h``)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_store"
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
        self.x_blk = SobIFSlave(
            name=f"{self.name}_x_blk",
            sim=self.sim,
            element_type=block_type(f.X.W, K, N, L),
        )
        self.cmd_out = StreamIFMaster(
            name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w, has_tlast=True
        )
        for ep in (self.desc_in, self.x_blk, self.cmd_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_store_task",
            "cg_store_task.h",
            ("desc_in", "x_blk", "cmd_out"),
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
        desc = yield from self.desc_in.get_schema(CgDesc)
        xb = yield from self.x_blk.acquire_read()
        words = to_words(*xb.payload, f.X, w)
        yield from self.x_blk.release_read()
        cmd = MemWCmd(addr=int(desc.x_off), len=len(words), fwd_bursts=0)
        yield from self.cmd_out.write(np.asarray(cmd.serialize(word_bw=w), np.uint64))
        yield from self.cmd_out.write(words)
        # The echo rides on a zero-length write, so a job writes as often as it reads (A, B): the
        # RTL's pointer FIFOs couple the reader's and writer's firing counts (cg_store_task.h).
        echo = MemWCmd(addr=int(desc.x_off), len=0, fwd_bursts=1)
        yield from self.cmd_out.write(np.asarray(echo.serialize(word_bw=w), np.uint64))
        yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), np.uint64))


@dataclass
class CgDetector(FreeRunMod):
    """The integrated CG detector (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_detector"
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
        kw = {"mem_dwidth": w, "K": int(self.K), "clk": self.clk}
        kwn = {**kw, "N": int(self.N)}
        kwf = {
            **kwn,
            "L": int(self.L),
            "fmt": int(self.fmt),
            "sob_depth": int(self.sob_depth),
        }
        self.rx = CgCmdRx(name=f"{self.name}_rx", sim=self.sim, **kwn)
        self.rstream = MemRStream(
            name=f"{self.name}_memr",
            sim=self.sim,
            mem_dwidth=w,
            inband=True,
            clk=self.clk,
        )
        self.load = CgLoad(name=f"{self.name}_load", sim=self.sim, **kwf)
        self.ctrl = CgCtrl(name=f"{self.name}_ctrl", sim=self.sim, **kw)
        self.vec = CgVec(name=f"{self.name}_vec", sim=self.sim, **kwf)
        self.mm = CgMm(
            name=f"{self.name}_mm",
            sim=self.sim,
            R=int(self.R),
            C=int(self.C),
            cmul=int(self.cmul),
            **kwf,
        )
        self.store = CgStore(name=f"{self.name}_store", sim=self.sim, **kwf)
        self.wstream = MemWStream(
            name=f"{self.name}_memw",
            sim=self.sim,
            mem_dwidth=w,
            inband=True,
            emit_done=True,
            clk=self.clk,
        )
        comps = (
            self.rx,
            self.rstream,
            self.load,
            self.ctrl,
            self.vec,
            self.mm,
            self.store,
            self.wstream,
        )
        for c in comps:
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
        _sif("desc_lc", self.load.desc_out, self.ctrl.desc_in)
        _sif("desc_cs", self.ctrl.desc_out, self.store.desc_in)
        _sif("vec_q", self.ctrl.vec_cmd, self.vec.cmd_in, depth=int(self.cmd_depth))
        _sif("mm_q", self.ctrl.mm_cmd, self.mm.cmd_in, depth=int(self.cmd_depth))
        _sif("wdata", self.store.cmd_out, self.wstream.s_in)
        _sobif("a_blk", self.load.a_blk, self.mm.a_blk)
        _sobif("b_blk", self.load.b_blk, self.vec.b_blk)
        _sobif("p_blk", self.vec.p_blk, self.mm.p_blk)
        _sobif("s_blk", self.mm.s_blk, self.vec.s_blk)
        _sobif("x_blk", self.vec.x_blk, self.store.x_blk)

        self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
        self.s_cmd = self.rx.s_cmd
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done


# --- problems, the testbench graph and the procedure ------------------------------------------


def detector_problems(M: int, K: int, N: int, n: int, seed: int, modulation: int = 16):
    """``n`` random uplink problems ``(A, B, M)`` from M × K i.i.d. Rayleigh channels with
    ``modulation``-QAM symbols at an SNR uniform in −5 … 25 dB; the last has column 2 of ``B``
    set to zero, the zero-residual case."""
    from examples.mimo_cg.detectors import mmse_matrix
    from examples.mimo_cg.mimo_link import Qam, noise_variance, point_rng, rayleigh

    qam = Qam(modulation)
    out = []
    for c in range(n):
        rng = point_rng(61, M, K, seed, c)
        sigma2 = noise_variance(float(rng.uniform(-5.0, 25.0)))
        H = rayleigh(rng, (M, K))
        tx = rng.integers(0, 2, size=(N, K * qam.bits_per_symbol))
        Y = H @ qam.modulate(tx).T + math.sqrt(sigma2) * rayleigh(rng, (M, N))
        out.append((mmse_matrix(H, sigma2), H.conj().T @ Y, float(M)))
    A, B, scale = out[-1]
    B = B.copy()
    B[:, 2] = 0
    out[-1] = (A, B, scale)
    return out


@dataclass
class CgDetectorTB(FreeRunMod):
    """The testbench graph: a driver, a sink, one arena behind both ``m_axi`` bundles, and the
    :class:`CgDetector`.  ``jobs`` gives each job's ``nit``."""

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
    n_cycles: int = 400_000
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        bad = [n for n in self.jobs if not 1 <= int(n) <= int(self.K)]
        if bad:
            raise ValueError(f"nit must lie in 1..K = {int(self.K)}; got {bad}")
        w, K, N = int(self.mem_dwidth), int(self.K), int(self.N)
        nwa, nwb = nwords(K * K, w), nwords(K * N, w)
        cur, self.layout = 0, []
        for nit in self.jobs:  # (nit, a_off, b_off, x_off)
            self.layout.append((int(nit), cur, cur + nwa, cur + nwa + nwb))
            cur += nwa + 2 * nwb
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
        self.dut = CgDetector(
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
            CgCmd(a_off=a, b_off=b, x_off=x, nit=nit) for (nit, a, b, x) in self.layout
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


class CgDetectorSim:
    """The procedure around a :class:`CgDetectorTB`: write the scenario, run, and check every
    ``X`` word against ``cg_fixed`` itself.  ``problems`` are ``(A, B, scale)`` per job.
    """

    def __init__(self, problems, jobs, **tb_kw) -> None:
        self.problems = list(problems)
        self.tb = CgDetectorTB(name="tb", sim=Simulation(), jobs=tuple(jobs), **tb_kw)
        self.expected: list[tuple[int, np.ndarray]] = []

    def scenario(self) -> dict:
        """Command words, the starting memory image and the golden image, as arrays."""
        tb = self.tb
        w = int(tb.mem_dwidth)
        f = hw_format(int(tb.fmt))
        mem_in = np.zeros(int(tb.mem.nwords_tot), np.uint64)
        golden = np.zeros_like(mem_in)
        self.expected = []
        for (nit, a_off, b_off, x_off), (A, B, scale) in zip(
            tb.layout, self.problems, strict=True
        ):
            ar, ai, br, bi = quantize_inputs(A, B, f, scale)
            wa, wb = to_words(ar, ai, f.A, w), to_words(br, bi, f.B, w)
            mem_in[a_off : a_off + len(wa)] = wa
            mem_in[b_off : b_off + len(wb)] = wb
            x = cg_fixed(A, B, nit, f, scale=scale)
            exp = to_words(x.re, x.im, f.X, w)
            golden[x_off : x_off + len(exp)] = exp
            self.expected.append((x_off, exp))
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

    def run(self) -> CgDetectorTB:
        with tempfile.TemporaryDirectory() as root:
            self.write_scenario(root)
            self.tb.sim.run_sim()
        return self.check()

    def check(self) -> CgDetectorTB:
        tb = self.tb
        bpw = int(tb.mem_dwidth) // 8
        for j, (off, exp) in enumerate(self.expected):
            got = tb.mem._mem.read(off * bpw, len(exp)).astype(np.uint64)
            if not np.array_equal(got, exp):
                bad = int(np.argmax(got != exp))
                raise AssertionError(
                    f"cg_detector job {j} word {bad}: 0x{int(got[bad]):016x} != "
                    f"golden 0x{int(exp[bad]):016x}"
                )
        assert len(tb.done_sink.words) == len(
            tb.layout
        ), f"expected one done per job ({len(tb.layout)}), got {len(tb.done_sink.words)}"
        return tb
