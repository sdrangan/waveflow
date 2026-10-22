"""model.py — the bit-exact interpreter of the vector unit: its golden.

It runs a job's three programs on stored integers with :mod:`waveflow.utils.fixputils`, each value in
its register's own format.  Every product and sum is exact; a value rounds and saturates only when it
is written -- to a vector group, a scalar, or a queue -- to that destination's format.

**Passes.**  A vector instruction and the ones chained to it (``chain``, the ``.c`` suffix) form a
pass.  ``mode="pass"`` runs it as the hardware does, element by element, every slot at element ``i``
before any slot at ``i + 1``, so a later slot reads an earlier slot's result for the same element
after its quantization.  ``mode="serial"`` runs the same instructions one after another, each over
every element.  The two agree bit for bit whenever the pass rules hold (:func:`pass_status`), which
the hardware checks: chaining never changes a result.

**Blocks** are modelled in the hardware's layout: a flat array of lane groups, row ``i``'s column
group ``g`` at ``i * stride + g``, so a program's stride means what it means at RTL.  A read of a lane
group no load or store wrote is a program error (:class:`ProgramError`), not a status: the hardware
would read stale data.

**Status.**  The execute task checks every instruction it decodes, and every pass it gathers
(:func:`instr_status`, :func:`pass_status`): an unknown operation, or a conjugate, broadcast or chain
where none is allowed, is ``BAD_OP``; an index, ``VL``, column group or stride out of range is
``BAD_DIMS``; a pass that breaks a pass rule is ``BAD_OP``.  An error is fatal: the job stops at the
instruction, and the unit takes no more input.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from waveflow.linalg.message import Status
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format
from waveflow.vecunit.config import Cap, VecUnitConfig
from waveflow.vecunit.isa import (
    MULTIPLYING,
    COp,
    Instr,
    Opcode,
    QOp,
    SOp,
    VOp,
    is_vector,
)

#: ``vmv.v.i``'s immediate: a signed 12-bit integer.
IMM_FORMAT = Format(12, 12, True)


class ProgramError(Exception):
    """A program that the hardware would run but whose result is undefined (a read of a register or
    a lane group never written, a queue read while empty): the assembler refuses these."""


@dataclass
class Block:
    """A queue block: ``rows`` lane-group rows of ``groups`` lane groups, each ``L`` complex values in
    the queue's format, and which lane groups were written."""

    re: np.ndarray  # (slots, L) stored integers
    im: np.ndarray
    written: np.ndarray  # (slots,) bool

    @classmethod
    def empty(cls, slots: int, L: int) -> Block:
        return cls(np.zeros((slots, L), np.int64), np.zeros((slots, L), np.int64),
                   np.zeros(slots, bool))


@dataclass
class JobResult:
    """What one job produced."""

    status: int
    #: per output port: ``(resp, re, im)`` per ``wf.st``, ``resp`` a dict of ``VecResp``'s fields
    outputs: dict[int, list] = field(default_factory=dict)
    #: the number of execute instructions that ran
    executed: int = 0


# --- the checks the hardware makes ------------------------------------------------------------------


def instr_status(x: Instr, cfg: VecUnitConfig) -> Status:
    """The status the execute task gives one instruction as it decodes it."""
    op, f = int(x.opcode), int(x.funct)
    vector = is_vector(op, f)
    if x.chain and not vector:
        return Status.BAD_OP
    if op == Opcode.VEC:
        if f not in tuple(VOp):
            return Status.BAD_OP
        v = VOp(f)
        if (x.conj or x.bcast) and v not in MULTIPLYING:
            return Status.BAD_OP
        if v == VOp.MVI:
            return _ranges((x.rd, cfg.NG))
        if v == VOp.MV:
            return _ranges((x.rd, cfg.NG), (x.rs1, cfg.NG))
        if v == VOp.DOTR:
            return _ranges((x.rd, cfg.NS), (x.rs1, cfg.NG), (x.rs2, cfg.NG))
        if v in (VOp.MACC_S, VOp.NMSAC_S, VOp.MADD_S):
            return _ranges((x.rd, cfg.NG), (x.rs1, cfg.NS), (x.rs2, cfg.NG))
        return _ranges((x.rd, cfg.NG), (x.rs1, cfg.NG), (x.rs2, cfg.NG))
    if x.conj or x.bcast:
        return Status.BAD_OP
    if op == Opcode.QUEUE:
        if f == QOp.VLSE:
            return _ranges((x.rd, cfg.NG), (x.rs1, cfg.NQI), (x.imm1, cfg.NGQ),
                           (x.imm2 - 1, cfg.NGQ))
        if f == QOp.VSSE:
            return _ranges((x.rd, cfg.NQO), (x.rs1, cfg.NG), (x.imm1, cfg.NGQ),
                           (x.imm2 - 1, cfg.NGQ))
        return Status.BAD_OP  # wf.ld / wf.st belong to the load and store programs
    if op == Opcode.SCALAR:
        if f == SOp.DIVZ:
            return _ranges((x.rd, cfg.NS), (x.rs1, cfg.NS), (x.rs2, cfg.NS))
        if f == SOp.MV:
            return _ranges((x.rd, cfg.NS), (x.rs1, cfg.NS))
        return Status.BAD_OP
    if op == Opcode.CTRL:
        if f == COp.SETVL:
            return Status.OK if 1 <= x.imm1 <= cfg.VLMAX else Status.BAD_DIMS
        if f == COp.QPOP:
            return _ranges((x.rs1, cfg.NQI))
        if f == COp.QPUSH:
            return _ranges((x.rd, cfg.NQO))
        return Status.BAD_OP
    return Status.BAD_OP


def _ranges(*pairs) -> Status:
    return Status.OK if all(0 <= int(v) < int(n) for v, n in pairs) else Status.BAD_DIMS


def cap_of(x: Instr) -> Cap:
    """The slot capability an execute instruction needs."""
    if int(x.opcode) == Opcode.QUEUE:
        return Cap.LOAD if int(x.funct) == QOp.VLSE else Cap.STORE
    v = VOp(int(x.funct))
    if v in (VOp.MVI, VOp.MV, VOp.ADD, VOp.SUB):
        return Cap.MOVE
    return Cap.DOT if v == VOp.DOTR else Cap.MUL


@dataclass(frozen=True)
class Operands:
    """What one execute instruction reads and writes: vector groups, scalars and queues."""

    vreads: tuple[int, ...] = ()
    sreads: tuple[int, ...] = ()
    vwrite: int | None = None
    swrite: int | None = None
    qread: int | None = None
    qwrite: int | None = None


def operands(x: Instr) -> Operands:
    """The registers and queues an (in-range) execute instruction touches."""
    op, f = int(x.opcode), int(x.funct)
    if op == Opcode.VEC:
        v = VOp(f)
        if v == VOp.MVI:
            return Operands(vwrite=x.rd)
        if v == VOp.MV:
            return Operands(vreads=(x.rs1,), vwrite=x.rd)
        if v in (VOp.ADD, VOp.SUB, VOp.MUL):
            return Operands(vreads=(x.rs1, x.rs2), vwrite=x.rd)
        if v in (VOp.MACC, VOp.NMSAC, VOp.MADD):
            return Operands(vreads=(x.rd, x.rs1, x.rs2), vwrite=x.rd)
        if v in (VOp.MACC_S, VOp.NMSAC_S, VOp.MADD_S):
            return Operands(vreads=(x.rd, x.rs2), sreads=(x.rs1,), vwrite=x.rd)
        return Operands(vreads=(x.rs1, x.rs2), swrite=x.rd)  # DOTR
    if op == Opcode.QUEUE:
        if f == QOp.VLSE:
            return Operands(vwrite=x.rd, qread=x.rs1)
        return Operands(vreads=(x.rs1,), qwrite=x.rd)  # VSSE
    if op == Opcode.SCALAR:
        return Operands(sreads=(x.rs1, x.rs2) if f == SOp.DIVZ else (x.rs1,), swrite=x.rd)
    return Operands()


def pass_status(chain: list[Instr], cfg: VecUnitConfig) -> Status:
    """The rules a pass must keep, as the execute task checks them once it has gathered the pass
    (each instruction already passed :func:`instr_status`):

    * at most ``S`` instructions, each in a slot with the capability it needs;
    * a group read from the register file (not forwarded from an earlier slot of the pass) is the
      only group of its bank the pass reads;
    * one write per bank, so two slots never write one group;
    * one ``wf.vlse`` per input queue and one ``wf.vsse`` per output queue;
    * no slot reads a scalar a reduction of the same pass writes (reductions deliver at the end).
    """
    if not 1 <= len(chain) <= cfg.S:
        return Status.BAD_OP
    bank_read: dict[int, int] = {}
    bank_write: set[int] = set()
    written: set[int] = set()
    dots: set[int] = set()
    qin: set[int] = set()
    qout: set[int] = set()
    for s, x in enumerate(chain):
        if not cfg.has_cap(s, cap_of(x)):
            return Status.BAD_OP
        o = operands(x)
        for g in o.vreads:
            if g in written:
                continue  # forwarded
            b = cfg.bank(g)
            if bank_read.setdefault(b, g) != g:
                return Status.BAD_OP
        if any(sr in dots for sr in o.sreads):
            return Status.BAD_OP
        if o.vwrite is not None:
            b = cfg.bank(o.vwrite)
            if b in bank_write:
                return Status.BAD_OP
            bank_write.add(b)
            written.add(o.vwrite)
        if o.swrite is not None:
            if o.swrite in dots:
                return Status.BAD_OP
            dots.add(o.swrite)
        for q, seen in ((o.qread, qin), (o.qwrite, qout)):
            if q is not None:
                if q in seen:
                    return Status.BAD_OP
                seen.add(q)
    return Status.OK


def load_status(x: Instr, cfg: VecUnitConfig) -> bool:
    """Whether the load task can run a load-program instruction."""
    return (int(x.opcode) == Opcode.QUEUE and int(x.funct) == QOp.LD and not x.chain
            and not x.conj and not x.bcast and 0 <= x.rd < cfg.NQI and 0 <= x.rs1 < cfg.n_in_ports
            and 1 <= x.imm1 <= cfg.Kmax and 1 <= x.imm2 <= cfg.Nmax)


def store_status(x: Instr, cfg: VecUnitConfig) -> bool:
    """Whether the store task can run a store-program instruction."""
    return (int(x.opcode) == Opcode.QUEUE and int(x.funct) == QOp.ST and not x.chain
            and not x.conj and not x.bcast and 0 <= x.rd < cfg.n_out_ports
            and 0 <= x.rs1 < cfg.NQO and 1 <= x.imm1 <= cfg.Kmax and 1 <= x.imm2 <= cfg.Nmax)


def split_passes(prog: list[Instr], S: int) -> list[list[Instr]]:
    """The program as the execute task gathers it: a pass is a vector instruction and the ones
    chained to it (at most ``S``); every other instruction stands alone."""
    out, cur = [], []
    for x in prog:
        cur.append(x)
        if not (x.vector and x.chain) or len(cur) == S:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


# --- block layout ------------------------------------------------------------------------------------


def block_from_matrix(re: np.ndarray, im: np.ndarray, cfg: VecUnitConfig) -> Block:
    """A loaded ``k x n`` matrix as the loader lands it: row-major lane groups, ``ceil(n / L)`` a
    row, the spare lanes of a row's last group zero."""
    re, im = np.asarray(re, np.int64), np.asarray(im, np.int64)
    k, n = re.shape
    L, gpr = cfg.L, cfg.groups_per_row(n)
    blk = Block.empty(cfg.Kmax * cfg.NGQ, L)
    pr = np.zeros((k, gpr * L), np.int64)
    pi = np.zeros((k, gpr * L), np.int64)
    pr[:, :n], pi[:, :n] = re, im
    blk.re[: k * gpr] = pr.reshape(k * gpr, L)
    blk.im[: k * gpr] = pi.reshape(k * gpr, L)
    blk.written[: k * gpr] = True
    return blk


def matrix_from_block(blk: Block, k: int, n: int, cfg: VecUnitConfig) -> tuple[np.ndarray, np.ndarray]:
    """The ``k x n`` matrix the store reads from a block (row ``i`` at lane groups
    ``i * ceil(n / L) ...``)."""
    L, gpr = cfg.L, cfg.groups_per_row(n)
    if not blk.written[: k * gpr].all():
        raise ProgramError(f"the store reads a {k} x {n} block whose lane groups "
                           f"{np.flatnonzero(~blk.written[: k * gpr]).tolist()} were never written")
    re = blk.re[: k * gpr].reshape(k, gpr * L)[:, :n]
    im = blk.im[: k * gpr].reshape(k, gpr * L)[:, :n]
    return re.copy(), im.copy()


# --- the machine -------------------------------------------------------------------------------------


class Machine:
    """The architectural state of one job: register and scalar files, ``VL``, the queues."""

    def __init__(self, cfg: VecUnitConfig) -> None:
        self.cfg = cfg
        NG, NS, L, V = cfg.NG, cfg.NS, cfg.L, cfg.VLMAX
        self.vre = np.zeros((NG, L, V), np.int64)
        self.vim = np.zeros((NG, L, V), np.int64)
        self.vlen = np.zeros(NG, np.int64)  # elements written, from 0
        self.s = np.zeros((NS, L), np.int64)
        self.sset = np.zeros(NS, bool)
        self.vl = int(cfg.VLMAX)
        self.inq: list[deque] = [deque() for _ in range(cfg.NQI)]
        self.outq: list[deque] = [deque() for _ in range(cfg.NQO)]
        self.head: list[Block | None] = [None] * cfg.NQI
        self.tail: list[Block | None] = [None] * cfg.NQO
        #: run_job's hooks: the load program, run until a queue has a block; the store program, run
        #: after every push
        self.pull = lambda q: None
        self.pushed = lambda: None

    # -- reads ---------------------------------------------------------------------------------
    def _vread(self, g: int, sl: slice) -> tuple[np.ndarray, np.ndarray, Format]:
        if self.vlen[g] < sl.stop:
            raise ProgramError(f"v{g} is read at elements up to {sl.stop - 1}, but only "
                               f"{int(self.vlen[g])} were written")
        return self.vre[g][:, sl], self.vim[g][:, sl], self.cfg.group_formats[g]

    def _sread(self, j: int) -> tuple[np.ndarray, Format]:
        if not self.sset[j]:
            raise ProgramError(f"s{j} is read before it is written")
        return self.s[j][:, None], self.cfg.scalar_formats[j]

    def _vwrite(self, g: int, sl: slice, re, rf: Format, im, imf: Format) -> None:
        fd = self.cfg.group_formats[g]
        self.vre[g][:, sl] = fx.quantize(np.asarray(re), rf, fd)
        self.vim[g][:, sl] = fx.quantize(np.asarray(im), imf, fd)
        if sl.start <= self.vlen[g]:
            self.vlen[g] = max(int(self.vlen[g]), sl.stop)

    def _head(self, q: int) -> Block:
        blk = self.head[q]
        if blk is None:
            if not self.inq[q]:
                self.pull(q)
            if not self.inq[q]:
                raise ProgramError(f"input queue q{q} is read while empty")
            blk = self.head[q] = self.inq[q].popleft()
        return blk

    def _tail(self, q: int) -> Block:
        blk = self.tail[q]
        if blk is None:
            blk = self.tail[q] = Block.empty(self.cfg.Kmax * self.cfg.NGQ, self.cfg.L)
        return blk

    # -- one vector instruction over the elements sl -------------------------------------------
    def vector(self, x: Instr, sl: slice, acc: dict) -> None:
        cfg = self.cfg
        op, f = int(x.opcode), int(x.funct)
        if op == Opcode.QUEUE:
            idx = np.arange(sl.start, sl.stop) * int(x.imm2) + int(x.imm1)
            if f == QOp.VLSE:
                blk = self._head(x.rs1)
                if (idx >= len(blk.written)).any() or not blk.written[idx].all():
                    raise ProgramError(f"{x}: lane groups {idx.tolist()} of q{x.rs1}'s head block "
                                       "were not all loaded")
                fq = cfg.in_queue_formats[x.rs1]
                self._vwrite(x.rd, sl, blk.re[idx].T, fq, blk.im[idx].T, fq)
            else:  # VSSE
                re, im, fv = self._vread(x.rs1, sl)
                blk = self._tail(x.rd)
                if (idx >= len(blk.written)).any():
                    raise ProgramError(f"{x}: lane groups {idx.tolist()} are outside the block")
                fq = cfg.out_queue_formats[x.rd]
                blk.re[idx] = fx.quantize(re, fv, fq).T
                blk.im[idx] = fx.quantize(im, fv, fq).T
                blk.written[idx] = True
            return
        v = VOp(f)
        if v == VOp.MVI:
            n = sl.stop - sl.start
            imm = np.full((cfg.L, n), x.simm, np.int64)
            self._vwrite(x.rd, sl, imm, IMM_FORMAT, np.zeros_like(imm), IMM_FORMAT)
        elif v == VOp.MV:
            re, im, f1 = self._vread(x.rs1, sl)
            self._vwrite(x.rd, sl, re, f1, im, f1)
        elif v in (VOp.ADD, VOp.SUB):
            a_r, a_i, fa = self._vread(x.rs1, sl)
            b_r, b_i, fb = self._vread(x.rs2, sl)
            fn = fx.add if v == VOp.ADD else fx.sub
            (rr, fr), (ri, fi) = fn(a_r, fa, b_r, fb), fn(a_i, fa, b_i, fb)
            self._vwrite(x.rd, sl, rr, fr, ri, fi)
        elif v == VOp.DOTR:
            a_r, a_i, fa = self._vread(x.rs1, sl)
            b_r, b_i, fb = self._vread(x.rs2, sl)
            (pr, fp), (pi, _) = fx.mult(a_r, fa, b_r, fb), fx.mult(a_i, fa, b_i, fb)
            term, ft = fx.add(pr, fp, pi, fp)  # Re(conj(a) b), exact
            part = np.asarray(term, np.int64).sum(axis=1)  # over the elements: an integer sum
            prev = acc.get(x.rd)
            acc[x.rd] = (part if prev is None else prev[0] + part, fx.sum_format(ft, cfg.VLMAX))
        else:
            if v in (VOp.MACC_S, VOp.NMSAC_S, VOp.MADD_S):
                sv, fs = self._sread(x.rs1)
                a = (sv, np.zeros_like(sv), fs)
            else:
                a = self._vread(x.rs1, sl)
                if x.bcast:
                    a = (np.broadcast_to(a[0][:1], a[0].shape), np.broadcast_to(a[1][:1], a[1].shape),
                         a[2])
            c: tuple | None
            if v in (VOp.MADD, VOp.MADD_S):  # vd * a + vs2
                b, c = self._vread(x.rd, sl), self._vread(x.rs2, sl)
            else:  # a * vs2 (+- vd)
                b, c = self._vread(x.rs2, sl), self._vread(x.rd, sl) if v != VOp.MUL else None
            (pr, fpr), (pi, fpi) = _cmul(a, b, conj=bool(x.conj))
            if c is None:
                self._vwrite(x.rd, sl, pr, fpr, pi, fpi)
            else:
                fn = fx.sub if v in (VOp.NMSAC, VOp.NMSAC_S) else fx.add
                (rr, fr), (ri, fi) = fn(c[0], c[2], pr, fpr), fn(c[1], c[2], pi, fpi)
                self._vwrite(x.rd, sl, rr, fr, ri, fi)

    def deliver(self, acc: dict) -> None:
        """A pass's reductions, delivered at its end: each quantized once to its scalar."""
        for j, (s, fs) in acc.items():
            self.s[j] = fx.quantize(np.asarray(s).reshape(-1), fs, self.cfg.scalar_formats[j])
            self.sset[j] = True

    def scalar(self, x: Instr) -> None:
        cfg = self.cfg
        a, fa = self._sread(x.rs1)
        fd = cfg.scalar_formats[x.rd]
        if int(x.funct) == SOp.DIVZ:
            b, fb = self._sread(x.rs2)
            faw = Format(fa.W + int(cfg.g_div), fa.int_bits, True)
            q, fq = fx.div(fx.quantize(a, fa, faw), faw, b, fb)  # 0 where b == 0
            self.s[x.rd] = fx.quantize(q, fq, fd).reshape(-1)
        else:
            self.s[x.rd] = fx.quantize(a, fa, fd).reshape(-1)
        self.sset[x.rd] = True

    def control(self, x: Instr) -> None:
        f = int(x.funct)
        if f == COp.SETVL:
            self.vl = int(x.imm1)
        elif f == COp.QPOP:  # release the head block (taking it first if it was not taken)
            self._head(x.rs1)
            self.head[x.rs1] = None
        else:  # QPUSH: commit the tail block
            self.outq[x.rd].append(self._tail(x.rd))
            self.tail[x.rd] = None
            self.pushed()

    def run_pass(self, chain: list[Instr], mode: str) -> None:
        acc: dict = {}
        vl = self.vl
        if mode == "pass":
            for i in range(vl):
                for x in chain:
                    self.vector(x, slice(i, i + 1), acc)
        else:
            for x in chain:
                self.vector(x, slice(0, vl), acc)
        self.deliver(acc)


def _cmul(a, b, *, conj: bool):
    """``a * b`` (or ``conj(a) * b``) exactly: ``(re, fmt), (im, fmt)``."""
    ar, ai, fa = a
    br, bi, fb = b
    p_rr, fp = fx.mult(ar, fa, br, fb)
    p_ii, _ = fx.mult(ai, fa, bi, fb)
    p_ri, _ = fx.mult(ar, fa, bi, fb)
    p_ir, _ = fx.mult(ai, fa, br, fb)
    if conj:
        return fx.add(p_rr, fp, p_ii, fp), fx.sub(p_ri, fp, p_ir, fp)
    return fx.sub(p_rr, fp, p_ii, fp), fx.add(p_ri, fp, p_ir, fp)


# --- a job ------------------------------------------------------------------------------------------


def run_job(cfg: VecUnitConfig, load: list[Instr], execute: list[Instr], store: list[Instr],
            inputs: dict[int, list], *, tx_id: int = 0, mode: str = "pass",
            on_starve: Callable[[int, JobResult], tuple | None] | None = None) -> JobResult:
    """Run one job's three programs and return its status and outputs.

    ``inputs`` holds, per input port, the ``(re, im)`` stored-integer matrices that arrive on it, in
    order.  The programs run as the tasks do, each in its own order: the load program runs when the
    execute program reads an input queue that is empty (in a functional model a queue never fills),
    and the store program runs after every ``wf.qpush``, so ``result.outputs`` -- per output port,
    ``(VecResp fields, re, im)`` per ``wf.st`` -- grows while the job runs.  When the load program
    needs a burst on a port that has none left, ``on_starve(port, result)`` may supply it (a harness
    playing the unit's neighbour: the matrix it computes from what the unit emitted); ``None`` means
    there is none.

    An execute error stops the job at the instruction (``result.status``; the store program then
    takes only what was pushed).  A load or store instruction its task cannot run, or a read of
    something never written, is a :class:`ProgramError`."""
    if mode not in ("pass", "serial"):
        raise ValueError(f"mode must be 'pass' or 'serial', not {mode!r}")
    m = Machine(cfg)
    feed = {p: deque(v) for p, v in inputs.items()}
    result = JobResult(Status.OK, {p: [] for p in range(cfg.n_out_ports)})
    at = {"load": 0, "store": 0}

    def pull(q: int) -> None:
        while not m.inq[q] and at["load"] < len(load):
            x = load[at["load"]]
            at["load"] += 1
            if not load_status(x, cfg):
                raise ProgramError(f"the load task cannot run {x}: it stops")
            if not feed.get(x.rs1) and on_starve is not None:
                more = on_starve(x.rs1, result)
                if more is not None:
                    feed.setdefault(x.rs1, deque()).append(more)
            if not feed.get(x.rs1):
                raise ProgramError(f"{x}: no more input on port {x.rs1}")
            re, im = feed[x.rs1].popleft()
            re, im = np.asarray(re, np.int64), np.asarray(im, np.int64)
            if re.shape != (x.imm1, x.imm2):
                raise ProgramError(f"{x}: the burst on port {x.rs1} is {re.shape}")
            m.inq[x.rd].append(block_from_matrix(re, im, cfg))

    def pushed() -> None:
        while at["store"] < len(store):
            x = store[at["store"]]
            if not store_status(x, cfg):
                raise ProgramError(f"the store task cannot run {x}: it stops")
            if not m.outq[x.rs1]:
                return
            at["store"] += 1
            blk = m.outq[x.rs1].popleft()
            re, im = matrix_from_block(blk, x.imm1, x.imm2, cfg)
            resp = {"tx_id": int(tx_id), "tag": int(x.rs2), "k": int(x.imm1), "n": int(x.imm2)}
            result.outputs[x.rd].append((resp, re, im))

    m.pull, m.pushed = pull, pushed
    for chain in split_passes(execute, cfg.S):
        st = Status.OK
        for x in chain:
            st = instr_status(x, cfg)
            if st != Status.OK:
                break
        if st == Status.OK and chain[0].vector:
            if chain[-1].chain or not all(y.vector for y in chain):
                # the pass ran past S instructions or the program's end, or chained a non-vector
                st = Status.BAD_OP
            else:
                st = pass_status(chain, cfg)
        if st != Status.OK:
            result.status = int(st)
            break
        if chain[0].vector:
            m.run_pass(chain, mode)
        elif int(chain[0].opcode) == Opcode.SCALAR:
            m.scalar(chain[0])
        else:
            m.control(chain[0])
        result.executed += len(chain)
    if result.status != Status.OK:
        return result  # fatal: nothing more is taken or answered
    held = [f"q{q} (input)" for q, b in enumerate(m.head) if b is not None]
    held += [f"q{q} (output)" for q, b in enumerate(m.tail) if b is not None]
    left = [f"q{q}" for q, d in enumerate(m.inq) if d]
    if held or left or at["load"] < len(load):
        raise ProgramError(f"the job ends holding {held}, with blocks left in {left} and "
                           f"{len(load) - at['load']} load instructions not run: every block a job "
                           "loads is taken and released, every block it fills is pushed")
    pushed()
    if at["store"] < len(store) or any(m.outq):
        raise ProgramError("the store program and the pushed blocks do not match")
    return result
