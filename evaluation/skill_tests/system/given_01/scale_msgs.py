"""The messages of the gain-and-shift offload, and its golden.

Every message is a schema; nothing below packs a word by hand.  Addresses are **word** coordinates (the
memory reader's and writer's ``addr``), and a buffer starts on a word: the host allocates it so.
"""
from __future__ import annotations

from enum import IntEnum

import numpy as np

from waveflow.hw.arrayutils import get_nwords
from waveflow.hw.dataschema import DataList, EnumField, IntField

#: The bus and memory word: four int16 samples a word.
DW = 64
#: The longest job the device takes, in samples; a longer one is a fatal error (the spec's limit).
NMAX = 2048

U8 = IntField.specialize(bitwidth=8, signed=False)
U16 = IntField.specialize(bitwidth=16, signed=False)
U32 = IntField.specialize(bitwidth=32, signed=False)
I16 = IntField.specialize(bitwidth=16, signed=True)


class Code(IntEnum):
    OK = 0
    TOO_LONG = 1        # n > NMAX: fatal, nothing read or written


CodeField = EnumField.specialize(enum_type=Code)


class ScaleCmd(DataList):
    """One job, host -> device (its command queue)."""
    elements = {
        "tx_id": {"schema": U16, "description": "the host's job id, echoed in the status"},
        "n":     {"schema": U16, "description": "samples, 1..NMAX"},
        "src":   {"schema": U32, "description": "input buffer, a word address"},
        "dst":   {"schema": U32, "description": "output buffer, a word address"},
        "gain":  {"schema": I16, "description": "signed gain"},
        "shift": {"schema": U8,  "description": "right shift after the multiply, 0..15"},
    }


class ScaleStatus(DataList):
    """One per job, device -> host, once the output is in memory (or the job was refused)."""
    elements = {
        "tx_id": {"schema": U16},
        "code":  {"schema": CodeField},
        "nsat":  {"schema": U16, "description": "samples that saturated"},
    }


def scale(x: np.ndarray, gain: int, shift: int) -> tuple[np.ndarray, int]:
    """The golden: ``y = sat16((x * gain) >> shift)`` (arithmetic shift, floor), and how many
    saturated."""
    v = (x.astype(np.int64) * int(gain)) >> int(shift)
    y = np.clip(v, -32768, 32767)
    return y.astype(np.int16), int(np.count_nonzero(y != v))


def nwords(n: int) -> int:
    """Words a buffer of *n* samples spans."""
    return int(get_nwords(I16, word_bw=DW, shape=n))


__all__ = ["DW", "NMAX", "Code", "ScaleCmd", "ScaleStatus", "I16", "scale", "nwords"]
