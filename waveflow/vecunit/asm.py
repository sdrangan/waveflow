"""asm.py — the vector unit's assembler: checks a job's three programs and serializes them.

A program is a list of :class:`~waveflow.vecunit.isa.Instr` (built with the constructors in
:mod:`waveflow.vecunit.isa`).  :func:`check` returns every problem, and :func:`assemble` refuses a
job with any:

* **what the hardware reports** -- an instruction the execute task would answer with a status
  (:func:`~waveflow.vecunit.model.instr_status`), a pass that breaks a pass rule
  (:func:`~waveflow.vecunit.model.pass_status`: ``S`` slots and their capabilities, one group read
  and one written per bank, one access per queue, no reduction read in its own pass), a chain left
  open, a load or store instruction its task cannot run;
* **what the hardware would get wrong without a word** -- a vector group read at elements no
  instruction wrote, a scalar read before it is written, a ``wf.vlse`` past the loaded rows or with a
  stride that is not the block's layout (``ceil(n / L)`` lane groups a row), a block pushed with lane
  groups never written, or one whose store instruction reads it at another shape;
* **queue balance** between the programs: per input queue, as many ``wf.qpop`` as ``wf.ld``; per
  output queue, as many ``wf.qpush`` as ``wf.st``; no block held when the job ends.

Each program serializes as one framed burst: a :class:`~waveflow.vecunit.isa.VecJob` word, then its
instruction words.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.linalg.message import Status
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.isa import COp, Instr, Opcode, VecJob
from waveflow.vecunit.model import (
    instr_status,
    load_status,
    operands,
    pass_status,
    split_passes,
    store_status,
)

#: The program a word belongs to.
PROGRAMS = ("load", "execute", "store")


@dataclass
class Programs:
    """A job's three programs."""

    load: list[Instr] = field(default_factory=list)
    execute: list[Instr] = field(default_factory=list)
    store: list[Instr] = field(default_factory=list)

    def __iter__(self):
        return iter((self.load, self.execute, self.store))


class AsmError(ValueError):
    """A job the assembler refuses; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems[:8]) + (" ..." if len(self.problems) > 8 else ""))


def check(cfg: VecUnitConfig, progs: Programs) -> list[str]:
    """Every problem with a job's programs (see the module doc); empty if it may run."""
    out: list[str] = []
    in_blocks: list[list[tuple[int, int]]] = [[] for _ in range(cfg.NQI)]
    for j, x in enumerate(progs.load):
        if not load_status(x, cfg):
            out.append(f"load[{j}] {x}: not a load instruction the load task can run")
            continue
        in_blocks[x.rd].append((int(x.imm1), int(x.imm2)))
    out_blocks: list[list[tuple[int, int]]] = [[] for _ in range(cfg.NQO)]
    for j, x in enumerate(progs.store):
        if not store_status(x, cfg):
            out.append(f"store[{j}] {x}: not a store instruction the store task can run")
            continue
        out_blocks[x.rs1].append((int(x.imm1), int(x.imm2)))
    out += _check_execute(cfg, progs.execute, in_blocks, out_blocks)
    for name, prog in zip(PROGRAMS, progs):
        if len(prog) >= 1 << 16:
            out.append(f"the {name} program has {len(prog)} instructions; a VecJob counts 65,535")
    return out


def _check_execute(cfg, prog, in_blocks, out_blocks) -> list[str]:
    out: list[str] = []
    vlen = [0] * cfg.NG  # elements written, from 0
    sset = [False] * cfg.NS
    vl = cfg.VLMAX
    in_next = [0] * cfg.NQI  # the next block each input queue's head will be
    head: list[tuple[int, int] | None] = [None] * cfg.NQI
    out_next = [0] * cfg.NQO
    tail: list[set | None] = [None] * cfg.NQO  # (row, group) pairs written
    tail_dims: list[tuple[int, int] | None] = [None] * cfg.NQO
    pc = 0

    def take_head(q: int, x: Instr) -> tuple[int, int] | None:
        if head[q] is None:
            if in_next[q] >= len(in_blocks[q]):
                out.append(f"execute: {x}: input queue qi{q} has no more blocks (the load program "
                           f"loads {len(in_blocks[q])})")
                return None
            head[q] = in_blocks[q][in_next[q]]
            in_next[q] += 1
        return head[q]

    def take_tail(q: int, x: Instr) -> tuple[int, int] | None:
        if tail[q] is None:
            if out_next[q] >= len(out_blocks[q]):
                out.append(f"execute: {x}: output queue qo{q} fills more blocks than the store program "
                           f"takes ({len(out_blocks[q])})")
                return None
            tail[q] = set()
            tail_dims[q] = out_blocks[q][out_next[q]]
            out_next[q] += 1
        return tail_dims[q]

    for chain in split_passes(prog, cfg.S):
        at = pc  # formatted only for a message (a disassembly is not free: Stage 10)
        pc += len(chain)
        bad = [(x, instr_status(x, cfg)) for x in chain if instr_status(x, cfg) != Status.OK]
        if bad:
            out += [f"execute: {x}: {Status(st).name}" for x, st in bad]
            continue
        if chain[0].vector:
            if chain[-1].chain:
                out.append(f"execute[{at}] {chain[0]}: the chain is left open (it ends the program or runs past "
                           f"S = {cfg.S} instructions)")
                continue
            if not all(y.vector for y in chain):
                out.append(f"execute[{at}] {chain[0]}: a chain may hold only vector and queue instructions")
                continue
            if pass_status(chain, cfg) != Status.OK:
                out.append(f"execute[{at}] {chain[0]}: the pass breaks a pass rule (slots and capabilities, one "
                           "group read and written per bank, one access per queue, no reduction "
                           "read in its own pass)")
                continue
            written_here: set[int] = set()
            for x in chain:
                o = operands(x)
                for g in o.vreads:
                    if g not in written_here and vlen[g] < vl:
                        out.append(f"execute: {x} reads v{g} at {vl} elements; {vlen[g]} were "
                                   "written")
                for j in o.sreads:
                    if not sset[j]:
                        out.append(f"execute: {x} reads s{j} before it is written")
                if o.qread is not None:
                    dims = take_head(o.qread, x)
                    if dims is not None:
                        k, n = dims
                        gpr = cfg.groups_per_row(n)
                        if int(x.imm2) != gpr or int(x.imm1) >= gpr or vl > k:
                            out.append(f"execute: {x} reads a {k} x {n} block: the stride is "
                                       f"{gpr}, the column groups 0..{gpr - 1}, VL <= {k}")
                if o.qwrite is not None:
                    dims = take_tail(o.qwrite, x)
                    if dims is not None:
                        k, n = dims
                        gpr = cfg.groups_per_row(n)
                        if int(x.imm2) != gpr or int(x.imm1) >= gpr:
                            out.append(f"execute: {x} writes a {k} x {n} block (as the store "
                                       f"takes it): the stride is {gpr}, the column groups "
                                       f"0..{gpr - 1}")
                        cover = tail[o.qwrite]
                        assert cover is not None  # take_tail made it
                        cover.update((i, int(x.imm1)) for i in range(vl))
                if o.vwrite is not None:
                    written_here.add(o.vwrite)
                    vlen[o.vwrite] = max(vlen[o.vwrite], vl)
            for x in chain:  # reductions deliver at the end of the pass
                o = operands(x)
                if o.swrite is not None:
                    sset[o.swrite] = True
            continue
        x = chain[0]
        op, f = int(x.opcode), int(x.funct)
        if op == Opcode.SCALAR:
            for j in operands(x).sreads:
                if not sset[j]:
                    out.append(f"execute: {x} reads s{j} before it is written")
            sset[x.rd] = True
        elif op == Opcode.CTRL and f == COp.SETVL:
            vl = int(x.imm1)
        elif op == Opcode.CTRL and f == COp.QPOP:
            take_head(x.rs1, x)
            head[x.rs1] = None
        elif op == Opcode.CTRL and f == COp.QPUSH:
            dims = take_tail(x.rd, x)
            if dims is not None:
                k, n = dims
                need = {(i, g) for i in range(k) for g in range(cfg.groups_per_row(n))}
                missing = sorted(need - (tail[x.rd] or set()))
                if missing:
                    out.append(f"execute: {x} pushes a {k} x {n} block with (row, column group) "
                               f"{missing[:6]}{' ...' if len(missing) > 6 else ''} never written")
            tail[x.rd] = None
    for q in range(cfg.NQI):
        if head[q] is not None:
            out.append(f"the job ends holding input queue qi{q}'s head block (no wf.qpop)")
        if in_next[q] != len(in_blocks[q]):
            out.append(f"input queue qi{q}: the load program loads {len(in_blocks[q])} blocks, the "
                       f"execute program takes {in_next[q]}")
    for q in range(cfg.NQO):
        if tail[q] is not None:
            out.append(f"the job ends holding output queue qo{q}'s tail block (no wf.qpush)")
        if out_next[q] != len(out_blocks[q]):
            out.append(f"output queue qo{q}: the execute program pushes {out_next[q]} blocks, the "
                       f"store program takes {len(out_blocks[q])}")
    return out


def _job_head(tx_id: int, ninstr: int) -> np.ndarray:
    job = VecJob()
    job.tx_id, job.ninstr = int(tx_id), int(ninstr)
    return np.asarray(job.serialize(word_bw=64), np.uint64)


def job_words(tx_id: int, prog: list[Instr]) -> np.ndarray:
    """One program as its burst: the ``VecJob(tx_id, ninstr)`` word, then the instruction words."""
    body = np.asarray([x.encode() for x in prog], np.uint64)
    return np.concatenate([_job_head(tx_id, len(prog)), body])


def assemble(cfg: VecUnitConfig, progs: Programs, tx_id: int,
             cache: dict | None = None) -> dict[str, np.ndarray]:
    """``{"load": words, "execute": words, "store": words}`` for one job; raises :class:`AsmError`
    on any problem :func:`check` finds.  *cache*: a dict a caller passes across its jobs, so a
    program its jobs share is checked and encoded once (Stage 10)."""
    key = (id(cfg), *(tuple(prog) for prog in progs))
    hit = cache.get(key) if cache is not None else None
    if hit is None:
        problems = check(cfg, progs)
        if problems:
            raise AsmError(problems)
        hit = (cfg, [np.asarray([x.encode() for x in prog], np.uint64) for prog in progs])
        if cache is not None:
            cache[key] = hit  # the configuration kept, so its identity is not reused
    return {name: np.concatenate([_job_head(tx_id, len(body)), body])
            for name, body in zip(PROGRAMS, hit[1])}


def disassemble_words(words) -> list[str]:
    """A program burst back to text: the job word, then one line per instruction."""
    from waveflow.vecunit.isa import disassemble

    words = [int(w) for w in np.asarray(words, np.uint64)]
    tx_id, n = words[0] & 0xFFFF, (words[0] >> 16) & 0xFFFF
    return [f"# job tx_id={tx_id} ninstr={n}"] + [disassemble(Instr.decode(w)) for w in words[1:]]
