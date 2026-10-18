"""``waveflow_validate_schema`` reports the schemas a text DEFINES, not the ones it imports.

Found by the rotate blind test (PR #209): a file importing ``PolyCmdHdr`` from the
reference design to compare against had that class validated and reported as its own.
"""
from __future__ import annotations

from waveflow.mcp.schema_tools import validate_schema

TEXT = '''
from waveflow.hw import DataList, IntField
from examples.stream_inband.poly import PolyCmdHdr, PolyRespHdr as Borrowed
from examples.stream_inband.poly import TxIdField


class Mine(DataList):
    elements = {"a": {"schema": TxIdField, "description": "a"}}
'''


def test_imported_schemas_are_not_reported_as_defined() -> None:
    result = validate_schema(TEXT)
    assert result["valid"], result["errors"]
    assert result["schema_info"]["name"] == "Mine"
    # Before the fix the three imports were "defined" too: a multiple-classes warning,
    # and schema_info could describe a class the text never wrote.
    assert not any(w["code"] == "multiple_schema_classes" for w in result["warnings"])


DATACLASS_TEXT = '''
from dataclasses import dataclass

from waveflow.hw import DataList, IntField

U16 = IntField.specialize(bitwidth=16, signed=False)


class Cmd(DataList):
    elements = {"tx_id": U16, "n": U16}


@dataclass
class Intent:
    n: int
'''


def test_a_text_with_a_dataclass_validates() -> None:
    """Found by the rung-1 build blind test: the text ran in a bare namespace, so ``@dataclass`` (which
    looks its class's module up in ``sys.modules``) failed with ``'NoneType' object has no attribute
    '__dict__'``."""
    result = validate_schema(DATACLASS_TEXT)
    assert result["valid"], result["errors"]
    assert result["schema_info"]["name"] == "Cmd"
