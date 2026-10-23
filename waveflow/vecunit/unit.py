"""unit.py — the vector unit in pysim: three tasks joined by queues (``plans/vecunit_new.md``, Stage 4).

``VecUnit`` is a composite of :class:`VecLoad`, :class:`VecExec` and :class:`VecStore`, each a
free-running task taking its own program stream -- decoupled access/execute:

* ``s_lprog`` → **VecLoad**: per ``wf.ld q, port, k, n``, one burst of ``k x n`` memory elements from
  input port ``port``, landed as the next block of input queue ``q`` (row-major lane groups,
  ``ceil(n / L)`` a row);
* ``s_xprog`` → **VecExec**: the execute program on a register file and a scalar file that live for
  one firing, one job (D6), reading input-queue blocks and filling output-queue blocks; one
  ``VecStatus(tx_id, status)`` per job on ``m_status``.  An error is fatal: the status, then the
  task takes nothing more;
* ``s_sprog`` → **VecStore**: per ``wf.st port, q, tag, k, n``, the next block of output queue ``q``
  as ``VecResp(tx_id, tag, k, n)`` then its ``k x n`` memory elements, on output port ``port``.

Every program is one framed burst: its ``VecJob(tx_id, ninstr)`` word, then the instructions.  A queue
is a stream-of-blocks channel (a ping-pong of two blocks): the execute task takes a block at its first
``wf.vlse`` (or ``wf.vsse``) and gives it back at ``wf.qpop`` (``wf.qpush``), so the load task fills
the next block while the current one is in use, and stalls when both are taken.

The execute task's arithmetic is the interpreter's (:class:`~waveflow.vecunit.model.Machine`); its
time is charged per instruction from :class:`VecTiming` -- rough constants until Stage 7 calibrates
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    FramedStreamIFSlave,
    SobIFMaster,
    SobIFSlave,
    StreamOfBlocksIF,
)
from waveflow.linalg.lanes import block_type, from_words, to_words
from waveflow.linalg.message import Status
from waveflow.simulation.simobj import ProcessGen
from waveflow.vecunit import isa as I
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.model import (
    Block,
    Machine,
    block_from_matrix,
    instr_status,
    load_status,
    matrix_from_block,
    operands,
    pass_status,
    split_passes,
    store_status,
)

#: Program, status and response streams are 64-bit words.
CTRL_BITS = 64


@dataclass(frozen=True)
class VecTiming:
    """The execute task's cycles, per instruction: rough constants (Stage 1's witness for the pass
    depth, the CG core's calibrated divide), until Stage 7 calibrates them."""

    instr: float = 1.0  # an instruction word read and decoded
    pass_overhead: float = 4.0  # a pass's operand resolution and setup
    pass_depth: float = 22.0  # its pipeline's fill and drain (Stage 1, witness 3)
    divide: float = 45.0  # wf.sdivz
    scalar: float = 2.0  # wf.smv
    control: float = 1.0  # wf.setvl, wf.qpop, wf.qpush
    load: float = 3.0  # the loader's last lane group after its last word

    def pass_cycles(self, n_instr: int, vl: int) -> float:
        return self.instr * n_instr + self.pass_overhead + vl + self.pass_depth


@dataclass
class _VecPart(FreeRunMod):
    """What every task of the unit shares: the configuration and the clock."""

    cfg: VecUnitConfig = None  # type: ignore[assignment]  # required: checked below
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))
    #: the configuration is a build input that is not a template parameter
    build_fields: ClassVar[tuple[str, ...]] = ("cfg",)
    timing: ClassVar[VecTiming] = VecTiming()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.cfg is None:
            raise ValueError(f"{self.name}: a vector unit needs its VecUnitConfig")

    def _ep(self, cls, name: str, bits: int):
        ep = cls(name=f"{self.name}_{name}", sim=self.sim, bitwidth=int(bits), has_tlast=True)
        self.add_endpoint(ep)
        return ep

    def _block(self, cls, name: str):
        """A queue end: blocks of ``Kmax x ceil(Nmax / L)`` lane groups of ``WV``-bit values (every
        queue the widest vector format's width, each value in its queue's own format)."""
        cfg = self.cfg
        ep = cls(name=f"{self.name}_{name}", sim=self.sim,
                 element_type=block_type(cfg.WV, cfg.Kmax * cfg.NGQ, cfg.L))
        self.add_endpoint(ep)
        setattr(self, name, ep)
        return ep

    def _named(self, cls, name: str, bits: int):
        ep = self._ep(cls, name, bits)
        setattr(self, name, ep)
        return ep

    def _kernel(self, role: str, signature: tuple[str, ...], framed: tuple[str, ...]):
        from waveflow.hw.mem_stream import KernelTask
        from waveflow.vecunit.hls import header_name, task_names

        return KernelTask(task_names(self.cfg)[role], header_name(self.cfg), signature,
                          framed=framed)

    def _halt(self) -> ProcessGen[None]:
        """Fatal: never take input again (a reset clears it)."""
        yield self.env.event()

    @staticmethod
    def _program(words) -> tuple[int, list[I.Instr], bool]:
        """A program burst: ``(tx_id, instructions, whether ninstr matched the burst)``."""
        w = [int(x) for x in np.asarray(words, np.uint64).reshape(-1)]
        tx_id, n = w[0] & 0xFFFF, (w[0] >> 16) & 0xFFFF
        return tx_id, [I.Instr.decode(x) for x in w[1:]], n == len(w) - 1


@dataclass
class VecLoad(_VecPart):
    """The load task: lands each ``wf.ld``'s burst as the next block of its input queue."""

    cpp_kernel_name: ClassVar[str | None] = "vecunit_load"

    def __post_init__(self) -> None:
        super().__post_init__()
        cfg = self.cfg
        self.s_prog = self._ep(FramedStreamIFSlave, "s_prog", CTRL_BITS)
        self.s_in = [self._named(FramedStreamIFSlave, f"s_in{p}", cfg.word_bits)
                     for p in range(cfg.n_in_ports)]
        self.q = [self._block(SobIFMaster, f"q{j}") for j in range(cfg.NQI)]
        self.halted = False

    def kernel_task(self):
        sig = ("s_prog", *(f"s_in{p}" for p in range(self.cfg.n_in_ports)),
               *(f"q{j}" for j in range(self.cfg.NQI)))
        return self._kernel("load", sig, sig[:1 + self.cfg.n_in_ports])

    def run_iter(self) -> ProcessGen[None]:
        if self.halted:
            yield from self._halt()
        cfg = self.cfg
        _tx, prog, framed = self._program((yield from self.s_prog.get()))
        for x in prog:
            if not framed or not load_status(x, cfg):
                self.halted = True
                return
            k, n = int(x.imm1), int(x.imm2)
            words = yield from self.s_in[x.rs1].get()
            re, im = from_words(words, k * n, cfg.in_queue_formats[x.rd], cfg.lane_bits,
                                cfg.word_bits)
            blk = yield from self.q[x.rd].acquire_write()
            blk.payload = block_from_matrix(re.reshape(k, n), im.reshape(k, n), cfg)
            yield self.timeout(self.timing.load * self.clk.period)
            yield from self.q[x.rd].commit_write(blk)


@dataclass
class VecExec(_VecPart):
    """The execute task: one firing is one job (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "vecunit_exec"

    def __post_init__(self) -> None:
        super().__post_init__()
        cfg = self.cfg
        self.s_prog = self._ep(FramedStreamIFSlave, "s_prog", CTRL_BITS)
        self.qi = [self._block(SobIFSlave, f"qi{j}") for j in range(cfg.NQI)]
        self.qo = [self._block(SobIFMaster, f"qo{j}") for j in range(cfg.NQO)]
        self.m_status = self._ep(FramedStreamIFMaster, "m_status", CTRL_BITS)
        self.halted = False

    def kernel_task(self):
        sig = ("s_prog", *(f"qi{j}" for j in range(self.cfg.NQI)),
               *(f"qo{j}" for j in range(self.cfg.NQO)), "m_status")
        return self._kernel("exec", sig, ("s_prog", "m_status"))

    def run_iter(self) -> ProcessGen[None]:
        if self.halted:
            yield from self._halt()
        cfg, t, period = self.cfg, self.timing, self.clk.period
        tx_id, prog, framed = self._program((yield from self.s_prog.get()))
        m = Machine(cfg)
        held_out: list = [None] * cfg.NQO  # the block handle of each output queue's tail
        st = Status.OK if framed else Status.BAD_LENGTH
        for chain in split_passes(prog, cfg.S) if framed else []:
            for x in chain:
                st = instr_status(x, cfg)
                if st != Status.OK:
                    break
            if st == Status.OK and chain[0].vector:
                if chain[-1].chain or not all(y.vector for y in chain):
                    st = Status.BAD_OP
                else:
                    st = pass_status(chain, cfg)
            if st != Status.OK:
                break
            x = chain[0]
            if x.vector:
                for y in chain:  # take each queue block at its first use
                    o = operands(y)
                    if o.qread is not None and m.head[o.qread] is None:
                        blk = yield from self.qi[o.qread].acquire_read()
                        m.head[o.qread] = blk.payload
                    if o.qwrite is not None and m.tail[o.qwrite] is None:
                        held_out[o.qwrite] = yield from self.qo[o.qwrite].acquire_write()
                        m.tail[o.qwrite] = Block.empty(cfg.Kmax * cfg.NGQ, cfg.L)
                m.run_pass(chain, "serial")
                yield self.timeout(t.pass_cycles(len(chain), m.vl) * period)
            elif int(x.opcode) == I.Opcode.SCALAR:
                m.scalar(x)
                yield self.timeout((t.divide if int(x.funct) == I.SOp.DIVZ else t.scalar) * period)
            elif int(x.funct) == I.COp.SETVL:
                m.control(x)
                yield self.timeout(t.control * period)
            elif int(x.funct) == I.COp.QPOP:
                if m.head[x.rs1] is None:  # released without a read: take it first
                    yield from self.qi[x.rs1].acquire_read()
                m.head[x.rs1] = None
                yield self.timeout(t.control * period)
                yield from self.qi[x.rs1].release_read()
            else:  # QPUSH
                if m.tail[x.rd] is None:
                    held_out[x.rd] = yield from self.qo[x.rd].acquire_write()
                    m.tail[x.rd] = Block.empty(cfg.Kmax * cfg.NGQ, cfg.L)
                held_out[x.rd].payload = m.tail[x.rd]
                m.tail[x.rd] = None
                yield self.timeout(t.control * period)
                yield from self.qo[x.rd].commit_write(held_out[x.rd])
        s = I.VecStatus()
        s.tx_id, s.status = int(tx_id), int(st)
        yield from self.m_status.write(np.asarray(s.serialize(word_bw=CTRL_BITS), np.uint64))
        if st != Status.OK:
            self.halted = True


@dataclass
class VecStore(_VecPart):
    """The store task: each ``wf.st`` as a ``VecResp`` and its matrix on an output port."""

    cpp_kernel_name: ClassVar[str | None] = "vecunit_store"

    def __post_init__(self) -> None:
        super().__post_init__()
        cfg = self.cfg
        self.s_prog = self._ep(FramedStreamIFSlave, "s_prog", CTRL_BITS)
        self.q = [self._block(SobIFSlave, f"q{j}") for j in range(cfg.NQO)]
        self.s_out = [self._named(FramedStreamIFMaster, f"s_out{p}", cfg.word_bits)
                      for p in range(cfg.n_out_ports)]
        self.halted = False

    def kernel_task(self):
        sig = ("s_prog", *(f"q{j}" for j in range(self.cfg.NQO)),
               *(f"s_out{p}" for p in range(self.cfg.n_out_ports)))
        return self._kernel("store", sig, ("s_prog", *sig[1 + self.cfg.NQO:]))

    def run_iter(self) -> ProcessGen[None]:
        if self.halted:
            yield from self._halt()
        cfg = self.cfg
        tx_id, prog, framed = self._program((yield from self.s_prog.get()))
        for x in prog:
            if not framed or not store_status(x, cfg):
                self.halted = True
                return
            k, n = int(x.imm1), int(x.imm2)
            blk = yield from self.q[x.rs1].acquire_read()
            re, im = matrix_from_block(blk.payload, k, n, cfg)
            resp = I.VecResp()
            resp.tx_id, resp.tag, resp.k, resp.n = int(tx_id), int(x.rs2), k, n
            out = self.s_out[x.rd]
            yield from out.write(np.asarray(resp.serialize(word_bw=cfg.word_bits), np.uint64))
            yield from out.write(np.asarray(
                to_words(re, im, cfg.out_queue_formats[x.rs1], cfg.lane_bits, cfg.word_bits),
                np.uint64))
            yield from self.q[x.rs1].release_read()


@dataclass
class VecUnit(_VecPart):
    """The vector unit: programs in on ``s_lprog``, ``s_xprog``, ``s_sprog``; matrices in on
    ``s_in<p>`` and out on ``s_out<p>``; a status per job on ``m_status`` (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "vecunit"
    endpoint_port_widths: ClassVar[bool] = True  # 64-bit programs and status beside data words

    def __post_init__(self) -> None:
        super().__post_init__()
        cfg = self.cfg
        self.load = VecLoad(name=f"{self.name}_load", sim=self.sim, clk=self.clk, cfg=cfg)
        self.exe = VecExec(name=f"{self.name}_exec", sim=self.sim, clk=self.clk, cfg=cfg)
        self.store = VecStore(name=f"{self.name}_store", sim=self.sim, clk=self.clk, cfg=cfg)
        for comp in (self.load, self.exe, self.store):
            self.add_comp(comp)
        for j in range(cfg.NQI):
            self._blocks(f"qi{j}", self.load.q[j], self.exe.qi[j])
        for j in range(cfg.NQO):
            self._blocks(f"qo{j}", self.exe.qo[j], self.store.q[j])
        self.s_lprog, self.s_xprog, self.s_sprog = (self.load.s_prog, self.exe.s_prog,
                                                    self.store.s_prog)
        self.m_status = self.exe.m_status
        for i, ep in enumerate(self.load.s_in):
            setattr(self, f"s_in{i}", ep)
        for i, ep in enumerate(self.store.s_out):
            setattr(self, f"s_out{i}", ep)
        # Boundary names pair with the children's unwired ports in walk order: the load task's
        # program and inputs, the execute task's program and status, the store task's program and
        # outputs.
        self.boundary = (["s_lprog"] + [f"s_in{i}" for i in range(cfg.n_in_ports)]
                         + ["s_xprog", "m_status", "s_sprog"]
                         + [f"s_out{i}" for i in range(cfg.n_out_ports)])
        self.extra_includes = ["hls_streamofblocks.h"]

    def _blocks(self, name, master, slave) -> StreamOfBlocksIF:
        iface = StreamOfBlocksIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                                 element_type=master.element_type, depth=2)
        iface.bind("master", master)
        iface.bind("slave", slave)
        self.add_if(iface)
        return iface

    # -- scenarios --------------------------------------------------------------------------------
    def jobs(self, kind: str = "test") -> list:
        """The jobs of a scenario: ``(tx_id, Programs, inputs)``.  ``"test"``: random jobs over
        every instruction form (:func:`waveflow.vecunit.workload.random_jobs`).  An application
        subclasses the unit to give its own."""
        from waveflow.vecunit.workload import random_jobs

        if kind == "test":
            return random_jobs(self.cfg, 4, seed=1)
        raise ValueError(f"no workload {kind!r}")

    def workload(self, kind: str = "test") -> dict:
        from waveflow.vecunit.workload import job_bursts

        return job_bursts(self.cfg, self.jobs(kind))

    def golden(self, kind: str = "test") -> dict:
        """Every output port's bursts and ``m_status``, from the interpreter."""
        from waveflow.vecunit.workload import golden_bursts

        return golden_bursts(self.cfg, self.jobs(kind))
