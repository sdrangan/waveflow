"""The poly schemas as the extracted-flow fixtures use them: frozen, owned by the tests.

These are ``examples/stream_inband``'s schemas as they were before that example moved its
coefficients into the command header (plans/stream_inband_pattern.md, 2026-10).  The
fixtures here test the extractor, ``add_state`` and the body-only code generation -- not the
example's protocol -- so they keep their own copy and their own design (coefficients in the
register map) instead of following the example.

:func:`gen_headers` writes the C++ headers these schemas generate, the way the example's
``HlsGenIncludeStep`` did.
"""
from __future__ import annotations

from enum import IntEnum
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import (
    DataArray, DataList, DataSchemaStep, EnumField, FloatField, IntField,
)
from waveflow.hw.hw_module import HwConst

INCLUDE_DIR = "include"
WORD_BW_SUPPORTED = [32, 64]
TxIdField = IntField.specialize(bitwidth=16, signed=False)
NsampField = IntField.specialize(bitwidth=16, signed=False)
Float32 = FloatField.specialize(bitwidth=32, include_dir=INCLUDE_DIR)


class PolyError(IntEnum):
    NO_ERROR = 0
    TLAST_EARLY_CMD_HDR = 1
    NO_TLAST_CMD_HDR = 2
    TLAST_EARLY_SAMP_IN = 3
    NO_TLAST_SAMP_IN = 4
    WRONG_NSAMP = 5

PolyErrorField = EnumField.specialize(enum_type=PolyError)


class PolyCmdType(IntEnum):
    DATA = 0
    END = 1

PolyCmdTypeField = EnumField.specialize(enum_type=PolyCmdType)


class CoeffArray(DataArray):
    """Array of polynomial coefficients in ascending order (constant term first)."""
    ncoeff: HwConst[int] = 4
    element_type = Float32
    static = True
    max_shape = (ncoeff,)
    cpp_storage = "raw"


class PolyCmdHdr(DataList):
    """Command header: type, transaction ID, and sample count (coefficients are in the regmap)."""
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField, "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,        "description": "Transaction ID"},
        "nsamp":    {"schema": NsampField,       "description": "Sample count (0 for END)"},
    }


class PolyRespHdr(DataList):
    """Response header: echo of the transaction ID."""
    elements = {
        "tx_id": {"schema": TxIdField, "description": "Echo of the transaction ID"},
    }


SCHEMA_CLASSES = [
    PolyErrorField,
    PolyCmdTypeField,
    CoeffArray,
    PolyCmdHdr,
    PolyRespHdr,
]


def gen_headers(root: Path, include_dir: str = INCLUDE_DIR) -> Path:
    """Generate the schema headers, float32 array utilities and stream helpers under ``root``."""
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=include_dir))
    for cls in SCHEMA_CLASSES:
        dag.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED, include_dir=include_dir))
    dag.add(ArrayUtilsStep(Float32, WORD_BW_SUPPORTED))
    failed = [n for n, r in dag.run(BuildConfig(root_dir=root)).items() if not r.success]
    if failed:
        raise RuntimeError(f"header generation failed: {failed}")
    return root / include_dir
