"""Independent S0c fixture intent and an observable fake register backend."""
from pathlib import Path

from waveflow.hw.dataschema import FloatField, IntField
from waveflow.hw.regmap import RegAccess, RegField, VitisRegMap

FIXTURES = Path(__file__).parent / "fixtures" / "control_abi"
TOPS = ("scalar_probe", "array_probe", "pointer_probe")
U32 = IntField.specialize(bitwidth=32, signed=False)
I32 = IntField.specialize(bitwidth=32, signed=True)
U64 = IntField.specialize(bitwidth=64, signed=False)


def intent(top):
    if top == "scalar_probe":
        # Explicit measured offsets are design intent, not patched into the parser.
        # The fixture pins Vitis output placement independently of model defaults.
        return VitisRegMap({
            "x": RegField(I32, RegAccess.RW),
            "gain": RegField(FloatField, RegAccess.RW),
            "wide": RegField(U64, RegAccess.RW),
            "y": RegField(I32, RegAccess.R, offset=0x2c),
        }), {}
    if top == "array_probe":
        return VitisRegMap({"sum": RegField(U32, RegAccess.R)}), {}
    if top == "pointer_probe":
        return VitisRegMap({
            "count": RegField(U32, RegAccess.RW, offset=0x1c),
            "sum": RegField(U32, RegAccess.R, offset=0x24),
        }), {"buffer_r": "m_axi_gmem"}
    raise ValueError(top)


class FakeRegisters:
    def __init__(self):
        self.words = {}
        self.reads = []
        self.writes = []
        self.cor = {}
        self.tow = set()

    def read_registers(self, offset, count):
        self.reads.append((offset, count))
        result = []
        for address in range(offset, offset + count * 4, 4):
            result.append(self.words.get(address, 0))
            self.words[address] = self.words.get(address, 0) & ~self.cor.get(address, 0)
        return result

    def write_registers(self, offset, values):
        self.writes.append((offset, list(values)))
        for i, word in enumerate(values):
            address = offset + i * 4
            if address in self.tow:
                self.words[address] = self.words.get(address, 0) ^ word
            else:
                self.words[address] = word
