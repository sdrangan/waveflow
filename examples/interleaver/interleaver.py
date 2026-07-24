"""interleaver.py — shared schema + constants for the interleaver example.

The interleaver **design** lives in :mod:`examples.interleaver.interleaver_inband` (the gather
``Y[i] = X[P[i]]`` composed on the framework ``MemRStream`` / ``MemWStream``).  This module holds only
the pieces the design and its harnesses (``interleaver_sim``, ``calibrate_compute``,
``interleaver_figures``) share: the boundary command :class:`InterleaverCmd`, the 32-bit element type
:class:`IlElem`, and the seeded compute-timing constants.

The word-packed canonical six-stage variant (``InterleaverCanon`` + its ``il_mem_r`` / ``il_load`` /
``il_compute`` / ``il_store`` custom adaptors) was retired 2026-07 once the in-band design was
XSI-verified; the in-band design is the one and only interleaver now.
"""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

HERE = Path(__file__).resolve().parent

from waveflow.build.composite_gen import INCLUDE_DIR  # noqa: E402
from waveflow.hw.dataschema import DataList, IntField  # noqa: E402

DEFAULT_MEM_DW = 64
DEFAULT_N = 256


# The block element type (32-bit, unsigned): its array-utils header (il_elem_array_utils.h) supplies the
# read_framed_stream_lane / elem_read<MEM_DW> the gather uses.  The class name fixes the header/namespace.
#
# It is a distinct *subclass* rather than ``specialize(...).__name__ = "IlElem"``: ``specialize`` returns
# a cached class keyed by (bitwidth, signed, include_dir), so that shared object is the very same
# ``UInt32`` another example (shared_mem/hist) specializes with the same key — renaming it in place would
# corrupt that example's codegen (its array-utils namespace derives from ``__name__``).  A subclass gets
# its own name while inheriting bitwidth/signed/include_dir/cpp_type untouched.
class IlElem(IntField.specialize(bitwidth=32, signed=False, include_dir=INCLUDE_DIR)):  # type: ignore[misc]
    pass

# --- command field type (element/word coordinates — the word_index convention) --------------------
Word32 = IntField.specialize(bitwidth=32, signed=False)


class InterleaverCmd(DataList):
    """One app interleaver command (host -> ``s_cmd``): gather ``n`` elements ``Y[i]=X[P[i]]`` where
    P lives at word offset ``p_off``, X at ``x_off``, Y at ``y_off`` (all element/word coordinates)."""
    include_filename: ClassVar[str | None] = "il_cmd.h"
    elements = {
        "p_off": {"schema": Word32, "description": "P (index) buffer word offset"},
        "x_off": {"schema": Word32, "description": "X (source) buffer word offset"},
        "y_off": {"schema": Word32, "description": "Y (output) buffer word offset"},
        "n":     {"schema": Word32, "description": "number of elements to interleave"},
    }


#: Schema classes the gen-include step emits C++ headers for.
SCHEMA_CLASSES = [InterleaverCmd]


def _nwords(n: int, lw: int) -> int:
    """Words per array: LW 32-bit elements per MEM_DW word (ceil)."""
    return (n + lw - 1) // lw


#: The compute task-body id — the key its firings carry and its subdir in the platform library (a
#: calibrated compute residual is stored under ``components/il_compute_task/``).
IL_COMPUTE_COMPONENT = "il_compute_task"

#: Seed loop-model parameters for the gather compute, ``cycles = latency + ii·(nw − 1)`` — the pipelined
#: word loop's fixed latency and its per-word initiation interval.  Placeholders until a cosim sweep
#: fits them (see the calibration guide); with them the loosely-timed pysim already charges a plausible
#: compute cost instead of zero.
IL_COMPUTE_LATENCY_SEED = 32.0
IL_COMPUTE_II_SEED = 2.0
