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

Formats
-------
The formats are one :class:`~waveflow.linalg.cg.CgFormats`, a plain field.  They reach the body
as one integer, the format id of :func:`cg_traits`, which also carries the exact types the body
needs: the widened dividend ``rzw_t`` and the dot accumulators ``dot_ps_t`` and ``dot_rz_t`` (sums
of ``Kmax`` terms).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import SobIFMaster, SobIFSlave, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.linalg.build import LinalgParts
from waveflow.linalg.cg import CgFormats, accumulator_formats, cg_init, vec_step
from waveflow.linalg.formats import Traits
from waveflow.linalg.lanes import block_type
from waveflow.linalg.message import U16, Status
from waveflow.simulation.simobj import ProcessGen

#: The task body of the core, in ``waveflow/build/``.
CORE_BODY = "cg_vector_task.h"
#: The headers a design with the core copies from ``waveflow/build/``.
CORE_HEADERS = (CORE_BODY,)
#: The traits family of the core.
CORE_TRAITS = "wf_cg_traits"
#: Bits of the core's command stream: one :class:`CgVectorCmd` per word.
CMD_BITS = 64


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
        yield self.timeout(start_cycles(k, n, L=L) * period)
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
            yield self.timeout(iter_cycles(k, n, L=L) * period)
            if it < nit:
                yield from self._write(self.p_blk, state.pr, state.pi)
            else:
                yield from self._write(self.x_blk, state.xr, state.xi)

    def _write(self, ep, re, im) -> ProcessGen[None]:
        out = yield from ep.acquire_write()
        out.payload = (re, im)
        yield from ep.commit_write(out)
