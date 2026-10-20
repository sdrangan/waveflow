"""The systolic engine: ``C = q(A·B)`` / ``q(Aᴴ·B)`` on a streaming systolic array, fed in order.

The redesign of :class:`~waveflow.linalg.systolic.SystolicUnit` (``plans/systolic_characterization.md``,
Part B).  What changes:

* **No copy.** The loaders write ``A`` and ``B`` straight into banked stream-of-blocks buffers in the
  layout the array reads -- ``X`` a bank per row (and per word lane), ``B`` a bank per array column
  -- so the core sweeps them in place, and the ping-pong double-buffers the next operands.
* **A streaming array.** Each processing element carries a ``first`` flag with its ``X`` input and
  latches its sum into a shadow register when it arrives, starting the next tile at once: tiles follow
  every ``P = max(kp, R·C/L, L)`` cycles, and the shadows drain while the next tile runs.
* **Panels.** One command multiplies ``A`` (``m × npanels·kp``) by ``B`` (``npanels·kp × n``) as
  ``npanels`` panels of ``kp``, accumulating exactly on chip and rounding once -- the outer loop for a
  ``K`` larger than the buffers.
* **Reuse in order.** ``keep_a``: one ``A`` on ``s_in`` for ``nb`` products (the CG case); the
  ``A`` loader repeats it from a local copy, beside the sweep.
* **The default command-response shape** (``docs/guide/systems/command_response.md``): a command with
  a ``tx_id``; per output, a ``Resp`` then ``C``; a ``Status`` per command on its own stream; an error
  is fatal (the receiver stops taking input).

On ``s_in``, a command is an :class:`EngineCmd` burst, then its operands, one burst each, in the order
the core takes them: with ``keep_a``, ``A`` then the ``nb`` matrices ``B``; otherwise per output and per
panel, ``A`` then ``B``.  ``A`` is ``m × kp`` (``kp × m`` for ``MUL_AH``) and ``B`` is ``kp × n``,
row-major, as memory elements (``waveflow.linalg.lanes.to_words``).

The array is **continuous**: its state persists across panels, outputs and commands, so each tile
starts the step after the one before it ends and drains while the next runs; ``C`` leaves the core
in that drain order, and :class:`EngineCollect` places it in a block for the store.

Tasks: :class:`EngineRx`, :class:`EngineLoadA`, :class:`EngineLoadB`, :class:`EngineCore`,
:class:`EngineCollect`, :class:`EngineStore`; the composite :class:`SystolicEngine`.  The bit-exact model is
:func:`waveflow.linalg.matmul.matmul` on the whole ``K``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    FramedStreamIFSlave,
    SobIFMaster,
    SobIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
    StreamOfBlocksIF,
)
from waveflow.hw.mem_stream import KernelTask
from waveflow.hw.timed_phase import TimedPhase
from waveflow.linalg import matmul as mm
from waveflow.linalg.build import LinalgParts
from waveflow.linalg.formats import DEFAULT_LANE_BITS, mem_format
from waveflow.linalg.lanes import (
    DEFAULT_WORD_BITS,
    block_type,
    elems_per_word,
    from_words,
    nwords,
    to_words,
)
from waveflow.linalg.lanes import pack_group as lanes_pack
from waveflow.linalg.lanes import unpack_group as lanes_unpack
from waveflow.linalg.message import U8, U16, Status
from waveflow.linalg.systolic import (
    MatmulOp,
    _check_signed,
    core_traits,
    io_traits,
    stored_shape,
)
from waveflow.simulation.simobj import ProcessGen
from waveflow.utils.fixputils import Format

#: The task bodies, in ``waveflow/build/``.
ENGINE_HEADERS = ("systolic_engine_rx_task.h", "systolic_engine_load_task.h",
                  "systolic_engine_core_task.h", "systolic_engine_store_task.h")
#: The helper headers they include.
ENGINE_HELPERS = ("complex_utils.hpp", "wf_cint.h")
#: Where the core's calibrated phases are filed (a ModuleStore, by identity).
CALIB_DIR = __import__("pathlib").Path(__file__).resolve().parent / "calib"
#: Width of the internal job and reply streams (each message is two words).
CTRL_BITS = 64


# --- the messages ----------------------------------------------------------------------------------


class EngineCmd(DataList):
    """A command on ``s_in``: one ``MatMult`` job."""

    include_filename: ClassVar[str | None] = "wf_engine_cmd.h"
    elements: ClassVar[dict] = {
        "tx_id": {"schema": U16, "description": "the job's id, echoed in every Resp and the Status"},
        "op": {"schema": U8, "description": "a MatmulOp: MUL or MUL_AH"},
        "keep_a": {"schema": U8, "description": "1: one A for all nb outputs (npanels must be 1)"},
        "m": {"schema": U16, "description": "rows of C"},
        "kp": {"schema": U16, "description": "the inner dimension of one panel"},
        "n": {"schema": U16, "description": "columns of C"},
        "npanels": {"schema": U16, "description": "panels accumulated into each C (K = npanels·kp)"},
        "nb": {"schema": U16, "description": "outputs C (each its own B, or its own A and B)"},
    }


class EngineJob(DataList):
    """The job as the loaders and the core take it (the command less its ``tx_id``)."""

    include_filename: ClassVar[str | None] = "wf_engine_job.h"
    elements: ClassVar[dict] = {
        "op": {"schema": U8, "description": "a MatmulOp"},
        "keep_a": {"schema": U8, "description": "one A for the job"},
        "m": {"schema": U16, "description": "rows of C"},
        "kp": {"schema": U16, "description": "the inner dimension of a panel"},
        "n": {"schema": U16, "description": "columns of C"},
        "npanels": {"schema": U16, "description": "panels per output"},
        "nb": {"schema": U16, "description": "outputs"},
    }


class EngineReply(DataList):
    """What the store answers a command with: its outputs, or its error."""

    include_filename: ClassVar[str | None] = "wf_engine_reply.h"
    elements: ClassVar[dict] = {
        "tx_id": {"schema": U16, "description": "the command's"},
        "status": {"schema": U8, "description": "a Status"},
        "m": {"schema": U16, "description": "rows of C"},
        "n": {"schema": U16, "description": "columns of C"},
        "nb": {"schema": U16, "description": "outputs to write (0 if refused)"},
    }


class EngineResp(DataList):
    """Heads each output ``C`` on ``s_out``."""

    include_filename: ClassVar[str | None] = "wf_engine_resp.h"
    elements: ClassVar[dict] = {
        "tx_id": {"schema": U16, "description": "the command's"},
        "index": {"schema": U16, "description": "which of its nb outputs, from 0"},
        "m": {"schema": U16, "description": "rows of the C that follows"},
        "n": {"schema": U16, "description": "columns of the C that follows"},
    }


class EngineStatus(DataList):
    """One per command on ``m_status``, after its outputs."""

    include_filename: ClassVar[str | None] = "wf_engine_status.h"
    elements: ClassVar[dict] = {
        "tx_id": {"schema": U16, "description": "the command it reports on"},
        "status": {"schema": U8, "description": "OK, or BAD_OP / BAD_DIMS (fatal)"},
    }


SCHEMAS = (EngineCmd, EngineJob, EngineReply, EngineResp, EngineStatus)


def command(tx_id: int, op: int, m: int, kp: int, n: int, *, npanels: int = 1, nb: int = 1,
            keep_a: bool = False) -> EngineCmd:
    c = EngineCmd()
    c.tx_id, c.op, c.keep_a, c.m, c.kp, c.n = int(tx_id), int(op), int(bool(keep_a)), m, kp, n
    c.npanels, c.nb = int(npanels), int(nb)
    return c


def command_status(c, *, Mmax: int, Kmax: int, Nmax: int, Pmax: int, R: int, C: int) -> Status:
    """Whether an engine with these maxima can run command *c* (``systolic_engine_rx_task.h``)."""
    if int(c.op) not in (MatmulOp.MUL, MatmulOp.MUL_AH):
        return Status.BAD_OP
    m, kp, n, p, nb = int(c.m), int(c.kp), int(c.n), int(c.npanels), int(c.nb)
    if not (1 <= m <= Mmax and 1 <= kp <= Kmax and 1 <= n <= Nmax and 1 <= p <= Pmax and nb >= 1):
        return Status.BAD_DIMS
    if m % R or n % C or (int(c.keep_a) and p != 1):
        return Status.BAD_DIMS
    return Status.OK


def operand_order(c) -> list[tuple[str, int, int]]:
    """The operand bursts a command's payload carries, in order: ``(name, output, panel)``."""
    nb, p = int(c.nb), int(c.npanels)
    if int(c.keep_a):
        return [("A", 0, 0)] + [("B", ob, 0) for ob in range(nb)]
    return [x for ob in range(nb) for pp in range(p) for x in (("A", ob, pp), ("B", ob, pp))]


def period(kp: int, *, L: int, R: int, C: int) -> int:
    """The array's tile period: at least ``kp`` steps, the ``R·C/L`` output groups, and ``L``."""
    return max(int(kp), R * C // L, L)


def panel_gap(m: int, kp: int, n: int, *, L: int, R: int, C: int) -> int:
    """Steps a panel that is not an output's last runs past its tiles, so the next panel reads no
    partial sum the drain has not yet stored (``systolic_engine_core_task.h``): none for one tile,
    which keeps accumulating."""
    T = (m // R) * (n // C)
    return 0 if T == 1 else max(0, L + 9 - (T - 1) * period(kp, L=L, R=R, C=C))


def drain_order(m: int, n: int, *, L: int, R: int, C: int) -> list[tuple[int, int]]:
    """``(row, first column)`` of each ``L``-wide group of an ``m × n`` ``C``, in the order the core
    drains them: row tiles, column tiles, then the diagonals ``n = i + cg·L`` of an ``R × C`` tile."""
    G = R * C // L
    return [(rt * R + dn % R, ct * C + (dn // R) * L)
            for rt in range(m // R) for ct in range(n // C) for dn in range(G)]


def _group_words(groups: list[int], bits: int) -> np.ndarray:
    """Lane groups as stream words: one ``uint64`` each up to 64 bits, else ``k`` chunks, low first."""
    if bits <= 64:
        return np.asarray(groups, np.uint64)
    k = -(-bits // 64)
    return np.asarray([[(g >> (64 * c)) & ((1 << 64) - 1) for c in range(k)] for g in groups],
                      np.uint64)


def _word_groups(words) -> list[int]:
    w = np.asarray(words, np.uint64)
    if w.ndim == 1:
        return [int(x) for x in w]
    return [sum(int(x) << (64 * c) for c, x in enumerate(row)) for row in w]


# --- the tasks -------------------------------------------------------------------------------------


@dataclass
class _EnginePart(FreeRunMod):
    """The parameters every task of the engine shares."""

    word_bits: HwParam[int] = DEFAULT_WORD_BITS
    Mmax: HwParam[int] = 8
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    Pmax: HwParam[int] = 4
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
    #: The formats are build inputs that are not template parameters.
    build_fields: ClassVar[tuple[str, ...]] = ("a", "b", "c")

    def __post_init__(self) -> None:
        super().__post_init__()
        if None in (self.a, self.b, self.c):
            raise ValueError(f"{self.name}: the engine needs the formats a, b and c")
        _check_signed(self.name, self.a, self.b, self.c)
        M, N, L, R, C = int(self.Mmax), int(self.Nmax), int(self.L), int(self.R), int(self.C)
        if L < 1 or L & (L - 1):
            raise ValueError(f"the lane count L = {L} is not a power of two")
        if C % L or N % C or M % R:
            raise ValueError(f"need L | C, C | Nmax and R | Mmax (L={L}, C={C}, R={R})")
        if R > L:
            raise ValueError(f"the drain schedule needs R <= L (R={R}, L={L})")
        self.lw = elems_per_word(int(self.lane_bits), int(self.word_bits))
        if C % self.lw:
            raise ValueError(f"B's banks need the word's {self.lw} values to divide C = {C}")
        for fmt in (self.a, self.b, self.c):
            mem_format(fmt, int(self.lane_bits))
        # the accumulator covers a whole K: Kmax products per panel, Pmax panels
        self.core_traits = core_traits(self.a, self.b, self.c, int(self.Kmax) * int(self.Pmax))
        self.io_traits = io_traits(self.a, self.b, self.c, int(self.lane_bits))

    def _ep(self, cls, name: str, bits: int, tlast: bool):
        ep = cls(name=f"{self.name}_{name}", sim=self.sim, bitwidth=int(bits), has_tlast=tlast)
        self.add_endpoint(ep)
        return ep

    def _block(self, cls, name: str, fmt: Format, n_elems: int, L: int):
        ep = cls(name=f"{self.name}_{name}", sim=self.sim,
                 element_type=block_type(fmt.W, -(-int(n_elems) // L), L))
        self.add_endpoint(ep)
        return ep

    def status(self, c) -> Status:
        return command_status(c, Mmax=int(self.Mmax), Kmax=int(self.Kmax), Nmax=int(self.Nmax),
                              Pmax=int(self.Pmax), R=int(self.R), C=int(self.C))

    def _words(self, n_elems: int) -> int:
        return nwords(int(n_elems), int(self.lane_bits), int(self.word_bits))

    def _targs(self, *extra) -> tuple:
        return (int(self.word_bits), int(self.Mmax), int(self.Kmax), int(self.Nmax), int(self.Pmax),
                int(self.L), int(self.R), int(self.C), *extra)

    def linalg_parts(self) -> LinalgParts:
        return LinalgParts((self.core_traits, self.io_traits), (*ENGINE_HELPERS, *ENGINE_HEADERS),
                           SCHEMAS)

    def _job_words(self, job: EngineJob) -> np.ndarray:
        return np.asarray(job.serialize(word_bw=CTRL_BITS), np.uint64)

    @property
    def group_bits(self) -> int:
        """A lane group of ``C``: ``L`` complex values of the register format."""
        return 2 * int(self.c.W) * int(self.L)


@dataclass
class EngineRx(_EnginePart):
    """Validates each command, routes the job, and forwards the operand bursts -- ``A`` to one
    loader, ``B`` to the other.  A refused command is answered and is fatal: the receiver stops."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_engine_rx"

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.word_bits)
        self.s_in = self._ep(FramedStreamIFSlave, "s_in", w, True)
        self.a_job = self._ep(StreamIFMaster, "a_job", CTRL_BITS, False)
        self.b_job = self._ep(StreamIFMaster, "b_job", CTRL_BITS, False)
        self.core_job = self._ep(StreamIFMaster, "core_job", CTRL_BITS, False)
        self.reply = self._ep(StreamIFMaster, "reply", CTRL_BITS, False)
        self.pay_a = self._ep(StreamIFMaster, "pay_a", w, True)
        self.pay_b = self._ep(StreamIFMaster, "pay_b", w, True)
        self.halted = False

    def kernel_task(self) -> KernelTask:
        return KernelTask("systolic_engine_rx_task", ENGINE_HEADERS[0],
                          ("s_in", "a_job", "b_job", "core_job", "reply", "pay_a", "pay_b"),
                          framed=("s_in",), template_args=self._targs(self.io_traits.id))

    def run_iter(self) -> ProcessGen[None]:
        if self.halted:                       # fatal: never take input again (a reset clears it)
            yield self.env.event()
        c = yield from self.s_in.get_schema(EngineCmd)
        st = self.status(c)
        r = EngineReply()
        r.tx_id, r.status, r.m, r.n = int(c.tx_id), int(st), int(c.m), int(c.n)
        r.nb = int(c.nb) if st == Status.OK else 0
        if st != Status.OK:
            yield from self.reply.write(self._job_words(r))
            self.halted = True
            return
        job = EngineJob()
        for f in ("op", "keep_a", "m", "kp", "n", "npanels", "nb"):
            setattr(job, f, int(getattr(c, f)))
        words = self._job_words(job)
        for ep in (self.a_job, self.b_job, self.core_job):
            yield from ep.write(words)
        yield from self.reply.write(self._job_words(r))
        for name, _ob, _p in operand_order(c):
            burst = yield from self.s_in.get()
            yield from (self.pay_a if name == "A" else self.pay_b).write(np.asarray(burst))


@dataclass
class _EngineLoad(_EnginePart):
    """A loader: one operand, straight into its banked block."""

    operand: ClassVar[str] = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        M, K, N = int(self.Mmax), int(self.Kmax), int(self.Nmax)
        self.job_in = self._ep(StreamIFSlave, "job_in", CTRL_BITS, False)
        self.s_in = self._ep(StreamIFSlave, "s_in", int(self.word_bits), True)
        if self.operand == "A":
            self.blk = self._block(SobIFMaster, "blk", self.a, M * K, 1)
        else:
            self.blk = self._block(SobIFMaster, "blk", self.b, K * N, 1)

    def kernel_task(self) -> KernelTask:
        return KernelTask(f"systolic_engine_load_{self.operand.lower()}_task", ENGINE_HEADERS[1],
                          ("job_in", "s_in", "blk"),
                          template_args=self._targs(int(self.sob_depth), int(self.lw),
                                                    self.core_traits.id, self.io_traits.id))

    def run_iter(self) -> ProcessGen[None]:
        job = yield from self.job_in.get_schema(EngineJob)
        op, m, kp, n = int(job.op), int(job.m), int(job.kp), int(job.n)
        count = int(job.nb) * int(job.npanels)       # a block per output and panel
        fmt, shape = (self.a, stored_shape(op, m, kp)) if self.operand == "A" else (self.b, (kp, n))
        keep = self.operand == "A" and bool(int(job.keep_a))
        held = None
        for q in range(count):
            if keep and q > 0:                           # keep_a: the one A, repeated
                re, im = held
            else:
                words = yield from self.s_in.get()
                re, im = from_words(words, shape[0] * shape[1], fmt, int(self.lane_bits),
                                    int(self.word_bits))
                re, im = re.reshape(shape), im.reshape(shape)
                if self.operand == "A" and op == MatmulOp.MUL_AH:
                    re, im = re.T.copy(), im.T.copy()       # the block holds X = Aᵀ
                held = (re, im)
            blk = yield from self.blk.acquire_write()
            blk.payload = (re, im)
            yield from self.blk.commit_write(blk)


@dataclass
class EngineLoadA(_EngineLoad):
    """Loads ``A`` (or ``Aᵀ`` for ``MUL_AH``) into the ``X`` block, a bank per row and word lane."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_engine_load_a"
    operand: ClassVar[str] = "A"


@dataclass
class EngineLoadB(_EngineLoad):
    """Loads ``B`` into its block, a bank per array column."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_engine_load_b"
    operand: ClassVar[str] = "B"


@dataclass
class EngineCore(_EnginePart):
    """The streaming systolic array (``systolic_engine_core_task.h``): per output, per panel, it
    sweeps the ``X`` and ``B`` blocks in place, accumulating exactly; the last panel rounds once, and
    ``C`` leaves on ``c_out`` in drain order (:func:`drain_order`), a lane group a word.  At RTL the
    array runs continuously across a command's panels and outputs, and runs its last tile out at the
    command's end; pysim times each panel."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_engine_core"

    def __post_init__(self) -> None:
        super().__post_init__()
        M, K, N, L = int(self.Mmax), int(self.Kmax), int(self.Nmax), int(self.L)
        self.job_in = self._ep(StreamIFSlave, "job_in", CTRL_BITS, False)
        self.a_blk = self._block(SobIFSlave, "a_blk", self.a, M * K, 1)
        self.b_blk = self._block(SobIFSlave, "b_blk", self.b, K * N, 1)
        self.col_job = self._ep(StreamIFMaster, "col_job", CTRL_BITS, False)
        self.c_out = self._ep(StreamIFMaster, "c_out", self.group_bits, False)
        # pysim's time: a panel from its B's acquire to its release (the core holds X and B for its
        # tiles and frees both at their end) -- the tiles at the period; continuous at RTL, so no fill
        # or drain per panel.  Seeded, not measured: calibrated once, at the end.
        self.panel = TimedPhase(
            "panel", spans={"panel": (self.b_blk.first, self.b_blk.last)}, features=("steps",),
            seed={"panel": {"intercept": 2.0, "steps": 1.0}}, calib_dir=CALIB_DIR)
        self.add_timing_phase(self.panel)

    def kernel_task(self) -> KernelTask:
        return KernelTask("systolic_engine_core_task", ENGINE_HEADERS[2],
                          ("job_in", "a_blk", "b_blk", "col_job", "c_out"),
                          template_args=(int(self.Mmax), int(self.Kmax), int(self.Nmax),
                                         int(self.Pmax), int(self.L), int(self.R), int(self.C),
                                         int(self.form), int(self.sob_depth), int(self.lw),
                                         self.core_traits.id))

    def steps(self, m: int, kp: int, n: int, last: bool = True, run_out: bool = False) -> int:
        """A panel's steps: ``tiles · P``; the gap a panel that is not an output's last may need; and
        on a command's last panel the empty tile that runs its last tile out (``L + R·C/L``)."""
        L, R, C = int(self.L), int(self.R), int(self.C)
        gap = 0 if last else panel_gap(m, kp, n, L=L, R=R, C=C)
        tail = L + R * C // L if run_out else 0
        return (m // R) * (n // C) * period(kp, L=L, R=R, C=C) + gap + tail

    def run_iter(self) -> ProcessGen[None]:
        from waveflow.utils import fixputils as fx

        job = yield from self.job_in.get_schema(EngineJob)
        yield from self.col_job.write(self._job_words(job))
        op = int(job.op)
        m, kp, n, npan, nb = int(job.m), int(job.kp), int(job.n), int(job.npanels), int(job.nb)
        adj = op == MatmulOp.MUL_AH
        # every panel takes an X block (with keep_a the A loader repeats its one A) and a B block
        for _ob in range(nb):
            accr = acci = None
            for _p in range(npan):
                ablk = yield from self.a_blk.acquire_read()
                bblk = yield from self.b_blk.acquire_read()
                (xr, xi), (br, bi) = ablk.payload, bblk.payload
                ar, ai = (xr.T, xi.T) if adj else (xr, xi)   # the model takes A as stated
                pr, pi, fmt = mm.matmul_exact(ar, ai, self.a, br, bi, self.b, adjoint=adj,
                                              form=int(self.form))
                accr, acci = (pr, pi) if accr is None else (accr + pr, acci + pi)
                yield self.panel.timeout(steps=self.steps(m, kp, n, _p == npan - 1,
                                                          _p == npan - 1 and _ob == nb - 1))
                yield from self.b_blk.release_read()
                yield from self.a_blk.release_read()
            cr, ci = fx.quantize(accr, fmt, self.c), fx.quantize(acci, fmt, self.c)
            yield from self.c_out.write(self.groups_of(cr, ci))

    def groups_of(self, cr, ci) -> np.ndarray:
        """``C``'s stored integers as ``c_out`` words: its lane groups in drain order."""
        L, W = int(self.L), int(self.c.W)
        m, n = cr.shape
        groups = [lanes_pack(cr[r, c0:c0 + L], ci[r, c0:c0 + L], W)
                  for r, c0 in drain_order(m, n, L=L, R=int(self.R), C=int(self.C))]
        return _group_words(groups, self.group_bits)


@dataclass
class EngineCollect(_EnginePart):
    """Per output, ``C``'s lane groups off ``c_in`` in the core's drain order, each into its place in
    a ``C`` block for the store."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_engine_collect"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.job_in = self._ep(StreamIFSlave, "job_in", CTRL_BITS, False)
        self.c_in = self._ep(StreamIFSlave, "c_in", self.group_bits, False)
        self.c_blk = self._block(SobIFMaster, "c_blk", self.c, int(self.Mmax) * int(self.Nmax),
                                 int(self.L))

    def kernel_task(self) -> KernelTask:
        return KernelTask("systolic_engine_collect_task", ENGINE_HEADERS[2],
                          ("job_in", "c_in", "c_blk"),
                          template_args=self._targs(int(self.sob_depth), self.core_traits.id))

    def run_iter(self) -> ProcessGen[None]:
        job = yield from self.job_in.get_schema(EngineJob)
        m, n, L, W = int(job.m), int(job.n), int(self.L), int(self.c.W)
        order = drain_order(m, n, L=L, R=int(self.R), C=int(self.C))
        for _ob in range(int(job.nb)):
            words = yield from self.c_in.get(nwords_max=len(order))
            cr, ci = np.zeros((m, n), np.int64), np.zeros((m, n), np.int64)
            for (r, c0), g in zip(order, _word_groups(words), strict=True):
                re, im = lanes_unpack(g, W, L)
                cr[r, c0:c0 + L], ci[r, c0:c0 + L] = re, im
            out = yield from self.c_blk.acquire_write()
            out.payload = (cr, ci)
            yield from self.c_blk.commit_write(out)


@dataclass
class EngineStore(_EnginePart):
    """Per output, its ``Resp`` and ``C``; then the command's ``Status``."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_engine_store"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.reply = self._ep(StreamIFSlave, "reply", CTRL_BITS, False)
        self.c_blk = self._block(SobIFSlave, "c_blk", self.c, int(self.Mmax) * int(self.Nmax),
                                 int(self.L))
        self.s_out = self._ep(FramedStreamIFMaster, "s_out", int(self.word_bits), True)
        self.m_status = self._ep(FramedStreamIFMaster, "m_status", CTRL_BITS, True)

    def kernel_task(self) -> KernelTask:
        return KernelTask("systolic_engine_store_task", ENGINE_HEADERS[3],
                          ("reply", "c_blk", "s_out", "m_status"), framed=("s_out", "m_status"),
                          template_args=self._targs(int(self.sob_depth), self.core_traits.id,
                                                    self.io_traits.id))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.word_bits)
        r = yield from self.reply.get_schema(EngineReply)
        m, n = int(r.m), int(r.n)
        for ob in range(int(r.nb)):
            blk = yield from self.c_blk.acquire_read()
            cr, ci = blk.payload
            resp = EngineResp()
            resp.tx_id, resp.index, resp.m, resp.n = int(r.tx_id), ob, m, n
            yield from self.s_out.write(np.asarray(resp.serialize(word_bw=w), np.uint64))
            yield from self.s_out.write(np.asarray(
                to_words(cr, ci, self.c, int(self.lane_bits), w), np.uint64))
            yield from self.c_blk.release_read()
        st = EngineStatus()
        st.tx_id, st.status = int(r.tx_id), int(r.status)
        yield from self.m_status.write(np.asarray(st.serialize(word_bw=CTRL_BITS), np.uint64))


# --- the composite ---------------------------------------------------------------------------------


@dataclass
class SystolicEngine(_EnginePart):
    """The engine: commands and operands in on ``s_in``, outputs on ``s_out``, a status per command
    on ``m_status`` (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "systolic_engine"
    endpoint_port_widths: ClassVar[bool] = True     # m_status stays 64 bits beside wider data words

    def __post_init__(self) -> None:
        super().__post_init__()
        p = {f: getattr(self, f) for f in ("word_bits", "Mmax", "Kmax", "Nmax", "Pmax", "L", "R",
                                           "C", "form", "sob_depth", "lane_bits", "a", "b", "c")}
        p = {**p, "sim": self.sim, "clk": self.clk}
        self.rx = EngineRx(name=f"{self.name}_rx", **p)
        self.load_a = EngineLoadA(name=f"{self.name}_load_a", **p)
        self.load_b = EngineLoadB(name=f"{self.name}_load_b", **p)
        self.core = EngineCore(name=f"{self.name}_core", **p)
        self.collect = EngineCollect(name=f"{self.name}_collect", **p)
        self.store = EngineStore(name=f"{self.name}_store", **p)
        for comp in (self.rx, self.load_a, self.load_b, self.core, self.collect, self.store):
            self.add_comp(comp)
        w = int(self.word_bits)
        # Each control message is 2 words; every channel holds two, so a task that keeps one for
        # its whole firing never stops the receiver on the next (plans/systolic_characterization.md).
        for name, src, dst in (("ajob", self.rx.a_job, self.load_a.job_in),
                               ("bjob", self.rx.b_job, self.load_b.job_in),
                               ("cjob", self.rx.core_job, self.core.job_in),
                               ("reply", self.rx.reply, self.store.reply),
                               ("kjob", self.core.col_job, self.collect.job_in)):
            self._stream(name, src, dst, CTRL_BITS, depth=4)
        # A's words wait here: its loader takes a value a cycle (half the word rate), and the receiver
        # forwards A and then B in order, so a channel holding one whole A lets it move on to B at once
        # (206 -> 175 cycles a job, input-bound: plans/systolic_characterization.md).
        self._stream("paya", self.rx.pay_a, self.load_a.s_in, w, framed=True,
                     depth=max(2, self._words(int(self.Mmax) * int(self.Kmax))))
        self._stream("payb", self.rx.pay_b, self.load_b.s_in, w, framed=True)
        M, K, N, lw, C = int(self.Mmax), int(self.Kmax), int(self.Nmax), self.lw, int(self.C)
        # X [Mmax][Kmax]: a bank per row -- the loader writes a value a cycle, each array row reads
        # its bank a value a cycle.
        self._blocks("a", self.load_a.blk, self.core.a_blk).bank((M, K), ((1, "complete", 0),))
        # B [Kmax][Nmax/C][C/lw][lw]: a bank per array column, (j/lw, j%lw).  A word's lw values land
        # in lanes 0..lw-1 of one (row, tile, group) -- n is a multiple of C, so no word crosses a
        # row -- so the loader writes a word a cycle into banks the compiler can tell apart.
        self._blocks("b", self.load_b.blk, self.core.b_blk).bank(
            (K, N // C, C // lw, lw), ((3, "complete", 0), (4, "complete", 0)))
        # C in drain order: a tile's G groups come a step apart, so two tiles' worth lets the
        # collector place them a cycle each without holding the array back.
        G = int(self.R) * int(self.C) // int(self.L)
        self._stream("cg", self.core.c_out, self.collect.c_in, self.core.group_bits, depth=2 * G)
        self._blocks("c", self.collect.c_blk, self.store.c_blk)
        self.boundary = ["s_in", "s_out", "m_status"]
        self.s_in = self.rx.s_in
        self.s_out = self.store.s_out
        self.m_status = self.store.m_status
        self.extra_includes = ["hls_streamofblocks.h"]

    def _stream(self, name, master, slave, bits: int, framed: bool = False, depth=None) -> None:
        kw = {"framed": True} if framed else {}
        if depth is not None:
            kw["depth"] = depth
        iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=bits,
                         **kw)
        iface.bind("master", master)
        iface.bind("slave", slave)
        self.add_if(iface)

    def _blocks(self, name, master, slave) -> StreamOfBlocksIF:
        iface = StreamOfBlocksIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                                 element_type=master.element_type, depth=int(self.sob_depth))
        iface.bind("master", master)
        iface.bind("slave", slave)
        self.add_if(iface)
        return iface

    # -- scenarios ----------------------------------------------------------------------------------
    def jobs(self, kind: str = "test") -> list[dict]:
        """The commands :meth:`workload` sends, as :func:`command` keywords."""
        M, K, N, P = int(self.Mmax), int(self.Kmax), int(self.Nmax), int(self.Pmax)
        R, C = int(self.R), int(self.C)
        if kind == "test":
            return [dict(op=MatmulOp.MUL, m=M, kp=K, n=N),
                    dict(op=MatmulOp.MUL_AH, m=R, kp=K, n=C),
                    dict(op=MatmulOp.MUL, m=M, kp=K, n=2 * C, npanels=min(P, 3)),
                    dict(op=MatmulOp.MUL, m=R, kp=3, n=N, nb=2, keep_a=True),
                    dict(op=MatmulOp.MUL_AH, m=M, kp=5, n=C, npanels=2, nb=2),
                    dict(op=MatmulOp.MUL, m=M, kp=1, n=N)]
        if kind == "steady":
            return [dict(op=MatmulOp.MUL, m=M, kp=K, n=N) for _ in range(4)]
        if kind == "short":                      # short panels: the output, not the input, binds
            return ([dict(op=MatmulOp.MUL, m=M, kp=1, n=N) for _ in range(4)]
                    + [dict(op=MatmulOp.MUL, m=M, kp=2, n=N) for _ in range(4)])
        raise ValueError(f"no workload {kind!r}")

    def _operands(self, kind: str):
        rng = np.random.default_rng(11)
        fa, fb = self.a, self.b
        lo = lambda f: -(1 << (f.W - 1))  # noqa: E731
        hi = lambda f: 1 << (f.W - 1)  # noqa: E731
        for j, kw in enumerate(self.jobs(kind)):
            c = command(100 + j, **kw)
            ops = {}
            for name, ob, pp in operand_order(c):
                if name == "A":
                    shape = stored_shape(int(c.op), int(c.m), int(c.kp))
                    ops[name, ob, pp] = tuple(rng.integers(lo(fa), hi(fa), shape) for _ in range(2))
                else:
                    shape = (int(c.kp), int(c.n))
                    ops[name, ob, pp] = tuple(rng.integers(lo(fb), hi(fb), shape) for _ in range(2))
            yield c, ops

    def workload(self, kind: str = "test") -> dict:
        from waveflow.utils.burst_io import StreamBurst

        lb, w = int(self.lane_bits), int(self.word_bits)
        bursts = []
        for c, ops in self._operands(kind):
            bursts.append(StreamBurst(np.asarray(c.serialize(word_bw=w), np.uint64), True))
            for name, ob, pp in operand_order(c):
                re, im = ops[name, ob, pp]
                fmt = self.a if name == "A" else self.b
                bursts.append(StreamBurst(np.asarray(to_words(re, im, fmt, lb, w), np.uint64), True))
        return {"s_in": bursts}

    def golden(self, kind: str = "test") -> dict:
        """``s_out`` and ``m_status`` from the bit-exact model on the whole ``K`` of each output."""
        lb, w = int(self.lane_bits), int(self.word_bits)
        out, status = [], []
        for c, ops in self._operands(kind):
            st = self.status(c)
            adj = int(c.op) == MatmulOp.MUL_AH
            if st == Status.OK:
                for ob in range(int(c.nb)):
                    if int(c.keep_a):
                        a_parts, b_parts = [ops["A", 0, 0]], [ops["B", ob, 0]]
                    else:
                        a_parts = [ops["A", ob, pp] for pp in range(int(c.npanels))]
                        b_parts = [ops["B", ob, pp] for pp in range(int(c.npanels))]
                    axis = 0 if adj else 1           # A is k x m for A^H: panels stack its rows
                    ar = np.concatenate([a[0] for a in a_parts], axis=axis)
                    ai = np.concatenate([a[1] for a in a_parts], axis=axis)
                    br = np.concatenate([b[0] for b in b_parts], axis=0)
                    bi = np.concatenate([b[1] for b in b_parts], axis=0)
                    cr, ci = mm.matmul(ar, ai, self.a, br, bi, self.b, self.c, adjoint=adj,
                                       form=int(self.form))
                    resp = EngineResp()
                    resp.tx_id, resp.index, resp.m, resp.n = int(c.tx_id), ob, int(c.m), int(c.n)
                    out.append(np.asarray(resp.serialize(word_bw=w), np.uint64))
                    out.append(np.asarray(to_words(cr, ci, self.c, lb, w), np.uint64))
            s = EngineStatus()
            s.tx_id, s.status = int(c.tx_id), int(st)
            status.append(np.asarray(s.serialize(word_bw=CTRL_BITS), np.uint64))
        return {"s_out": out, "m_status": status}
