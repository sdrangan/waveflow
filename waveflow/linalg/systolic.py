"""systolic.py — the systolic matrix multiply: its core, command and formats.

:class:`SystolicCore` computes ``C = q_c(A·B)`` or ``C = q_c(Aᴴ·B)`` on complex fixed-point
matrices, as an output-stationary array of ``R × C`` processing elements, bit for bit like
:func:`~waveflow.linalg.matmul.matmul`.  Its body is ``waveflow/build/systolic_core_task.h``.

The core
--------
One firing is one **job**, set by one :class:`SystolicCmd` on ``cmd_in``: the operation, the
dimensions ``m``, ``k``, ``n`` and the number ``nb`` of ``B`` matrices.  The core reads an
``m × k`` matrix ``X`` from ``a_blk``, then for each of the ``nb`` matrices ``B`` on ``b_blk``
writes one ``C`` on ``c_blk``; ``X`` is held for the whole job.  For ``MUL``, ``X = A``.  For
``MUL_AH``, ``X = Aᵀ``, transposed (not conjugated) by whoever wrote the block, and the core
computes ``Aᴴ·B = conj(X·conj(B))``.  Every block holds a matrix as row-major lane groups of
``L`` complex values (:mod:`~waveflow.linalg.lanes`).

``C`` (``m × n``) is covered in ``(m/R)·(n/C)`` tiles.  ``X`` values shift right along the rows of
the array and ``B`` values down its columns, with the usual skew; each element accumulates its
entry of ``C`` exactly over ``k`` and the tile is rounded once.  ``form`` picks the complex
product: four real multiplies, or three (the Gauss form,
:func:`~waveflow.utils.complexutils.cmult3`).  For ``MUL_AH`` the exact imaginary sum is negated
before the rounding, and ``conj(B)`` is taken as ``B`` enters the array (its imaginary part
negated in one more bit, so ``-2^(W-1)`` is exact).

The command is not checked by the core; :func:`cmd_status` says whether one is valid.  The
dimensions are run-time values up to the maxima ``Mmax``, ``Kmax``, ``Nmax``; ``m`` is a
multiple of ``R``, ``n`` of ``C``, and ``k`` is a multiple of ``L`` or divides it.

Formats
-------
The formats of ``A``, ``B`` and ``C`` are plain fields.  They reach the body as one integer, the
format id of :meth:`SystolicCore.traits`, which also carries the exact types the array needs:
``ba_t`` (``B`` in the array, one bit wider for the negation), ``p_t`` (one complex product)
and ``acc_t`` (a sum of ``Kmax`` products).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

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
from waveflow.linalg import matmul as mm
from waveflow.linalg.build import LinalgParts
from waveflow.linalg.formats import DEFAULT_LANE_BITS, Traits
from waveflow.linalg.lanes import (
    DEFAULT_WORD_BITS,
    block_type,
    from_words,
    n_groups,
    nwords,
    to_words,
)
from waveflow.linalg.message import U8, U16, LinalgHeader, Status, header_words, reply
from waveflow.simulation.simobj import ProcessGen
from waveflow.utils import complexutils as cx
from waveflow.utils.fixputils import Format

#: The task body of the core, in ``waveflow/build/``.
CORE_BODY = "systolic_core_task.h"
#: The headers a design with the core copies from ``waveflow/build/``.
CORE_HEADERS = (CORE_BODY, "complex_utils.hpp", "wf_cint.h")
#: The traits family of the core.
CORE_TRAITS = "wf_systolic_traits"
#: The traits family of the unit's message side: registers and memory elements.
IO_TRAITS = "wf_systolic_io_traits"
#: The task bodies of the standalone unit, in ``waveflow/build/``.
UNIT_HEADERS = ("systolic_rx_task.h", "systolic_load_task.h", "systolic_store_task.h")
#: Bits of the core's command stream: one :class:`SystolicCmd` per word.
CMD_BITS = 64
FORMS = mm.FORMS


class MatmulOp(IntEnum):
    """The operations of the matrix multiply (``0`` is not one, so a zeroed header is refused)."""

    MUL = 1  # C = q(A·B)
    MUL_AH = 2  # C = q(Aᴴ·B)


class SystolicCmd(DataList):
    """One job of the core: one ``A``, ``nb`` matrices ``B``, as many ``C``."""

    include_filename: ClassVar[str | None] = "wf_systolic_cmd.h"
    elements: ClassVar[dict] = {
        "op": {"schema": U8, "description": "a MatmulOp"},
        "nb": {"schema": U8, "description": "matrices B in the job (at least 1)"},
        "m": {"schema": U16, "description": "rows of C"},
        "k": {"schema": U16, "description": "the inner dimension"},
        "n": {"schema": U16, "description": "columns of B and C"},
    }


def command(op: int, m: int, k: int, n: int, nb: int = 1) -> SystolicCmd:
    """A :class:`SystolicCmd` with these fields."""
    c = SystolicCmd()
    c.op, c.nb, c.m, c.k, c.n = int(op), int(nb), int(m), int(k), int(n)
    return c


def core_traits(a: Format, b: Format, c: Format, Kmax: int) -> Traits:
    """The traits of a core with these formats: the registers and the exact types of the array."""
    ba = cx.conj_format(b)
    return Traits(
        CORE_TRAITS,
        (
            ("a_t", a),
            ("b_t", b),
            ("c_t", c),
            ("ba_t", ba),
            ("p_t", cx.cmult_format(a, ba)),
            ("acc_t", mm.acc_format(a, ba, Kmax)),
        ),
    )


def stored_shape(op: int, m: int, k: int) -> tuple[int, int]:
    """The shape of ``A`` as a job states it: ``m × k``, or ``k × m`` for ``Aᴴ``.  (The core's
    block always holds ``m × k``: ``A``, or ``Aᵀ``.)"""
    return (int(k), int(m)) if int(op) == MatmulOp.MUL_AH else (int(m), int(k))


def cmd_status(
    op: int,
    nb: int,
    m: int,
    k: int,
    n: int,
    *,
    Mmax: int,
    Kmax: int,
    Nmax: int,
    L: int,
    R: int,
    C: int,
) -> Status:
    """Whether a core built with these maxima and this array can run a job: ``OK``, ``BAD_OP``
    or ``BAD_DIMS``."""
    if int(op) not in (MatmulOp.MUL, MatmulOp.MUL_AH):
        return Status.BAD_OP
    if not (1 <= nb and 1 <= m <= Mmax and 1 <= k <= Kmax and 1 <= n <= Nmax):
        return Status.BAD_DIMS
    if m % R or n % C or (k % L and L % k):
        return Status.BAD_DIMS
    return Status.OK


#: Placeholder cycle constants, until the cost model of step 7.5 replaces them.
_SWEEP_FILL = 8
_OVERHEAD = 8


def load_a_cycles(m: int, k: int, *, L: int) -> int:
    """Rough cycles to load ``X``: a lane group per cycle."""
    return _OVERHEAD + n_groups(m * k, L)


def per_b_cycles(m: int, k: int, n: int, *, L: int, R: int, C: int) -> int:
    """Rough cycles per ``B``: its load, then per tile the skewed sweep and the output."""
    tiles = (m // R) * (n // C)
    return k * n // L + tiles * (k + R + C - 2 + _SWEEP_FILL + R * C // L)


def core_cycles(
    op: int, nb: int, m: int, k: int, n: int, *, L: int, R: int, C: int
) -> int:
    """Rough cycles of one job.  A placeholder until step 7.5 calibrates the core."""
    return load_a_cycles(m, k, L=L) + nb * per_b_cycles(m, k, n, L=L, R=R, C=C)


@dataclass
class SystolicCore(FreeRunMod):
    """The systolic matrix multiply core (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_core"
    Mmax: HwParam[int] = 8
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    L: HwParam[int] = 4
    R: HwParam[int] = 4
    C: HwParam[int] = 8
    form: HwParam[int] = 4
    sob_depth: HwParam[int] = 2
    a: Format | None = None
    b: Format | None = None
    c: Format | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        if None in (self.a, self.b, self.c):
            raise ValueError(f"{self.name}: SystolicCore needs the formats a, b and c")
        M, K, N = int(self.Mmax), int(self.Kmax), int(self.Nmax)
        L, R, C = int(self.L), int(self.R), int(self.C)
        if L < 1 or L & (L - 1):
            raise ValueError(f"the lane count L = {L} is not a power of two")
        if C % L or N % C or M % R:
            raise ValueError(f"need L | C, C | Nmax and R | Mmax (L={L}, C={C}, R={R})")
        if int(self.form) not in FORMS:
            raise ValueError(f"form must be one of {FORMS}, got {self.form}")
        self.traits = core_traits(self.a, self.b, self.c, K)
        self.cmd_in = StreamIFSlave(
            name=f"{self.name}_cmd_in", sim=self.sim, bitwidth=CMD_BITS, has_tlast=False
        )
        self.a_blk = SobIFSlave(
            name=f"{self.name}_a_blk",
            sim=self.sim,
            element_type=block_type(self.a.W, n_groups(M * K, L), L),
        )
        self.b_blk = SobIFSlave(
            name=f"{self.name}_b_blk",
            sim=self.sim,
            element_type=block_type(self.b.W, K * N // L, L),
        )
        self.c_blk = SobIFMaster(
            name=f"{self.name}_c_blk",
            sim=self.sim,
            element_type=block_type(self.c.W, M * N // L, L),
        )
        for ep in (self.cmd_in, self.a_blk, self.b_blk, self.c_blk):
            self.add_endpoint(ep)

    def status(self, op: int, nb: int, m: int, k: int, n: int) -> Status:
        """:func:`cmd_status` for this core."""
        return cmd_status(
            op,
            nb,
            m,
            k,
            n,
            Mmax=int(self.Mmax),
            Kmax=int(self.Kmax),
            Nmax=int(self.Nmax),
            L=int(self.L),
            R=int(self.R),
            C=int(self.C),
        )

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "systolic_core_task",
            CORE_BODY,
            ("cmd_in", "a_blk", "b_blk", "c_blk"),
            template_args=(
                int(self.Mmax),
                int(self.Kmax),
                int(self.Nmax),
                int(self.L),
                int(self.R),
                int(self.C),
                int(self.form),
                int(self.sob_depth),
                self.traits.id,
            ),
        )

    def linalg_parts(self) -> LinalgParts:
        """What a design with this core needs generated: its traits, body and command header."""
        return LinalgParts((self.traits,), CORE_HEADERS, (SystolicCmd,))

    def resource_structure(self):
        """What the core contains, for its resource model (:func:`~waveflow.linalg.cost.core_structure`)."""
        from waveflow.linalg.cost import core_structure

        return core_structure(self)

    @classmethod
    def get_rm(cls, platform):
        from waveflow.linalg.cost import resource_model

        return resource_model("systolic_core_task", cls, platform)

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.cmd_in.get_schema(SystolicCmd)
        op, nb = int(cmd.op), int(cmd.nb)
        m, k, n = int(cmd.m), int(cmd.k), int(cmd.n)
        st = self.status(op, nb, m, k, n)
        if st != Status.OK:
            raise RuntimeError(f"{self.name}: a command it cannot run ({st.name})")
        adjoint = op == MatmulOp.MUL_AH
        blk = yield from self.a_blk.acquire_read()
        xr, xi = blk.payload
        yield from self.a_blk.release_read()
        if xr.shape != (m, k):
            raise RuntimeError(f"{self.name}: X is {xr.shape}, the command says {m, k}")
        ar, ai = (xr.T, xi.T) if adjoint else (xr, xi)  # A as the model takes it
        L, R, C = int(self.L), int(self.R), int(self.C)
        period = self.clk.period
        yield self.timeout(load_a_cycles(m, k, L=L) * period)
        for _ in range(nb):
            blk = yield from self.b_blk.acquire_read()
            br, bi = blk.payload
            yield from self.b_blk.release_read()
            if br.shape != (k, n):
                raise RuntimeError(
                    f"{self.name}: B is {br.shape}, the command says {k, n}"
                )
            cr, ci = mm.matmul(
                ar,
                ai,
                self.a,
                br,
                bi,
                self.b,
                self.c,
                adjoint=adjoint,
                form=int(self.form),
            )
            yield self.timeout(per_b_cycles(m, k, n, L=L, R=R, C=C) * period)
            out = yield from self.c_blk.acquire_write()
            out.payload = (cr, ci)
            yield from self.c_blk.commit_write(out)


# --- the standalone unit -----------------------------------------------------------------------


def io_traits(
    a: Format, b: Format, c: Format, lane_bits: int = DEFAULT_LANE_BITS
) -> Traits:
    """The traits of the unit's message side: the registers and their memory elements."""
    return Traits(
        IO_TRAITS,
        (("a_t", a), ("b_t", b), ("c_t", c)),
        (("a_mem", a), ("b_mem", b), ("c_mem", c)),
        int(lane_bits),
    )


def request_words(
    m: int,
    k: int,
    n: int,
    lane_bits: int = DEFAULT_LANE_BITS,
    word_bits: int = DEFAULT_WORD_BITS,
) -> int:
    """Payload words of a request: ``A`` (``m·k`` values), then ``B`` (``k·n``), one burst each."""
    return nwords(m * k, lane_bits, word_bits) + nwords(k * n, lane_bits, word_bits)


def reply_words(
    m: int,
    n: int,
    lane_bits: int = DEFAULT_LANE_BITS,
    word_bits: int = DEFAULT_WORD_BITS,
) -> int:
    """Payload words of a served request's reply: ``C`` (``m·n`` values)."""
    return nwords(m * n, lane_bits, word_bits)


def request_status(
    h: LinalgHeader,
    *,
    Mmax: int,
    Kmax: int,
    Nmax: int,
    L: int,
    R: int,
    C: int,
    lane_bits: int = DEFAULT_LANE_BITS,
    word_bits: int = DEFAULT_WORD_BITS,
) -> Status:
    """The status the unit answers a request header with (``systolic_rx_task.h``)."""
    op, m, k, n = int(h.op), int(h.m), int(h.k), int(h.n)
    if op not in (MatmulOp.MUL, MatmulOp.MUL_AH):
        return Status.BAD_OP
    if int(h.nfollow):
        return Status.BAD_SEQUENCE
    dims = {"Mmax": Mmax, "Kmax": Kmax, "Nmax": Nmax, "L": L, "R": R, "C": C}
    if cmd_status(op, 1, m, k, n, **dims) != Status.OK:
        return Status.BAD_DIMS
    if int(h.length) != request_words(m, k, n, lane_bits, word_bits):
        return Status.BAD_LENGTH
    return Status.OK


@dataclass
class _UnitPart(FreeRunMod):
    """The parameters every task of the unit shares."""

    word_bits: HwParam[int] = DEFAULT_WORD_BITS
    Mmax: HwParam[int] = 8
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    L: HwParam[int] = 4
    R: HwParam[int] = 4
    C: HwParam[int] = 8
    sob_depth: HwParam[int] = 2
    lane_bits: HwParam[int] = DEFAULT_LANE_BITS
    a: Format | None = None
    b: Format | None = None
    c: Format | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        if None in (self.a, self.b, self.c):
            raise ValueError(f"{self.name}: the unit needs the formats a, b and c")
        self.core_traits = core_traits(self.a, self.b, self.c, int(self.Kmax))
        self.io_traits = io_traits(self.a, self.b, self.c, int(self.lane_bits))

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

    def _block(self, cls, name: str, fmt: Format, n_elems: int):
        L = int(self.L)
        ep = cls(
            name=f"{self.name}_{name}",
            sim=self.sim,
            element_type=block_type(fmt.W, n_groups(n_elems, L), L),
        )
        self.add_endpoint(ep)
        return ep

    def _words(self, n_elems: int) -> int:
        return nwords(n_elems, int(self.lane_bits), int(self.word_bits))

    def linalg_parts(self) -> LinalgParts:
        return LinalgParts(
            (self.core_traits, self.io_traits),
            (*CORE_HEADERS, *UNIT_HEADERS),
            (SystolicCmd,),
        )


@dataclass
class SystolicRx(_UnitPart):
    """The unit's receiver: validates each request, routes the job, forwards or drains the
    payload (``systolic_rx_task.h``)."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_rx"

    def resource_structure(self):
        from waveflow.linalg.cost import rx_structure

        return rx_structure(self)

    @classmethod
    def get_rm(cls, platform):
        from waveflow.linalg.cost import resource_model

        return resource_model("systolic_rx_task", cls, platform)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = self._framed(StreamIFSlave, "s_in")
        self.load_cmd = self._cmd(StreamIFMaster, "load_cmd")
        self.core_cmd = self._cmd(StreamIFMaster, "core_cmd")
        self.store_cmd = self._framed(StreamIFMaster, "store_cmd")
        self.s_pay = self._framed(StreamIFMaster, "s_pay")

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "systolic_rx_task",
            UNIT_HEADERS[0],
            ("s_in", "load_cmd", "core_cmd", "store_cmd", "s_pay"),
            template_args=(
                int(self.word_bits),
                int(self.Mmax),
                int(self.Kmax),
                int(self.Nmax),
                int(self.L),
                int(self.R),
                int(self.C),
                self.io_traits.id,
            ),
        )

    def status(self, h: LinalgHeader) -> Status:
        return request_status(
            h,
            Mmax=int(self.Mmax),
            Kmax=int(self.Kmax),
            Nmax=int(self.Nmax),
            L=int(self.L),
            R=int(self.R),
            C=int(self.C),
            lane_bits=int(self.lane_bits),
            word_bits=int(self.word_bits),
        )

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.word_bits)
        h = yield from self.s_in.get_schema(LinalgHeader)
        st = self.status(h)
        m, n = int(h.m), int(h.n)
        if st == Status.OK:
            cmd = np.asarray(
                command(int(h.op), m, int(h.k), n).serialize(word_bw=64), np.uint64
            )
            yield from self.load_cmd.write(cmd)
            yield from self.core_cmd.write(cmd)
        r = reply(h, st, self._words(m * n) if st == Status.OK else 0)
        yield from self.store_cmd.write(np.asarray(r.serialize(word_bw=w), np.uint64))
        done = 0
        while done < int(h.length):  # forward, or drain, the payload's bursts
            burst = yield from self.s_in.get()
            done += len(burst)
            if st == Status.OK:
                yield from self.s_pay.write(np.asarray(burst))
            else:
                yield self.timeout(len(burst) * self.clk.period)


@dataclass
class SystolicLoad(_UnitPart):
    """The unit's loader: lands ``X`` (``A``, or ``Aᵀ`` for ``Aᴴ``) and ``B`` in the core's blocks
    (``systolic_load_task.h``)."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_load"

    def resource_structure(self):
        from waveflow.linalg.cost import load_structure

        return load_structure(self)

    @classmethod
    def get_rm(cls, platform):
        from waveflow.linalg.cost import resource_model

        return resource_model("systolic_load_task", cls, platform)

    def __post_init__(self) -> None:
        super().__post_init__()
        M, K, N = int(self.Mmax), int(self.Kmax), int(self.Nmax)
        self.cmd_in = self._cmd(StreamIFSlave, "cmd_in")
        self.s_in = self._framed(StreamIFSlave, "s_in")
        self.a_blk = self._block(SobIFMaster, "a_blk", self.a, M * K)
        self.b_blk = self._block(SobIFMaster, "b_blk", self.b, K * N)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "systolic_load_task",
            UNIT_HEADERS[1],
            ("cmd_in", "s_in", "a_blk", "b_blk"),
            template_args=(
                int(self.word_bits),
                int(self.Mmax),
                int(self.Kmax),
                int(self.Nmax),
                int(self.L),
                int(self.sob_depth),
                self.core_traits.id,
                self.io_traits.id,
            ),
        )

    def _matrix(self, fmt: Format, rows: int, cols: int):
        words = yield from self.s_in.get()
        re, im = from_words(
            words, rows * cols, fmt, int(self.lane_bits), int(self.word_bits)
        )
        return re.reshape(rows, cols), im.reshape(rows, cols)

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.cmd_in.get_schema(SystolicCmd)
        op, m, k, n = int(cmd.op), int(cmd.m), int(cmd.k), int(cmd.n)
        ar, ai = yield from self._matrix(self.a, *stored_shape(op, m, k))
        if op == MatmulOp.MUL_AH:
            ar, ai = ar.T.copy(), ai.T.copy()
            yield self.timeout(m * k * self.clk.period)  # a value per cycle
        blk = yield from self.a_blk.acquire_write()
        blk.payload = (ar, ai)
        yield from self.a_blk.commit_write(blk)
        b = yield from self._matrix(self.b, k, n)
        blk = yield from self.b_blk.acquire_write()
        blk.payload = b
        yield from self.b_blk.commit_write(blk)


@dataclass
class SystolicStore(_UnitPart):
    """The unit's store: the reply header, then ``C`` for a served request
    (``systolic_store_task.h``)."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_store"

    def resource_structure(self):
        from waveflow.linalg.cost import store_structure

        return store_structure(self)

    @classmethod
    def get_rm(cls, platform):
        from waveflow.linalg.cost import resource_model

        return resource_model("systolic_store_task", cls, platform)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.cmd_in = self._framed(StreamIFSlave, "cmd_in")
        self.c_blk = self._block(
            SobIFSlave, "c_blk", self.c, int(self.Mmax) * int(self.Nmax)
        )
        self.s_out = self._framed(StreamIFMaster, "s_out")

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "systolic_store_task",
            UNIT_HEADERS[2],
            ("cmd_in", "c_blk", "s_out"),
            template_args=(
                int(self.word_bits),
                int(self.Mmax),
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
        if int(r.status) == Status.OK:
            blk = yield from self.c_blk.acquire_read()
            cr, ci = blk.payload
            words = to_words(cr, ci, self.c, int(self.lane_bits), w)
            yield from self.s_out.write(words)
            yield from self.c_blk.release_read()


@dataclass
class SystolicUnit(FreeRunMod):
    """The standalone systolic matrix multiply: framed requests in on ``s_in``, replies out on
    ``s_out`` (see :mod:`~waveflow.linalg.message`).

    A request is a :class:`~waveflow.linalg.message.LinalgHeader` (op ``MUL`` or ``MUL_AH``,
    ``m``, ``k``, ``n``, ``length``, ``nfollow = 0``) and two bursts of memory elements: ``A`` as
    the job states it (``m × k``, or ``k × m`` for ``Aᴴ``), then ``B`` (``k × n``).  The reply
    carries the request's tag, operation and dimensions, a status and, when served, ``C``
    (``m × n``).  Tasks: :class:`SystolicRx`, :class:`SystolicLoad`, :class:`SystolicCore`,
    :class:`SystolicStore`.
    """

    cpp_kernel_name: ClassVar[str | None] = "systolic_unit"
    word_bits: HwParam[int] = DEFAULT_WORD_BITS
    Mmax: HwParam[int] = 8
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    L: HwParam[int] = 4
    R: HwParam[int] = 4
    C: HwParam[int] = 8
    form: HwParam[int] = 4
    sob_depth: HwParam[int] = 2
    lane_bits: HwParam[int] = DEFAULT_LANE_BITS
    a: Format | None = None
    b: Format | None = None
    c: Format | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        shared = {
            "sim": self.sim,
            "clk": self.clk,
            "Mmax": int(self.Mmax),
            "Kmax": int(self.Kmax),
            "Nmax": int(self.Nmax),
            "L": int(self.L),
            "R": int(self.R),
            "C": int(self.C),
            "sob_depth": int(self.sob_depth),
            "a": self.a,
            "b": self.b,
            "c": self.c,
        }
        io = {
            **shared,
            "word_bits": int(self.word_bits),
            "lane_bits": int(self.lane_bits),
        }
        self.rx = SystolicRx(name=f"{self.name}_rx", **io)
        self.load = SystolicLoad(name=f"{self.name}_load", **io)
        self.core = SystolicCore(
            name=f"{self.name}_core", form=int(self.form), **shared
        )
        self.store = SystolicStore(name=f"{self.name}_store", **io)
        for comp in (self.rx, self.load, self.core, self.store):
            self.add_comp(comp)
        w = int(self.word_bits)
        self._stream("pay", self.rx.s_pay, self.load.s_in, w, framed=True)
        self._stream("reply", self.rx.store_cmd, self.store.cmd_in, w, framed=True)
        self._stream("lcmd", self.rx.load_cmd, self.load.cmd_in, CMD_BITS)
        self._stream("ccmd", self.rx.core_cmd, self.core.cmd_in, CMD_BITS)
        self._blocks("a", self.load.a_blk, self.core.a_blk)
        self._blocks("b", self.load.b_blk, self.core.b_blk)
        self._blocks("c", self.core.c_blk, self.store.c_blk)
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
        """The unit's own share, its channels (:class:`~waveflow.linalg.cost.UnitResourceModel`);
        each task has its own model."""
        from waveflow.linalg.cost import unit_model

        return unit_model(platform)
