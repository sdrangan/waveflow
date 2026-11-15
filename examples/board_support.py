"""Python-only register adapter for the board examples' host protocol checks.

This describes model addresses, never a verified hardware ABI. Actual board
register drivers are generated only after parsing a fresh Vitis export.
"""
from dataclasses import asdict

from waveflow.build.hls_control import ControlField
from waveflow.build._host_driver_template import HlsRegisterDriver
from waveflow.hw.dataschema import IntField


def model_abi(regmap):
    fields = []
    for name, field in regmap._fields.items():
        offset = regmap.offset_of(name)
        encoding = ("int" if issubclass(field.schema, IntField)
                    and field.schema.signed else "uint")
        fields.append(ControlField(
            name, "control" if offset == 0 else "scalar", offset,
            field.schema.get_bitwidth(), "R" if field.access.value == "R" else "RW",
            bit_offset=field.bit_offset or 0, encoding=encoding))
    fields.append(ControlField("auto_restart", "control", 0, 1, "RW", bit_offset=7))
    return {"validation_level": "python_model_only",
            "fields": [asdict(f) for f in fields]}


def model_driver(backend):
    return HlsRegisterDriver(backend, model_abi(backend.dut.regmap))
