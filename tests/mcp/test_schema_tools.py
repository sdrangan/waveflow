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
