"""schemas.py -- every message of the emitter detection and AoA design (``docs/examples/aoa_detect/arch.md``).

One ``DataList`` per message; every code is an ``EnumField``.  The widths follow ``param.md``: the
energy sum is exact (44 bits), the stored scan power and departure statistic 24 bits signed, a grid
index fits ``L = 256`` directions, a ring slot ``R <= 256`` slots.

=============== ======================== =================================================================
message         from -> to               when
=============== ======================== =================================================================
Notification    detector -> controller   a channel's energy crossed its threshold (a credit was free)
StateMsg        controller -> detector   every notification answered once: the channel's state now
ArrivalJob      controller -> systolic   a search of one channel's block (a.2, a.3)
DepartureJob    controller -> vector     a departure check of one channel's block (b.2)
ArrivalResult   systolic -> controller   the best direction and its stored power, or a late read
DepartureResult vector -> controller     the stored departure statistic, or a late read
ResultMsg       controller -> host       a decision: an arrival detected or a departure declared
=============== ======================== =================================================================

Each pool has its own arbiter, so ``ArrivalResult`` and ``DepartureResult`` arrive on separate streams.
Both still begin with the same tag (``docs/guide/schema/python/tagged_union.md``), which costs nothing
and lets one log or one reader take both: decode :class:`ResultTag` first, then the rest by
:data:`RESULT_SCHEMA`.
"""
from __future__ import annotations

from enum import IntEnum

from waveflow.hw.dataschema import DataList, EnumField, IntField

U8 = IntField.specialize(bitwidth=8, signed=False)
U16 = IntField.specialize(bitwidth=16, signed=False)
U32 = IntField.specialize(bitwidth=32, signed=False)
U48 = IntField.specialize(bitwidth=48, signed=False)   # the exact energy sum (44 bits used)
S24 = IntField.specialize(bitwidth=24, signed=True)    # a stored scan power or departure statistic


class Kind(IntEnum):
    """What a notification asks for, and what a decision is."""

    ARRIVAL = 0
    DEPARTURE = 1


class ChState(IntEnum):
    """A channel's state, as the controller tells the detector (``pending`` is the detector's own)."""

    ABSENT = 0
    ACTIVE = 1


class ReadStatus(IntEnum):
    """Whether a unit's read of its ring slot was intact (the header check, ``arch.md``)."""

    OK = 0
    LATE = 1


KindField = EnumField.specialize(enum_type=Kind, bitwidth=8)
ChStateField = EnumField.specialize(enum_type=ChState, bitwidth=8)
ReadStatusField = EnumField.specialize(enum_type=ReadStatus, bitwidth=8)


class Notification(DataList):
    elements = {
        "tx_id": {"schema": U16, "description": "echoed by the state message that answers it"},
        "block": {"schema": U32, "description": "the block index; the decision time starts at its arrival"},
        "channel": {"schema": U8, "description": "the channel j"},
        "kind": {"schema": KindField, "description": "ARRIVAL (energy above) or DEPARTURE (below)"},
        "slot": {"schema": U8, "description": "the ring slot holding the block"},
        "energy": {"schema": U48, "description": "the channel's exact energy sum over the block"},
    }


class StateMsg(DataList):
    elements = {
        "tx_id": {"schema": U16, "description": "the notification answered"},
        "channel": {"schema": U8, "description": "the channel j"},
        "state": {"schema": ChStateField, "description": "the channel's state from now on"},
    }


class ArrivalJob(DataList):
    elements = {
        "tx_id": {"schema": U16, "description": "echoed in the result"},
        "channel": {"schema": U8, "description": "which channel's region of the slot to read"},
        "slot": {"schema": U8, "description": "the ring slot"},
        "block": {"schema": U32, "description": "the block index, checked against the slot's header"},
    }


class DepartureJob(DataList):
    elements = {
        "tx_id": {"schema": U16, "description": "echoed in the result"},
        "channel": {"schema": U8, "description": "which channel's region of the slot to read"},
        "slot": {"schema": U8, "description": "the ring slot"},
        "block": {"schema": U32, "description": "the block index, checked against the slot's header"},
        "ell": {"schema": U16, "description": "the tracked direction's grid index"},
    }


class ResultTag(DataList):
    """What the controller decodes first from the merged result stream."""

    elements = {"kind": {"schema": KindField, "description": "which unit type answered"}}


class ArrivalResult(DataList):
    elements = {
        "kind": {"schema": KindField, "description": "ARRIVAL"},
        "tx_id": {"schema": U16, "description": "the job's"},
        "status": {"schema": ReadStatusField, "description": "LATE: the slot was overwritten during the read"},
        "ell": {"schema": U16, "description": "the direction of the largest normalized power"},
        "p": {"schema": S24, "description": "its stored scan power"},
    }


class DepartureResult(DataList):
    elements = {
        "kind": {"schema": KindField, "description": "DEPARTURE"},
        "tx_id": {"schema": U16, "description": "the job's"},
        "status": {"schema": ReadStatusField, "description": "LATE: the slot was overwritten during the read"},
        "pb": {"schema": S24, "description": "the stored departure statistic"},
    }


RESULT_SCHEMA = {Kind.ARRIVAL: ArrivalResult, Kind.DEPARTURE: DepartureResult}


class ResultMsg(DataList):
    elements = {
        "channel": {"schema": U8, "description": "the channel j"},
        "block": {"schema": U32, "description": "the block the decision came from"},
        "kind": {"schema": KindField, "description": "ARRIVAL detected or DEPARTURE declared"},
        "ell": {"schema": U16, "description": "the direction's grid index (arrival)"},
        "p": {"schema": S24, "description": "the stored scan power, for the SNR estimate (arrival)"},
    }
