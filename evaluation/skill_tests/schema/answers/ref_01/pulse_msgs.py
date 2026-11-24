"""Reference answer, 01_pulse_report: the messages."""
from enum import IntEnum

from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.fixpoint import FixedField

U16 = IntField.specialize(bitwidth=16, signed=False)
Thresh = FixedField.specialize(16, 4, signed=False)   # ufixed<16, 4>
Samp = FixedField.specialize(12, 1, include_dir="include")   # fixed<12, 1>; its array utils go to include/


class Op(IntEnum):
    DETECT = 0
    END = 1


class Kind(IntEnum):
    START = 0
    PEAK = 1
    DONE = 2


OpField = EnumField.specialize(enum_type=Op, bitwidth=8)
KindField = EnumField.specialize(enum_type=Kind, bitwidth=8)


class PulseCmd(DataList):
    elements = {
        "op":     {"schema": OpField, "description": "DETECT or END"},
        "tx_id":  {"schema": U16,     "description": "echoed in the reports"},
        "nsamp":  {"schema": U16,     "description": "samples that follow"},
        "thresh": {"schema": Thresh,  "description": "detection threshold"},
    }


class Start(DataList):
    elements = {"kind": KindField, "tx_id": U16}


class Peak(DataList):
    elements = {"kind": KindField, "index": U16, "value": Samp}


class Done(DataList):
    elements = {"kind": KindField, "tx_id": U16, "count": U16}


class Tag(DataList):
    elements = {"kind": KindField}


SCHEMA = {Kind.START: Start, Kind.PEAK: Peak, Kind.DONE: Done}
SCHEMA_CLASSES = [OpField, KindField, PulseCmd, Start, Peak, Done, Tag]
