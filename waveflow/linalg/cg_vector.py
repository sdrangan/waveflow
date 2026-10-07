"""cg_vector.py — the CG vector unit: fixed-point conjugate gradient's vector steps, multi-RHS.

:class:`CgVectorCore` runs the start and register steps 2–9 of :mod:`waveflow.linalg.cg` (the
column dots, the two guarded divisions, the updates of ``X``, ``R`` and ``P``), bit for bit like
:func:`~waveflow.linalg.cg.cg_init` and :func:`~waveflow.linalg.cg.vec_step`.  Its body is
``waveflow/build/cg_vector_task.h``.  The matrix multiply ``S = A·P`` of each iteration is not
here: a :class:`~waveflow.linalg.systolic.SystolicCore` job with ``nb = nit`` computes the ``nit``
matrices ``S`` from the ``nit`` matrices ``P`` this core writes.

The core
--------
One firing is one **job**, set by one :class:`CgVectorCmd` on ``cmd_in``: the iteration count
``nit`` and the dimensions ``k`` (rows: the unknowns) and ``n`` (columns: the right-hand sides).
The core reads ``B`` (``k × n``) from ``b_blk``, starts (``X = 0``, ``R = q_R(B)``,
``P = q_P(R)``) and writes ``P₀`` on ``p_blk``; then, for each of the ``nit`` iterations, it reads
``S`` from ``s_blk`` and writes the next ``P``, or, after the last, ``X`` on ``x_blk``.  Every block
holds a matrix as row-major lane groups of ``L`` complex values (:mod:`~waveflow.linalg.lanes`),
the systolic core's layout.  ``L`` columns are processed side by side, each with its own pair of
dividers.

The command is not checked by the core; :func:`cmd_status` says whether one is valid.  The
dimensions and the iteration count are run-time values up to the maxima ``Kmax``, ``Nmax`` and
``nitmax``; ``n`` is a multiple of ``L``.

The standalone unit
-------------------
:class:`CgVectorUnit` puts the core behind a receiver, a loader and a store, and speaks the framed
messages of :mod:`~waveflow.linalg.message` on ``s_in`` and ``s_out``.  A job is a ``START``
request (``k``, ``n``, ``nfollow`` = ``nit``; payload ``B``), answered with ``P₀``, then ``nit``
``STEP`` requests (the same ``k`` and ``n``, ``nfollow`` = the steps still to come; payload ``S``),
each answered with the next ``P``, the last with ``X``; replies keep the request's ``nfollow``.  A
request the unit cannot serve (:func:`message_status`) is answered with its status and no payload,
drained by its length, and leaves a job in progress as it was.

Formats
-------
The formats are one :class:`~waveflow.linalg.cg.CgFormats`, a plain field.  They reach the body
as one integer, the format id of :func:`cg_traits`, which also carries the exact types the body
needs: the widened dividend ``rzw_t`` and the dot accumulators ``dot_ps_t`` and ``dot_rz_t`` (sums
of ``Kmax`` terms).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar, NamedTuple

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList
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
from waveflow.hw.mem_stream import KernelTask
from waveflow.linalg import cg_cost
from waveflow.linalg import cost as _cost
from waveflow.linalg.build import LinalgParts
from waveflow.linalg.cg import CgFormats, accumulator_formats, cg_init, vec_step
from waveflow.linalg.formats import DEFAULT_LANE_BITS, Traits, mem_format
from waveflow.linalg.lanes import (
    DEFAULT_WORD_BITS,
    block_type,
    elems_per_word,
    from_words,
    nwords,
    to_words,
)
from waveflow.linalg.message import U16, LinalgHeader, Status, header_words
from waveflow.simulation.simobj import ProcessGen

#: The task body of the core, in ``waveflow/build/``.
CORE_BODY = "cg_vector_task.h"
#: The headers a design with the core copies from ``waveflow/build/``.
CORE_HEADERS = (CORE_BODY,)
#: The traits family of the core.
CORE_TRAITS = "wf_cg_traits"
#: Bits of the core's command stream: one :class:`CgVectorCmd` per word.
CMD_BITS = 64
#: The traits family of the unit's message side: registers and memory elements.
IO_TRAITS = "wf_cg_io_traits"
#: The task bodies of the standalone unit, in ``waveflow/build/``.
UNIT_HEADERS = (
    "cg_vector_rx_task.h",
    "cg_vector_load_task.h",
    "cg_vector_store_task.h",
)


class CgOp(IntEnum):
    """The operations of the unit's messages (``0`` is not one, so a zeroed header is refused)."""

    START = 1  # payload B: start a job of nfollow iterations
    STEP = 2  # payload S: one iteration


class CgVectorCmd(DataList):
    """One job of the core: ``nit`` iterations on a ``k × n`` system."""

    include_filename: ClassVar[str | None] = "wf_cg_vector_cmd.h"
    elements: ClassVar[dict] = {
        "nit": {"schema": U16, "description": "iterations (at least 1)"},
        "k": {"schema": U16, "description": "rows: the unknowns"},
        "n": {"schema": U16, "description": "columns: the right-hand sides"},
    }


def command(nit: int, k: int, n: int) -> CgVectorCmd:
    """A :class:`CgVectorCmd` with these fields."""
    c = CgVectorCmd()
    c.nit, c.k, c.n = int(nit), int(k), int(n)
    return c


def cg_traits(formats: CgFormats, Kmax: int) -> Traits:
    """The traits of a core with these formats: its registers and the exact types of the body."""
    f = formats
    acc = accumulator_formats(f, int(Kmax))
    return Traits(
        CORE_TRAITS,
        (
            ("b_t", f.B),
            ("s_t", f.S),
            ("p_t", f.P),
            ("x_t", f.X),
            ("r_t", f.R),
            ("rz_t", f.rz),
            ("ps_t", f.ps),
            ("alpha_t", f.alpha),
            ("beta_t", f.beta),
            ("rzw_t", acc["rzw"]),
            ("dot_ps_t", acc["dot_ps"]),
            ("dot_rz_t", acc["dot_rz"]),
        ),
    )


def cmd_status(
    nit: int, k: int, n: int, *, Kmax: int, Nmax: int, nitmax: int, L: int
) -> Status:
    """Whether a core built with these maxima can run a job: ``OK`` or ``BAD_DIMS``."""
    if not (1 <= nit <= nitmax and 1 <= k <= Kmax and 1 <= n <= Nmax) or n % L:
        return Status.BAD_DIMS
    return Status.OK


#: Constants of the rough cycle counts below, which are not calibrated.
_OVERHEAD = 10
_DIV_LATENCY = 40


def start_cycles(k: int, n: int, *, L: int) -> int:
    """Rough cycles of the start: a lane group per cycle."""
    return _OVERHEAD + k * (n // L)


def iter_cycles(k: int, n: int, *, L: int) -> int:
    """Rough cycles of one iteration: per column group, three passes over the rows and the two
    divisions."""
    return _OVERHEAD + (n // L) * (3 * k + 2 * _DIV_LATENCY)


def _check_signed(name: str, f: CgFormats) -> None:
    if not all(fmt.signed for fmt in f.registers().values()):
        raise ValueError(f"{name}: the CG formats must be signed")


@dataclass
class CgVectorCore(FreeRunMod):
    """The CG vector core (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_vector"
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    nitmax: HwParam[int] = 8
    L: HwParam[int] = 4
    sob_depth: HwParam[int] = 2
    formats: CgFormats | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.formats is None:
            raise ValueError(f"{self.name}: CgVectorCore needs its formats")
        _check_signed(self.name, self.formats)
        K, N, L = int(self.Kmax), int(self.Nmax), int(self.L)
        if L < 1 or L & (L - 1):
            raise ValueError(f"the lane count L = {L} is not a power of two")
        if N % L:
            raise ValueError(f"need L | Nmax (L={L}, Nmax={N})")
        if K < 1 or int(self.nitmax) < 1:
            raise ValueError("Kmax and nitmax must be at least 1")
        self.traits = cg_traits(self.formats, K)
        f = self.formats
        self.cmd_in = StreamIFSlave(
            name=f"{self.name}_cmd_in", sim=self.sim, bitwidth=CMD_BITS, has_tlast=False
        )
        groups = K * N // L
        self.b_blk = SobIFSlave(
            name=f"{self.name}_b_blk",
            sim=self.sim,
            element_type=block_type(f.B.W, groups, L),
        )
        self.s_blk = SobIFSlave(
            name=f"{self.name}_s_blk",
            sim=self.sim,
            element_type=block_type(f.S.W, groups, L),
        )
        self.p_blk = SobIFMaster(
            name=f"{self.name}_p_blk",
            sim=self.sim,
            element_type=block_type(f.P.W, groups, L),
        )
        self.x_blk = SobIFMaster(
            name=f"{self.name}_x_blk",
            sim=self.sim,
            element_type=block_type(f.X.W, groups, L),
        )
        for ep in (self.cmd_in, self.b_blk, self.s_blk, self.p_blk, self.x_blk):
            self.add_endpoint(ep)

    def status(self, nit: int, k: int, n: int) -> Status:
        """:func:`cmd_status` for this core."""
        return cmd_status(
            nit,
            k,
            n,
            Kmax=int(self.Kmax),
            Nmax=int(self.Nmax),
            nitmax=int(self.nitmax),
            L=int(self.L),
        )

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_vector_task",
            CORE_BODY,
            ("cmd_in", "b_blk", "s_blk", "p_blk", "x_blk"),
            template_args=(
                int(self.Kmax),
                int(self.Nmax),
                int(self.nitmax),
                int(self.L),
                int(self.sob_depth),
                self.traits.id,
            ),
        )

    def linalg_parts(self) -> LinalgParts:
        """What a design with this core needs generated: its traits, body and command header."""
        return LinalgParts((self.traits,), CORE_HEADERS, (CgVectorCmd,))

    def resource_structure(self):
        return cg_cost.core_structure(self)

    @classmethod
    def get_rm(cls, platform):
        return _cost.resource_model("cg_vector_task", cls, platform)

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.cmd_in.get_schema(CgVectorCmd)
        nit, k, n = int(cmd.nit), int(cmd.k), int(cmd.n)
        st = self.status(nit, k, n)
        if st != Status.OK:
            raise RuntimeError(f"{self.name}: a command it cannot run ({st.name})")
        f, L, period = self.formats, int(self.L), self.clk.period
        blk = yield from self.b_blk.acquire_read()
        br, bi = blk.payload
        yield from self.b_blk.release_read()
        if br.shape != (k, n):
            raise RuntimeError(f"{self.name}: B is {br.shape}, the command says {k, n}")
        state = cg_init(br, bi, f)
        coef = (
            cg_cost.message_model()
        )  # the calibrated share of each message, else rough counts
        if coef is None:
            yield self.timeout(start_cycles(k, n, L=L) * period)
        else:
            share = cg_cost.core_interval(coef, CgOp.START, k, n, L=L, formats=f)
            yield self.timeout(share * period)
        yield from self._write(self.p_blk, state.pr, state.pi)
        for it in range(1, nit + 1):
            blk = yield from self.s_blk.acquire_read()
            sr, si = blk.payload
            yield from self.s_blk.release_read()
            if sr.shape != (k, n):
                raise RuntimeError(
                    f"{self.name}: S is {sr.shape}, the command says {k, n}"
                )
            state, _ = vec_step(state, sr, si, f)
            if coef is None:
                yield self.timeout(iter_cycles(k, n, L=L) * period)
            else:
                share = cg_cost.core_interval(coef, CgOp.STEP, k, n, L=L, formats=f)
                yield self.timeout(share * period)
            if it < nit:
                yield from self._write(self.p_blk, state.pr, state.pi)
            else:
                yield from self._write(self.x_blk, state.xr, state.xi)

    def _write(self, ep, re, im) -> ProcessGen[None]:
        out = yield from ep.acquire_write()
        out.payload = (re, im)
        yield from ep.commit_write(out)


# --- the standalone unit -------------------------------------------------------------------------


def io_traits(formats: CgFormats, lane_bits: int = DEFAULT_LANE_BITS) -> Traits:
    """The traits of the unit's message side: the registers and their memory elements."""
    f = formats
    return Traits(
        IO_TRAITS,
        (("b_t", f.B), ("s_t", f.S), ("p_t", f.P), ("x_t", f.X)),
        (("b_mem", f.B), ("s_mem", f.S), ("p_mem", f.P), ("x_mem", f.X)),
        int(lane_bits),
    )


def message_words(
    k: int,
    n: int,
    lane_bits: int = DEFAULT_LANE_BITS,
    word_bits: int = DEFAULT_WORD_BITS,
) -> int:
    """Payload words of a ``k × n`` matrix: every request's, and every served reply's."""
    return nwords(int(k) * int(n), lane_bits, word_bits)


class Job(NamedTuple):
    """A job in progress at the receiver: its dimensions and the steps still to come."""

    k: int
    n: int
    left: int


def message_status(
    h: LinalgHeader,
    job: Job | None,
    *,
    Kmax: int,
    Nmax: int,
    nitmax: int,
    L: int,
    lane_bits: int = DEFAULT_LANE_BITS,
    word_bits: int = DEFAULT_WORD_BITS,
) -> Status:
    """The status the unit answers a request with, given the job in progress
    (``cg_vector_rx_task.h``)."""
    op, k, n, nf = int(h.op), int(h.k), int(h.n), int(h.nfollow)
    if op not in (CgOp.START, CgOp.STEP):
        return Status.BAD_OP
    if op == CgOp.START:
        if job is not None:
            return Status.BAD_SEQUENCE
        dims = {"Kmax": Kmax, "Nmax": Nmax, "nitmax": nitmax, "L": L}
        if cmd_status(nf, k, n, **dims) != Status.OK:
            return Status.BAD_DIMS
    elif job is None or (k, n, nf) != (job.k, job.n, job.left - 1):
        return Status.BAD_SEQUENCE
    if int(h.length) != message_words(k, n, lane_bits, word_bits):
        return Status.BAD_LENGTH
    return Status.OK


def reply_header(request: LinalgHeader, status: int, length: int) -> LinalgHeader:
    """The reply header to a request: the request's header with the status and the reply's length
    (``nfollow`` is kept, so the job's last reply has ``nfollow = 0``)."""
    r = LinalgHeader()
    for name in ("tag", "op", "nfollow", "m", "k", "n"):
        setattr(r, name, int(getattr(request, name)))
    r.status, r.length = int(status), int(length)
    return r


@dataclass
class _UnitPart(FreeRunMod):
    """The parameters every task of the unit shares."""

    word_bits: HwParam[int] = DEFAULT_WORD_BITS
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    nitmax: HwParam[int] = 8
    L: HwParam[int] = 4
    sob_depth: HwParam[int] = 2
    lane_bits: HwParam[int] = DEFAULT_LANE_BITS
    formats: CgFormats | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.formats is None:
            raise ValueError(f"{self.name}: the unit needs its formats")
        _check_signed(self.name, self.formats)
        elems_per_word(int(self.lane_bits), int(self.word_bits))  # a supported pair
        f = self.formats
        for fmt in (f.B, f.S, f.P, f.X):
            mem_format(fmt, int(self.lane_bits))  # each operand fits a lane
        self.core_traits = cg_traits(f, int(self.Kmax))
        self.io_traits = io_traits(f, int(self.lane_bits))

    def _framed(self, cls, name: str):
        ep = cls(
            name=f"{self.name}_{name}",
            sim=self.sim,
            bitwidth=int(self.word_bits),
            has_tlast=True,
        )
        self.add_endpoint(ep)
        return ep

    def _cmd(self, cls, name: str):
        ep = cls(
            name=f"{self.name}_{name}", sim=self.sim, bitwidth=CMD_BITS, has_tlast=False
        )
        self.add_endpoint(ep)
        return ep

    def _block(self, cls, name: str, W: int):
        L = int(self.L)
        groups = int(self.Kmax) * int(self.Nmax) // L
        ep = cls(
            name=f"{self.name}_{name}",
            sim=self.sim,
            element_type=block_type(W, groups, L),
        )
        self.add_endpoint(ep)
        return ep

    def _maxima(self) -> dict:
        return {
            "Kmax": int(self.Kmax),
            "Nmax": int(self.Nmax),
            "nitmax": int(self.nitmax),
            "L": int(self.L),
        }

    def linalg_parts(self) -> LinalgParts:
        return LinalgParts(
            (self.core_traits, self.io_traits),
            (*CORE_HEADERS, *UNIT_HEADERS),
            (CgVectorCmd,),
        )


@dataclass
class CgVectorRx(_UnitPart):
    """The unit's receiver: validates each request against the job in progress, routes the job,
    forwards or drains the payload (``cg_vector_rx_task.h``).  One firing is one request outside a
    job, or one whole job with the requests rejected inside it."""

    cpp_kernel_name: ClassVar[str | None] = "cg_vector_rx"

    def resource_structure(self):
        return cg_cost.rx_structure(self)

    @classmethod
    def get_rm(cls, platform):
        return _cost.resource_model("cg_vector_rx_task", cls, platform)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = self._framed(StreamIFSlave, "s_in")
        self.load_cmd = self._cmd(StreamIFMaster, "load_cmd")
        self.core_cmd = self._cmd(StreamIFMaster, "core_cmd")
        self.store_cmd = self._framed(StreamIFMaster, "store_cmd")
        self.s_pay = self._framed(StreamIFMaster, "s_pay")

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_vector_rx_task",
            UNIT_HEADERS[0],
            ("s_in", "load_cmd", "core_cmd", "store_cmd", "s_pay"),
            template_args=(
                int(self.word_bits),
                int(self.Kmax),
                int(self.Nmax),
                int(self.nitmax),
                int(self.L),
                self.io_traits.id,
            ),
        )

    def status(self, h: LinalgHeader, job: Job | None) -> Status:
        return message_status(
            h,
            job,
            **self._maxima(),
            lane_bits=int(self.lane_bits),
            word_bits=int(self.word_bits),
        )

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.word_bits)
        job: Job | None = None
        while True:
            h = yield from self.s_in.get_schema(LinalgHeader)
            st = self.status(h, job)
            k, n, nf = int(h.k), int(h.n), int(h.nfollow)
            words = (
                message_words(k, n, int(self.lane_bits), w) if st == Status.OK else 0
            )
            r = reply_header(h, st, words)
            yield from self.store_cmd.write(
                np.asarray(r.serialize(word_bw=w), np.uint64)
            )
            if st == Status.OK:
                if int(h.op) == CgOp.START:
                    cmd = np.asarray(
                        command(nf, k, n).serialize(word_bw=CMD_BITS), np.uint64
                    )
                    yield from self.load_cmd.write(cmd)
                    yield from self.core_cmd.write(cmd)
                    job = Job(k, n, nf)
                else:
                    job = Job(k, n, job.left - 1) if job.left > 1 else None
            done = 0
            while done < int(h.length):  # forward, or drain, the payload's bursts
                burst = yield from self.s_in.get()
                done += len(burst)
                if st == Status.OK:
                    yield from self.s_pay.write(np.asarray(burst))
                else:
                    yield self.timeout(len(burst) * self.clk.period)
            if job is None:
                return


@dataclass
class CgVectorLoad(_UnitPart):
    """The unit's loader: lands a job's ``B`` and its ``nit`` matrices ``S`` in the core's blocks
    (``cg_vector_load_task.h``)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_vector_load"

    def resource_structure(self):
        return cg_cost.load_structure(self)

    @classmethod
    def get_rm(cls, platform):
        return _cost.resource_model("cg_vector_load_task", cls, platform)

    def __post_init__(self) -> None:
        super().__post_init__()
        f = self.formats
        self.cmd_in = self._cmd(StreamIFSlave, "cmd_in")
        self.s_in = self._framed(StreamIFSlave, "s_in")
        self.b_blk = self._block(SobIFMaster, "b_blk", f.B.W)
        self.s_blk = self._block(SobIFMaster, "s_blk", f.S.W)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_vector_load_task",
            UNIT_HEADERS[1],
            ("cmd_in", "s_in", "b_blk", "s_blk"),
            template_args=(
                int(self.word_bits),
                int(self.Kmax),
                int(self.Nmax),
                int(self.nitmax),
                int(self.L),
                int(self.sob_depth),
                self.core_traits.id,
                self.io_traits.id,
            ),
        )

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.cmd_in.get_schema(CgVectorCmd)
        nit, k, n = int(cmd.nit), int(cmd.k), int(cmd.n)
        f, lb, wb = self.formats, int(self.lane_bits), int(self.word_bits)
        for ep, fmt in [(self.b_blk, f.B)] + [(self.s_blk, f.S)] * nit:
            words = yield from self.s_in.get()
            re, im = from_words(words, k * n, fmt, lb, wb)
            blk = yield from ep.acquire_write()
            blk.payload = (re.reshape(k, n), im.reshape(k, n))
            yield from ep.commit_write(blk)


@dataclass
class CgVectorStore(_UnitPart):
    """The unit's store: the reply header, then the core's next ``P``, or ``X`` for the job's last
    step, for a served request (``cg_vector_store_task.h``)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_vector_store"

    def resource_structure(self):
        return cg_cost.store_structure(self)

    @classmethod
    def get_rm(cls, platform):
        return _cost.resource_model("cg_vector_store_task", cls, platform)

    def __post_init__(self) -> None:
        super().__post_init__()
        f = self.formats
        self.cmd_in = self._framed(StreamIFSlave, "cmd_in")
        self.p_blk = self._block(SobIFSlave, "p_blk", f.P.W)
        self.x_blk = self._block(SobIFSlave, "x_blk", f.X.W)
        self.s_out = self._framed(StreamIFMaster, "s_out")

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_vector_store_task",
            UNIT_HEADERS[2],
            ("cmd_in", "p_blk", "x_blk", "s_out"),
            template_args=(
                int(self.word_bits),
                int(self.Kmax),
                int(self.Nmax),
                int(self.L),
                int(self.sob_depth),
                self.core_traits.id,
                self.io_traits.id,
            ),
        )

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.word_bits)
        r = yield from self.cmd_in.get_schema(LinalgHeader)
        yield from self.s_out.write(np.asarray(r.serialize(word_bw=w), np.uint64))
        if int(r.status) != Status.OK:
            return
        f = self.formats
        if int(r.op) == CgOp.START or int(r.nfollow):
            ep, fmt = self.p_blk, f.P
        else:
            ep, fmt = self.x_blk, f.X
        blk = yield from ep.acquire_read()
        re, im = blk.payload
        yield from self.s_out.write(to_words(re, im, fmt, int(self.lane_bits), w))
        yield from ep.release_read()


@dataclass
class CgVectorUnit(FreeRunMod):
    """The standalone CG vector unit: framed requests in on ``s_in``, replies out on ``s_out``
    (see the module doc).  Tasks: :class:`CgVectorRx`, :class:`CgVectorLoad`,
    :class:`CgVectorCore`, :class:`CgVectorStore`."""

    cpp_kernel_name: ClassVar[str | None] = "cg_vector_unit"
    word_bits: HwParam[int] = DEFAULT_WORD_BITS
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    nitmax: HwParam[int] = 8
    L: HwParam[int] = 4
    sob_depth: HwParam[int] = 2
    lane_bits: HwParam[int] = DEFAULT_LANE_BITS
    formats: CgFormats | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        shared = {
            "sim": self.sim,
            "clk": self.clk,
            "Kmax": int(self.Kmax),
            "Nmax": int(self.Nmax),
            "nitmax": int(self.nitmax),
            "L": int(self.L),
            "sob_depth": int(self.sob_depth),
            "formats": self.formats,
        }
        io = {
            **shared,
            "word_bits": int(self.word_bits),
            "lane_bits": int(self.lane_bits),
        }
        self.rx = CgVectorRx(name=f"{self.name}_rx", **io)
        self.load = CgVectorLoad(name=f"{self.name}_load", **io)
        self.core = CgVectorCore(name=f"{self.name}_core", **shared)
        self.store = CgVectorStore(name=f"{self.name}_store", **io)
        for comp in (self.rx, self.load, self.core, self.store):
            self.add_comp(comp)
        w = int(self.word_bits)
        self._stream("pay", self.rx.s_pay, self.load.s_in, w, framed=True)
        self._stream("reply", self.rx.store_cmd, self.store.cmd_in, w, framed=True)
        self._stream("lcmd", self.rx.load_cmd, self.load.cmd_in, CMD_BITS)
        self._stream("ccmd", self.rx.core_cmd, self.core.cmd_in, CMD_BITS)
        self._blocks("b", self.load.b_blk, self.core.b_blk)
        self._blocks("s", self.load.s_blk, self.core.s_blk)
        self._blocks("p", self.core.p_blk, self.store.p_blk)
        self._blocks("x", self.core.x_blk, self.store.x_blk)
        self.boundary = ["s_in", "s_out"]
        self.s_in = self.rx.s_in
        self.s_out = self.store.s_out
        self.extra_includes = ["hls_streamofblocks.h"]

    def _stream(self, name, master, slave, bits: int, framed: bool = False) -> None:
        kw = {"framed": True} if framed else {}
        iface = StreamIF(
            name=f"{self.name}_{name}_if",
            sim=self.sim,
            clk=self.clk,
            bitwidth=bits,
            **kw,
        )
        iface.bind("master", master)
        iface.bind("slave", slave)
        self.add_if(iface)

    def _blocks(self, name, master, slave) -> None:
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

    @property
    def header_words(self) -> int:
        return header_words(int(self.word_bits))

    @classmethod
    def get_rm(cls, platform):
        """The unit's own share, its channels (:class:`~waveflow.linalg.cg_cost.CgUnitResourceModel`);
        each task has its own model."""
        return cg_cost.unit_model(platform)
