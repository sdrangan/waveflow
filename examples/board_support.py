"""Application clock requests and Python-only board host protocol adapters.

This describes model addresses, never a verified hardware ABI. Actual board
register drivers are generated only after parsing a fresh Vitis export.
"""
from dataclasses import asdict

from waveflow.board import ClockRequest

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



# Application defaults, deliberately separate from the board's reference clocks.
BOARD_CLOCK_ARGS = (
    (("--clock-hz",), {"type": float, "default": 100e6,
                       "help": "Requested PS PL0 output frequency in Hz (default 100e6)."}),
    (("--hls-clock-hz",), {"type": float, "default": None,
                           "help": "HLS target frequency in Hz (default: --clock-hz)."}),
    (("--clock-tolerance-ppm",), {"type": float, "default": 100.0,
                                 "help": "Maximum clock-generation error in ppm (default 100)."}),
)


def ps_clock_request(clock_hz=100e6, hls_clock_hz=None, tolerance_ppm=100.0):
    return ClockRequest("ps_pl0", clock_hz,
                        clock_hz if hls_clock_hz is None else hls_clock_hz, tolerance_ppm)
