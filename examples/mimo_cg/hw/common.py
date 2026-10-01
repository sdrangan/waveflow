"""common.py — what every CG hardware block shares: element packing, commands, C++ types.

Step 4.1 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 4.0 decision record, §14).

Memory layout
-------------
A register value travels through memory in its **memory format**: the register format widened
to 16 bits with the same integer bits (:func:`mem_format`).  Widening only adds fraction bits,
so it is exact both ways, and a complex element is an aligned 32 bits (``re`` low, ``im``
high).  The packing itself is the framework's: :func:`to_words` / :func:`from_words` go
through ``DataArray[ComplexField[FixedField]].serialize``, whose generated C++ array utils the
task bodies use, so no block hand-rolls a shift or a mask.  (A 12-bit register packed as is
would put 2.5 complex elements in a 64-bit word, splitting ``re`` from ``im`` across words.)
Matrices are row-major in ``MEM_DW``-bit words, the widths the framework ``MemRStream`` /
``MemWStream`` support (32 and 64).  A job reads ``A`` (K·K elements) at word offset ``a_off``
and ``B`` (K·N) at ``b_off``, and writes ``X`` (K·N) at ``x_off``.

The commands
------------
:class:`CgCmd` is the plain boundary command, ``{a_off, b_off, x_off, nit}``; :class:`CgDesc`
is the framed internal descriptor forwarded through the pipeline, ``{nit, x_off}``.  ``nit`` is
a runtime field, so one RTL build runs any iteration count up to its ``K``.

C++ types
---------
:func:`cg_type_map` names every register and exact-accumulator format a block needs, and
:func:`render_typedefs` renders them as ``ap_fixed`` typedefs.  The step 2.4 conformance
testbench uses the same renderer, so the hardware and the proven C++ reference agree on types.
"""

from __future__ import annotations

from enum import IntEnum
from typing import ClassVar

import numpy as np

from examples.mimo_cg.mimo_cg_fixed import CgFormats, accumulator_formats
from waveflow.hw.complexfield import ComplexField
from waveflow.hw.dataschema import DataArray, DataList, EnumField, IntField
from waveflow.hw.fixpoint import FixedField
from waveflow.utils import complexutils as cx
from waveflow.utils.fixputils import Format

#: Bits per lane (one of ``re`` / ``im``) and per complex element.
LANE_BITS = 16
ELEM_BITS = 2 * LANE_BITS
#: Memory word widths the framework mem-streams support (``mem_stream.WORD_BW_SUPPORTED``).
MEM_DW_SUPPORTED = (32, 64)
DEFAULT_MEM_DW = 64
#: Right-hand sides per job: the N_s received vectors of a coherence block.
DEFAULT_N = 32

Word32 = IntField.specialize(bitwidth=32, signed=False)


class CgCmd(DataList):
    """One detector job (host → ``s_cmd``): solve with ``A`` at ``a_off`` and ``B`` at ``b_off``
    for ``nit`` iterations and write ``X`` at ``x_off`` (word offsets)."""

    include_filename: ClassVar[str | None] = "cg_cmd.h"
    elements: ClassVar[dict] = {
        "a_off": {"schema": Word32, "description": "A (K x K) word offset"},
        "b_off": {"schema": Word32, "description": "B (K x N) word offset"},
        "x_off": {"schema": Word32, "description": "X (K x N) output word offset"},
        "nit": {"schema": Word32, "description": "CG iterations (1..K)"},
    }


class CgDesc(DataList):
    """The framed internal descriptor of one job: the iteration count and the output offset."""

    include_filename: ClassVar[str | None] = "cg_desc.h"
    elements: ClassVar[dict] = {
        "nit": {"schema": Word32, "description": "CG iterations (1..K)"},
        "x_off": {"schema": Word32, "description": "X (K x N) output word offset"},
    }


class IterOp(IntEnum):
    """What one per-iteration command asks of a block."""

    INIT = 0  #: start a job (the vector unit initializes from B, the matmul loads A)
    ITER = 1  #: one CG iteration
    LAST = 2  #: the job's final iteration (the vector unit then emits X, not P)


IterOpField = EnumField.specialize(enum_type=IterOp, bitwidth=32)


class CgIterCmd(DataList):
    """One entry of a block's command queue (``cg_ctrl`` → ``cg_vec`` / ``cg_mm``): the op and the
    iteration it belongs to (0 for ``INIT``)."""

    include_filename: ClassVar[str | None] = "cg_iter_cmd.h"
    elements: ClassVar[dict] = {
        "op": {"schema": IterOpField, "description": "INIT, ITER or LAST"},
        "it": {
            "schema": Word32,
            "description": "iteration number, 1..nit (0 for INIT)",
        },
    }


# --- memory formats and word packing --------------------------------------------------------


def mem_format(fmt: Format) -> Format:
    """The 16-bit memory format of a register: the same integer bits, more fraction bits."""
    if fmt.W > LANE_BITS:
        raise ValueError(
            f"a {fmt.W}-bit register is wider than the {LANE_BITS}-bit lane"
        )
    return Format(LANE_BITS, fmt.int_bits, fmt.signed, fmt.q_mode, fmt.o_mode)


def check_lane_formats(formats: CgFormats) -> None:
    """Raise if a register that travels in memory or a block (A, B, P, R, S, X) is wider than a lane."""
    wide = [
        name
        for name in ("A", "B", "P", "R", "S", "X")
        if getattr(formats, name).W > LANE_BITS
    ]
    if wide:
        raise ValueError(f"registers {wide} are wider than the {LANE_BITS}-bit lane")


def mem_array_type(fmt: Format, n: int) -> type[DataArray]:
    """``DataArray[ComplexField[FixedField<mem_format(fmt)>]]`` of ``n`` elements."""
    m = mem_format(fmt)
    inner = FixedField.specialize(m.W, m.int_bits, m.signed, m.q_mode, m.o_mode)
    return DataArray.specialize(ComplexField.specialize(inner), max_shape=(int(n),))


def _check_mem_dw(mem_dw: int) -> None:
    if mem_dw not in MEM_DW_SUPPORTED:
        raise ValueError(f"mem_dw {mem_dw} not in {MEM_DW_SUPPORTED}")


def to_words(
    re: np.ndarray, im: np.ndarray, fmt: Format, mem_dw: int = DEFAULT_MEM_DW
) -> np.ndarray:
    """Stored register integers ``(re, im)`` (any shape, row-major) → memory words (``uint64``)."""
    _check_mem_dw(mem_dw)
    shift = LANE_BITS - fmt.W
    re = np.asarray(re, np.int64).reshape(-1) << shift
    im = np.asarray(im, np.int64).reshape(-1) << shift
    arr = mem_array_type(fmt, re.size)(cx.make_complex(re, im, mem_format(fmt)))
    return np.asarray(arr.serialize(word_bw=mem_dw), dtype=np.uint64)


def from_words(
    words: np.ndarray, n: int, fmt: Format, mem_dw: int = DEFAULT_MEM_DW
) -> tuple[np.ndarray, np.ndarray]:
    """Memory words → the stored register integers ``(re, im)`` of the first ``n`` elements.

    Raises if a value has fraction bits the register format cannot hold: the words were not
    written from that register format.
    """
    _check_mem_dw(mem_dw)
    arr = mem_array_type(fmt, n)().deserialize(
        np.asarray(words, dtype=np.uint64), word_bw=mem_dw
    )
    pairs = np.ascontiguousarray(np.asarray(arr)).view(np.int64).reshape(-1, 2)
    re, im = (
        pairs[:, 0].copy(),
        pairs[:, 1].copy(),
    )  # each element is an (re, im) int64 record
    shift = LANE_BITS - fmt.W
    if np.any(re & ((1 << shift) - 1)) or np.any(im & ((1 << shift) - 1)):
        raise ValueError(
            f"words hold values finer than the {fmt.W}-bit register format"
        )
    return re >> shift, im >> shift


def nwords(n_elems: int, mem_dw: int = DEFAULT_MEM_DW) -> int:
    """Memory words holding ``n_elems`` complex elements (aligned, ``mem_dw / 32`` per word)."""
    _check_mem_dw(mem_dw)
    return -(-n_elems // (mem_dw // ELEM_BITS))


# --- the formats the hardware is built for ----------------------------------------------------


#: The format sets a block can be built with, by id (a ``HwParam`` is an integer): the M3 frontier
#: formats (W12g8, the default; W14g8 for 64-QAM 32×16) and the M2 saturation stress set
#: (gate 4.0 decision 5).
HW_FORMAT_NAMES = ("W12g8", "W14g8", "stress")


def hw_formats() -> dict[str, CgFormats]:
    """The buildable format sets, by name (see :data:`HW_FORMAT_NAMES`)."""
    from examples.mimo_cg.mimo_cg_accuracy_sweep import sweep_format
    from examples.mimo_cg.mimo_cg_conformance import STRESS_FORMATS

    return {
        "W12g8": sweep_format(12, 8),
        "W14g8": sweep_format(14, 8),
        "stress": STRESS_FORMATS,
    }


def hw_format(fmt_id: int) -> CgFormats:
    """The format set with this id (``HW_FORMAT_NAMES[fmt_id]``)."""
    return hw_formats()[HW_FORMAT_NAMES[int(fmt_id)]]


# --- C++ types -----------------------------------------------------------------------------


def ap_type(fmt: Format) -> str:
    """The ``ap_fixed`` spelling of a format."""
    return f"ap_fixed<{fmt.W}, {fmt.int_bits}, {fmt.q_mode.value}, {fmt.o_mode.value}>"


def cg_type_map(formats: CgFormats, K: int) -> dict[str, Format]:
    """Every register and exact-accumulator type a CG implementation needs, by C++ name.

    The registers ``a_t … beta_t``, the widened dividend ``rzw_t``, and the exact accumulators
    ``mm_ap_t`` (A·P), ``mm_ax_t`` (A·X), ``dot_ps_t`` (Re PᴴS) and ``dot_rz_t`` (|R|²).
    """
    f = formats
    acc = accumulator_formats(f, K)
    return {
        "a_t": f.A, "b_t": f.B, "p_t": f.P, "r_t": f.R, "s_t": f.S, "x_t": f.X,
        "ps_t": f.ps, "rz_t": f.rz, "alpha_t": f.alpha, "beta_t": f.beta, "rzw_t": acc["rzw"],
        "mm_ap_t": acc["mm_ap"], "mm_ax_t": acc["mm_ax"], "dot_ps_t": acc["dot_ps"],
        "dot_rz_t": acc["dot_rz"],
    }  # fmt: skip


def render_typedefs(formats: CgFormats, K: int, indent: str = "    ") -> str:
    """The ``typedef ap_fixed<…> name;`` lines of :func:`cg_type_map`, one per line."""
    return "\n".join(
        f"{indent}typedef {ap_type(v)} {k};" for k, v in cg_type_map(formats, K).items()
    )
