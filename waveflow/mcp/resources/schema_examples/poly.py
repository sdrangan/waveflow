"""
Curated schema definitions from the polynomial accelerator example.

These are the data schemas of examples/stream_inband/poly.py, for use as teaching examples.
They follow that example's command-response contract: every DATA command header carries the
coefficients it is evaluated with, the response header echoes the transaction ID, and the
error codes are reported through the kernel's status registers -- never in a response footer.
"""
from __future__ import annotations

from enum import IntEnum

from waveflow.hw.dataschema import DataArray, DataList, EnumField, FloatField, IntField


INCLUDE_DIR = "include"

# ---------------------------------------------------------------------------
# Reusable field specializations
# ---------------------------------------------------------------------------

TxIdField = IntField.specialize(bitwidth=16, signed=False)
NsampField = IntField.specialize(bitwidth=16, signed=False)
Float32 = FloatField.specialize(bitwidth=32, include_dir=INCLUDE_DIR)

# ---------------------------------------------------------------------------
# Enums: the command type, and the error codes reported in the status registers
# ---------------------------------------------------------------------------


class PolyCmdType(IntEnum):
    DATA = 0  # a command header followed by nsamp samples
    END = 1   # ends the kernel run


PolyCmdTypeField = EnumField.specialize(enum_type=PolyCmdType, include_dir=INCLUDE_DIR)


class PolyError(IntEnum):
    NO_ERROR = 0
    TLAST_EARLY_SAMP_IN = 1  # TLAST arrived before the last sample word
    NO_TLAST_SAMP_IN = 2     # the last sample word had no TLAST


PolyErrorField = EnumField.specialize(enum_type=PolyError, include_dir=INCLUDE_DIR)

# ---------------------------------------------------------------------------
# DataArray: polynomial coefficients
# ---------------------------------------------------------------------------


class CoeffArray(DataArray):
    """
    Array of polynomial coefficients, stored in ascending order (constant term first).
    For example, for a cubic polynomial c0 + c1*x + c2*x^2 + c3*x^3, the array would be
    [c0, c1, c2, c3].
    """

    ncoeff: int = 4
    element_type = Float32
    static = True
    max_shape = (ncoeff,)
    include_dir = INCLUDE_DIR


# ---------------------------------------------------------------------------
# Command and response schemas
# ---------------------------------------------------------------------------


class PolyCmdHdr(DataList):
    """
    Command header sent to the accelerator on its input stream.  A DATA command carries its
    transaction ID, the number of samples that follow, and the coefficients to evaluate them
    with -- so every command is self-contained and nothing is configured over AXI-Lite.
    """

    elements = {
        "cmd_type": {
            "schema": PolyCmdTypeField,
            "description": "DATA or END",
        },
        "tx_id": {
            "schema": TxIdField,
            "description": "Command ID: echoed, or reported on error",
        },
        "nsamp": {
            "schema": NsampField,
            "description": "Sample count (0 for END)",
        },
        "coeffs": {
            "schema": CoeffArray,
            "description": "c0..c3, constant term first",
        },
    }
    include_dir = INCLUDE_DIR


class PolyRespHdr(DataList):
    """
    Response header sent back from the accelerator, containing an echo of the
    transaction ID from the command header.  This allows the host to correlate
    responses with the commands that generated them.
    """

    elements = {
        "tx_id": {
            "schema": TxIdField,
            "description": "Echo of the DATA command's tx_id",
        },
    }
    include_dir = INCLUDE_DIR
