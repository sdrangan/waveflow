from enum import IntEnum
from pathlib import Path

import numpy as np
import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
from waveflow.hw import (
    BooleanField as PublicBooleanField,
    DataArray as PublicDataArray,
    DataField as PublicDataField,
    DataList as PublicDataList,
    DataSchema as PublicDataSchema,
    EnumField as PublicEnumField,
    FloatField as PublicFloatField,
    IntField as PublicIntField,
    MemAddr as PublicMemAddr,
)
from waveflow.hw.dataschema import (
    BooleanField,
    DataArray,
    DataField,
    DataList,
    DataSchema,
    EnumField,
    FloatField,
    IntField,
    MemAddr,
    VarDataArray,
)


class Mode(IntEnum):
    OFF = 0
    ON = 1
    AUTO = 2


class OpCode(IntEnum):
    NOP = 0
    ADD = 1


U16 = IntField.specialize(bitwidth=16, signed=False)
U8 = IntField.specialize(bitwidth=8, signed=False)
S16 = IntField.specialize(bitwidth=16, signed=True)
F32 = FloatField.specialize(bitwidth=32)
Addr64 = MemAddr.specialize(bitwidth=64)
ModeField = EnumField.specialize(enum_type=Mode, default=Mode.AUTO)
CoeffArray = DataArray.specialize(element_type=F32, max_shape=(4,), member_name="coeff")
ByteArray = DataArray.specialize(element_type=U8, max_shape=(8,), member_name="x")
WordMatrix = DataArray.specialize(element_type=U16, max_shape=(4, 4), member_name="x")
DynByteArray = DataArray.specialize(element_type=U8, max_shape=(16,), static=False, member_name="x")


class Complex(DataList):
    elements = {
        "real": S16,
        "imag": S16,
    }


class Packet(DataList):
    elements = {
        "count": U16,
        "gain": F32,
        "mode": ModeField,
        "z": Complex,
    }


class PacketWithArray(DataList):
    elements = {
        "count": U16,
        "coeffs": CoeffArray,
    }


class MultiArrayPacket(DataList):
    elements = {
        "a": CoeffArray,
        "b": CoeffArray,
        "uab": CoeffArray,
    }


class DescribedComplex(DataList):
    elements = {
        "real": {
            "schema": F32,
            "description": "Real component",
        },
        "imag": {
            "schema": F32,
            "description": "Imaginary component of the complex sample in Q-format form.",
        },
    }


def test_intfield_specialize_same_args_returns_same_class():
    a = IntField.specialize(bitwidth=16, signed=False)
    b = IntField.specialize(bitwidth=16, signed=False)

    assert a is b


def test_intfield_specialize_include_metadata_affects_cache_key():
    a = IntField.specialize(bitwidth=16, signed=False, include_dir="a")
    b = IntField.specialize(bitwidth=16, signed=False, include_dir="b")

    assert a is not b
    assert a.include_dir == "a"
    assert b.include_dir == "b"


def test_memaddr_specialize_same_args_returns_same_class():
    a = MemAddr.specialize(bitwidth=64)
    b = MemAddr.specialize(bitwidth=64)

    assert a is b


def test_memaddr_specialize_supports_arbitrary_bitwidths():
    addr48 = MemAddr.specialize(bitwidth=48)
    addr128 = MemAddr.specialize(bitwidth=128)

    assert issubclass(addr48, MemAddr)
    assert issubclass(addr128, MemAddr)
    assert addr48.get_bitwidth() == 48
    assert addr128.get_bitwidth() == 128
    assert addr48.cpp_class_name() == "ap_uint<48>"
    assert addr128.cpp_class_name() == "ap_uint<128>"
    assert addr48.signed is False
    assert addr128.signed is False


def test_memaddr_init_value_defaults_to_unsigned_zero():
    init = Addr64.init_value()

    assert isinstance(init, np.uint64)
    assert int(init) == 0


def test_memaddr_roundtrip_wide_value():
    Addr96 = MemAddr.specialize(bitwidth=96)
    raw_value = (1 << 95) + 0x1234_5678_9ABC_DEF0

    addr = Addr96(raw_value)
    packed = addr.serialize(word_bw=32)
    restored = Addr96().deserialize(packed, word_bw=32)

    assert packed.dtype == np.uint32
    assert packed.shape == (3,)
    assert int(restored.val[0]) == (raw_value & 0xFFFFFFFFFFFFFFFF)
    assert int(restored.val[1]) == ((raw_value >> 64) & 0xFFFFFFFFFFFFFFFF)
    assert restored.is_close(addr)


def test_memaddr_masks_values_as_unsigned():
    Addr12 = MemAddr.specialize(bitwidth=12)
    addr = Addr12(-1)

    assert int(addr.val) == 0xFFF


def test_init_value_semantics():
    uint16_type = IntField.specialize(bitwidth=16, signed=False)
    float32_type = FloatField.specialize(bitwidth=32)
    enum_type = EnumField.specialize(enum_type=Mode, default=Mode.ON)

    int_init = uint16_type.init_value()
    float_init = float32_type.init_value()
    enum_init = enum_type.init_value()

    assert isinstance(int_init, np.uint32)
    assert int(int_init) == 0
    assert isinstance(float_init, np.float32)
    assert float(float_init) == pytest.approx(0.0)
    assert enum_init is Mode.ON


def test_enumfield_defaults_follow_enum_type_metadata():
    mode_field = EnumField.specialize(enum_type=Mode)
    opcode_field = EnumField.specialize(enum_type=OpCode)

    assert mode_field.cpp_class_name() == "Mode"
    assert mode_field.resolved_include_filename() == "mode.h"
    assert mode_field.resolved_tb_include_filename() == "mode_tb.h"
    assert opcode_field.cpp_class_name() == "OpCode"
    assert opcode_field.resolved_include_filename() == "op_code.h"
    assert opcode_field.resolved_tb_include_filename() == "op_code_tb.h"


def test_enumfield_gen_include_emits_guard_and_members(tmp_path: Path):
    mode_field = EnumField.specialize(enum_type=Mode)
    result = mode_field.as_buildable().run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]
    content = out_path.read_text(encoding="utf-8")
    tb_content = result.artifacts["tb_include"].read_text(encoding="utf-8")

    assert out_path == tmp_path / "mode.h"
    assert "#ifndef MODE_H" in content
    assert "#define MODE_H" in content
    assert "enum class Mode" in content
    assert "OFF = 0," in content
    assert "ON = 1," in content
    assert "AUTO = 2," in content
    assert "#endif // MODE_H" in content
    assert "enum_to_string(Mode value)" not in content
    assert '#include "mode.h"' in tb_content
    assert "inline const char* enum_to_string(Mode value) {" in tb_content
    assert 'case Mode::OFF:' in tb_content
    assert 'return "OFF";' in tb_content
    assert 'case Mode::AUTO:' in tb_content
    assert 'return "AUTO";' in tb_content
    assert 'return "UNKNOWN";' in tb_content


def test_enumfield_explicit_overrides_win():
    mode_field = EnumField.specialize(
        enum_type=Mode,
        cpp_repr="CustomMode",
        include_filename="custom_mode.h",
    )

    assert mode_field.cpp_class_name() == "CustomMode"
    assert mode_field.resolved_include_filename() == "custom_mode.h"
    assert mode_field.resolved_tb_include_filename() == "custom_mode_tb.h"


# --- BooleanField -------------------------------------------------------------
def test_booleanfield_construction_and_defaults():
    assert BooleanField.get_bitwidth() == 1
    assert BooleanField.signed is False
    assert BooleanField.cpp_class_name() == "ap_uint<1>"
    assert BooleanField.can_gen_include is False
    assert issubclass(BooleanField, IntField)

    init = BooleanField.init_value()
    assert init is False and isinstance(init, bool)
    assert BooleanField().val is False
    assert PublicBooleanField is BooleanField


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True), (False, False),
        (1, True), (0, False),
        (np.int64(1), True), (np.uint8(0), False),
        (np.bool_(True), True), (np.bool_(False), False),
    ],
)
def test_booleanfield_coerces_to_python_bool(value, expected):
    field = BooleanField(value)
    assert field.val is expected            # exact Python bool singleton
    assert isinstance(field.val, bool)


@pytest.mark.parametrize("bad", [2, -1, 255, 1.0, 0.0, "1"])
def test_booleanfield_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        BooleanField(bad)
    with pytest.raises(ValueError):
        BooleanField().val = bad        # also rejected on direct assignment


@pytest.mark.parametrize("word_bw", [32, 64])
@pytest.mark.parametrize("value", [True, False])
def test_booleanfield_serialize_deserialize_roundtrip(value, word_bw):
    packed = BooleanField(value).serialize(word_bw=word_bw)
    assert int(packed[0]) == (1 if value else 0)          # exact 1-bit payload
    restored = BooleanField().deserialize(packed, word_bw=word_bw)
    assert restored.val is value


def test_booleanfield_one_bit_width_packs_tightly():
    # Three booleans + a byte pack into a single 32-bit word (3 + 8 = 11 bits used).
    class Flags(DataList):
        elements = {"a": BooleanField, "b": BooleanField, "c": BooleanField, "n": U8}

    flags = Flags(a=True, b=False, c=True, n=200)
    packed = flags.serialize(word_bw=32)
    assert packed.shape == (1,)
    restored = Flags().deserialize(packed, word_bw=32)
    assert restored.val == {"a": True, "b": False, "c": True, "n": np.uint32(200)}


def test_booleanfield_specialize_caches_and_honors_overrides():
    assert BooleanField.specialize() is BooleanField.specialize()
    a = BooleanField.specialize(include_dir="a")
    b = BooleanField.specialize(include_dir="b")
    assert a is not b
    assert a.get_bitwidth() == 1 and a.cpp_class_name() == "ap_uint<1>"
    assert a.include_dir == "a" and b.include_dir == "b"


def test_booleanfield_codegen_emits_ap_uint1(tmp_path: Path):
    class Toggle(DataList):
        elements = {"enable": BooleanField, "count": U8}

    result = Toggle.as_buildable(word_bw_supported=[32]).run(BuildConfig(root_dir=tmp_path))
    content = result.artifacts["include"].read_text(encoding="utf-8")
    assert "ap_uint<1> enable;" in content                # exact 1-bit struct member
    assert "res.range(0, 0) = data.enable;" in content    # packs into a single bit


def test_inline_enum_specialization_keeps_expected_metadata():
    class Instruction(DataList):
        elements = {
            "mode": EnumField.specialize(enum_type=Mode),
        }

    mode_schema = Instruction.elements["mode"]
    assert mode_schema.cpp_class_name() == "Mode"
    assert mode_schema.resolved_include_filename() == "mode.h"
    assert mode_schema.resolved_tb_include_filename() == "mode_tb.h"


def test_datalist_get_dependencies_only_returns_generated_types():
    deps = Packet.get_dependencies()

    assert deps == [ModeField, Complex]
    assert U16 not in deps
    assert F32 not in deps


def test_datalist_backward_compatible_elements_form_still_works():
    packet = Packet(count=5, gain=2.0)

    assert isinstance(packet.z, Complex)
    assert int(packet.count) == 5
    assert float(packet.gain) == pytest.approx(2.0)


def test_datalist_metadata_form_initializes_and_assigns_normally():
    sample = DescribedComplex(real=1.5, imag=2.5)

    assert isinstance(sample.real, np.float32)
    assert isinstance(sample.imag, np.float32)
    assert float(sample.real) == pytest.approx(1.5)
    assert float(sample.imag) == pytest.approx(2.5)


def test_datalist_serialize_deserialize_roundtrip_word32():
    packet = Packet(count=7, gain=3.5, mode=Mode.ON)
    packet.z.real = -5
    packet.z.imag = 9

    packed = packet.serialize(word_bw=32)
    restored = Packet().deserialize(packed, word_bw=32)

    assert packed.dtype == np.uint32
    assert packed.shape == (4,)
    assert restored.is_close(packet)


def test_datalist_serialize_deserialize_roundtrip_word128():
    packet = Packet(count=11, gain=1.25, mode=Mode.AUTO)
    packet.z.real = -2
    packet.z.imag = 4

    packed = packet.serialize(word_bw=128)
    restored = Packet().deserialize(packed, word_bw=128)

    assert packed.dtype == np.uint64
    assert packed.shape == (1, 2)
    assert restored.is_close(packet)


def test_described_complex_serialize_deserialize_roundtrip():
    sample = DescribedComplex(real=1.5, imag=2.5)

    packed = sample.serialize(word_bw=64)
    restored = DescribedComplex().deserialize(packed, word_bw=64)

    assert packed.dtype == np.uint64
    assert packed.shape == (1,)
    assert restored.is_close(sample)


def test_serialize_rejects_non_positive_word_width():
    with pytest.raises(ValueError, match="word_bw must be positive"):
        Packet().serialize(word_bw=0)


def test_deserialize_rejects_invalid_shape_for_large_word_width():
    with pytest.raises(ValueError, match="packed must be a 2D array-like"):
        Packet().deserialize(np.array([1], dtype=np.uint64), word_bw=128)


def test_write_uint32_file_and_read_uint32_file_roundtrip(tmp_path: Path):
    packet = Packet(count=13, gain=2.75, mode=Mode.ON)
    packet.z.real = -7
    packet.z.imag = 12

    out_path = packet.write_uint32_file(tmp_path / "packet.bin")
    restored = Packet().read_uint32_file(out_path)

    assert out_path == tmp_path / "packet.bin"
    assert out_path.exists()
    assert restored.is_close(packet)


def test_write_uint32_file_creates_parent_directories(tmp_path: Path):
    packet = Packet(count=1)
    out_path = packet.write_uint32_file(tmp_path / "nested" / "dir" / "packet.bin")

    assert out_path.exists()


def test_dataarray_init_value_and_assignment():
    arr = ByteArray()

    assert isinstance(arr.val, np.ndarray)
    assert arr.val.shape == (8,)
    assert arr.val.dtype == np.uint32

    arr.val = [1, 2, 3, 4, 5, 6, 7, 8]
    assert np.array_equal(arr.val, np.asarray([1, 2, 3, 4, 5, 6, 7, 8], dtype=np.uint32))


def test_dynamic_dataarray_init_value_has_zero_length_first_axis():
    arr = DynByteArray()

    assert isinstance(arr.val, np.ndarray)
    assert arr.val.shape == (0,)
    assert arr.val.dtype == np.uint32


def test_dataarray_serialize_deserialize_roundtrip():
    arr = ByteArray()
    arr.val = np.asarray([1, 2, 3, 4, 5, 6, 7, 8], dtype=np.uint32)

    packed = arr.serialize(word_bw=32)
    restored = ByteArray().deserialize(packed, word_bw=32)

    assert packed.dtype == np.uint32
    assert packed.shape == (2,)
    assert restored.is_close(arr)


def test_dataarray_write_uint32_file_nwrite(tmp_path: Path):
    arr = ByteArray()
    arr.val = np.asarray([1, 2, 3, 4, 5, 6, 7, 8], dtype=np.uint32)

    out_path = tmp_path / "arr_nwrite.bin"
    arr.write_uint32_file(out_path, nwrite=4)

    file_words = np.fromfile(out_path, dtype="<u4")
    ref = DataArray.specialize(element_type=U8, max_shape=(4,), member_name="x")()
    ref.val = np.asarray([1, 2, 3, 4], dtype=np.uint32)
    expected_words = np.asarray(ref.serialize(word_bw=32), dtype="<u4")

    assert np.array_equal(file_words, expected_words)


def test_dataarray_write_uint32_file_write_slice(tmp_path: Path):
    arr = WordMatrix()
    arr.val = np.arange(16, dtype=np.uint32).reshape(4, 4)

    out_path = tmp_path / "arr_slice.bin"
    arr.write_uint32_file(out_path, write_slice=np.s_[:2, 1:3])

    file_words = np.fromfile(out_path, dtype="<u4")
    ref = DataArray.specialize(element_type=U16, max_shape=(2, 2), member_name="x")()
    ref.val = np.asarray(arr.val[:2, 1:3], dtype=np.uint32)
    expected_words = np.asarray(ref.serialize(word_bw=32), dtype="<u4")

    assert np.array_equal(file_words, expected_words)


def test_dataarray_gen_include_emits_nwords_len_for_dynamic(tmp_path: Path):
    result = DynByteArray.as_buildable(word_bw_supported=[32, 64]).run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]
    content = out_path.read_text(encoding="utf-8")

    assert out_path == tmp_path / "u_int8_array.h"
    assert "static int nwords_len(int n0=1) {" in content
    assert "static int nwords_len_impl(word_bw_tag<32>, int n0=1) {" in content
    assert "const int n0_eff = (n0 < 0) ? 0 : ((n0 > 16) ? 16 : n0);" in content
    assert "return (n_total + 4 - 1) / 4;" in content


def test_dataarray_gen_include_emits_runtime_shape_params(tmp_path: Path):
    result = DynByteArray.as_buildable(word_bw_supported=[32]).run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]
    content = out_path.read_text(encoding="utf-8")

    assert "void write_array(ap_uint<word_bw> x[], int n0=1) const {" in content
    assert "void read_array(const ap_uint<word_bw> x[], int n0=1) {" in content


def test_dataarray_stream_codegen_does_not_redeclare_word_buffer():
    coeff_write = CoeffArray.gen_write(word_bw=32, dst_type="stream")
    coeff_read = CoeffArray.gen_read(word_bw=32, src_type="stream")
    matrix_write = WordMatrix.gen_write(word_bw=32, dst_type="stream")
    matrix_read = WordMatrix.gen_read(word_bw=32, src_type="stream")

    assert coeff_write.count("ap_uint<32> w = 0;") == 1
    assert coeff_read.count("ap_uint<32> w = 0;") == 1
    assert matrix_write.count("ap_uint<32> w = 0;") == 1
    assert matrix_read.count("ap_uint<32> w = 0;") == 1


def test_datalist_with_array_field_assigns_and_roundtrips():
    packet = PacketWithArray(count=3)
    packet.coeffs = np.asarray([0.25, 0.5, 0.75, 1.0], dtype=np.float32)

    packed = packet.serialize(word_bw=32)
    restored = PacketWithArray().deserialize(packed, word_bw=32)

    assert np.asarray(packet.coeffs).shape == (4,)
    assert restored.is_close(packet, rel_tol=1e-6, abs_tol=1e-6)


def test_datalist_to_dict_from_dict_roundtrip():
    packet = Packet(count=9, gain=1.5, mode=Mode.ON)
    packet.z.real = -3
    packet.z.imag = 8

    payload = packet.to_dict()
    restored = Packet().from_dict(payload)

    assert payload == {
        "count": np.uint32(9),
        "gain": np.float32(1.5),
        "mode": Mode.ON,
        "z": {
            "real": np.int32(-3),
            "imag": np.int32(8),
        },
    }
    assert restored.is_close(packet)


def test_datalist_to_json_from_json_roundtrip(tmp_path: Path):
    packet = Packet(count=31, gain=2.25, mode=Mode.AUTO)
    packet.z.real = -17
    packet.z.imag = 6

    json_path = tmp_path / "packet.json"
    json_str = packet.to_json(file_path=json_path)

    restored = Packet().from_json(json_str)
    restored2 = Packet().from_json(json_path)

    assert json_path.exists()
    assert '"count": 31' in json_str
    assert '"mode": 2' in json_str
    assert restored.is_close(packet)
    assert restored2.is_close(packet)


def test_datalist_val_accepts_same_type_instance():
    src = Packet(count=4, gain=1.25, mode=Mode.ON)
    src.z.real = -2
    src.z.imag = 5

    dst = Packet()
    dst.val = src

    assert dst.is_close(src)


def test_datalist_element_normalization_accessors_work_for_both_forms():
    assert Complex.get_element_schema("real") is S16
    assert Complex.get_element_description("real") is None
    assert Complex.get_element_definition("real") == {
        "schema": S16,
        "description": None,
    }

    assert DescribedComplex.get_element_schema("real") is F32
    assert DescribedComplex.get_element_description("real") == "Real component"
    assert DescribedComplex.get_element_definition("imag") == {
        "schema": F32,
        "description": "Imaginary component of the complex sample in Q-format form.",
    }


def test_datalist_metadata_validation_rejects_missing_schema():
    class BadList(DataList):
        elements = {
            "x": {"description": "missing schema"},
        }

    with pytest.raises(TypeError, match="must define a 'schema' entry"):
        BadList.get_bitwidth()


def test_datalist_metadata_validation_rejects_unknown_keys():
    class BadList(DataList):
        elements = {
            "x": {"schema": U16, "units": "V"},
        }

    with pytest.raises(TypeError, match="unsupported metadata key"):
        BadList.get_bitwidth()


def test_datalist_metadata_validation_rejects_invalid_schema_value():
    class BadList(DataList):
        elements = {
            "x": {"schema": 123},
        }

    with pytest.raises(TypeError, match="must be a DataSchema subclass"):
        BadList.get_bitwidth()


def test_datalist_get_dependencies_works_with_metadata_form():
    class WithMetadataDeps(DataList):
        elements = {
            "mode": {
                "schema": ModeField,
                "description": "Mode field",
            },
            "nested": {
                "schema": Complex,
                "description": "Nested complex sample",
            },
            "gain": {
                "schema": F32,
                "description": "Local gain",
            },
        }

    assert WithMetadataDeps.get_dependencies() == [ModeField, Complex]


def test_datalist_gen_include_emits_dependency_includes_and_members(tmp_path: Path):
    result = Packet.as_buildable().run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]
    content = out_path.read_text(encoding="utf-8")
    tb_content = result.artifacts["tb_include"].read_text(encoding="utf-8")

    assert out_path == tmp_path / "packet.h"
    assert '#include "streamutils_hls.h"' in content
    assert '#include "streamutils_tb.h"' not in content
    assert "#ifndef PACKET_H" in content
    assert '#include "mode.h"' in content
    assert '#include "complex.h"' in content
    assert "struct Packet {" in content
    assert "ap_uint<16> count;" in content
    assert "float gain;" in content
    assert "Mode mode;" in content
    assert "Complex z;" in content
    assert "static constexpr int bitwidth = 82;" in content
    assert "static ap_uint<bitwidth> pack_to_uint(const Packet& data) {" in content
    assert "static Packet unpack_from_uint(const ap_uint<bitwidth>& packed) {" in content
    assert "#endif // PACKET_H" in content
    assert '#include "streamutils_tb.h"' in tb_content
    assert '#include "mode_tb.h"' in tb_content
    assert '#include "complex_tb.h"' in tb_content
    assert "inline void Packet::dump_json(std::ostream& os, int indent, int level) const {" in tb_content
    assert "inline void Packet::load_json(std::istream& is) {" in tb_content
    assert "inline void Packet::dump_json_file(const char* file_path, int indent) const {" in tb_content
    assert "inline void Packet::load_json_file(const char* file_path) {" in tb_content


def test_datalist_gen_include_emits_inline_and_block_comments(tmp_path: Path):
    result = DescribedComplex.as_buildable().run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]
    content = out_path.read_text(encoding="utf-8")

    assert "float real;  // Real component" in content
    assert "// Imaginary component of the complex sample in Q-format form." in content
    assert "float imag;" in content


def test_datalist_gen_include_emits_read_helpers_when_requested(tmp_path: Path):
    result = Packet.as_buildable(word_bw_supported=[32]).run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]
    content = out_path.read_text(encoding="utf-8")

    assert "template<int word_bw>" in content
    assert "void write_array(ap_uint<word_bw> x[]) const {" in content
    assert "void write_stream(hls::stream<ap_uint<word_bw>> &s) const {" in content
    assert "void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {" in content
    assert "x[0].range(15, 0) = self->count;" in content
    assert "streamutils::write_axi4_word<32>(s, w, tlast);" in content
    assert "void read_array(const ap_uint<word_bw> x[]) {" in content
    assert "void read_stream(hls::stream<ap_uint<word_bw>> &s) {" in content
    assert "void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {" in content
    assert "void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s) {" in content
    assert "self->count = (ap_uint<16>)(x[0].range(15, 0));" in content
    assert "last = axis_word.last;" in content


def test_datalist_gen_include_emits_nwords_helper_and_json_nested_calls(tmp_path: Path):
    result = Packet.as_buildable(word_bw_supported=[32, 64]).run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]
    content = out_path.read_text(encoding="utf-8")
    tb_content = result.artifacts["tb_include"].read_text(encoding="utf-8")

    assert out_path == tmp_path / "packet.h"
    assert "template<int word_bw>" in content
    assert "static constexpr int nwords() {" in content
    assert "struct word_bw_tag {};" in content
    assert "static constexpr int nwords_value(word_bw_tag<32>) {" in content
    assert "return 4;" in content
    assert "static constexpr int nwords_value(word_bw_tag<64>) {" in content
    assert "return 2;" in content
    assert "return nwords_value(word_bw_tag<word_bw>{});" in content
    assert "this->z.dump_json(os, step, level + 1);" in tb_content
    assert "this->z.load_json(json_text, pos);" in tb_content


def test_datalist_with_dataarray_field_uses_nested_storage_member_in_codegen(tmp_path: Path):
    result = PacketWithArray.as_buildable(word_bw_supported=[32, 64]).run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]
    content = out_path.read_text(encoding="utf-8")
    tb_content = result.artifacts["tb_include"].read_text(encoding="utf-8")

    assert "self->coeffs.coeff[i0]" in content
    assert "self->coeffs.coeff[i + 0]" in content
    assert "self->coeffs.coeff[i0] = streamutils::uint_to_float((uint32_t)(x[in_idx]));" in content
    assert "os << this->coeffs.coeff[i0];" in tb_content
    assert "this->coeffs.coeff[i0] =" in tb_content
    assert "streamutils::json_parse_number(json_text, pos)" in tb_content
    assert "        {\n            const int n0_eff = 4;\n            int out_idx = 1;" in content
    assert "const int total_words = (n0_eff + 2 - 1) / 2;" not in content
    assert "const bool last =" not in content
    assert "n0_eff_self_" not in content


def test_datalist_with_multiple_dataarray_fields_uses_unique_codegen_locals(tmp_path: Path):
    result = MultiArrayPacket.as_buildable(word_bw_supported=[32]).run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]
    content = out_path.read_text(encoding="utf-8")

    assert "        {\n            const int n0_eff = 4;\n            int out_idx = 0;\n            for (int i0 = 0; i0 < n0_eff; ++i0) {" in content
    assert "        {\n            const int n0_eff = 4;\n            int out_idx = 4;\n            for (int i0 = 0; i0 < n0_eff; ++i0) {" in content
    assert "        {\n            const int n0_eff = 4;\n            int out_idx = 8;\n            for (int i0 = 0; i0 < n0_eff; ++i0) {" in content
    assert "n0_eff_self_" not in content
    assert "out_idx_self_" not in content


def test_gen_include_rejects_non_positive_word_widths():
    with pytest.raises(ValueError, match="word_bw values must be positive"):
        Packet.as_buildable(word_bw_supported=[0])


def test_gen_include_writes_under_cfg_root_and_include_dir(tmp_path: Path):
    class Instruction(DataList):
        include_dir = "isa"
        elements = {
            "count": U16,
        }

    result = Instruction.as_buildable().run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]
    content = out_path.read_text(encoding="utf-8")
    tb_content = result.artifacts["tb_include"].read_text(encoding="utf-8")

    assert out_path == tmp_path / "isa" / "instruction.h"
    assert out_path.exists()
    assert '#include "../streamutils_hls.h"' in content
    assert '#include "../streamutils_tb.h"' in tb_content


def test_gen_include_uses_streamutils_step_output_dir(tmp_path: Path):
    su_step = StreamUtilsStep(output_dir="common")
    schema_step = Packet.as_buildable()
    schema_step.resolve_deps([su_step])
    result = schema_step.run(BuildConfig(root_dir=tmp_path))
    content = result.artifacts["include"].read_text(encoding="utf-8")
    tb_content = result.artifacts["tb_include"].read_text(encoding="utf-8")

    assert '#include "common/streamutils_hls.h"' in content
    assert '#include "common/streamutils_tb.h"' in tb_content


def test_gen_include_overwrites_existing_file(tmp_path: Path):
    out_path = tmp_path / "packet.h"
    out_path.write_text("stale", encoding="utf-8")

    result = Packet.as_buildable().run(BuildConfig(root_dir=tmp_path))
    written_path = result.artifacts["include"]

    assert written_path == out_path
    assert "stale" not in out_path.read_text(encoding="utf-8")


def test_streamutils_step_emits_tlast_status_enum(tmp_path: Path):
    cfg = BuildConfig(root_dir=tmp_path)
    StreamUtilsStep().run(cfg)
    MemMgrStep().run(cfg)
    content = (tmp_path / "streamutils_hls.h").read_text(encoding="utf-8")
    cpp_content = (tmp_path / "streamutils.cpp").read_text(encoding="utf-8")
    memmgr_hpp = (tmp_path / "memmgr.hpp").read_text(encoding="utf-8")
    memmgr_tb_hpp = (tmp_path / "memmgr_tb.hpp").read_text(encoding="utf-8")

    assert "enum class tlast_status {" in content
    assert "struct tlast_status_info {" in content
    assert "template<int W>" in content
    assert "using axi4s_word = ap_axis<W, 0, 0, 0>;" in content
    assert "void flush_axi4_stream_to_tlast(hls::stream<axi4s_word<W>> &s) {" in content
    assert "static const char* names[count];" in content
    assert "legacy compatibility shim" in cpp_content
    assert "Vitis HLS < 2025.1" in cpp_content
    assert "no_tlast," in content
    assert "tlast_at_end," in content
    assert "tlast_early," in content
    assert "byte_addr_to_word_index" in memmgr_hpp
    assert "namespace memmgr" in memmgr_hpp
    assert "class MemMgr" in memmgr_tb_hpp
    assert '#include "memmgr.hpp"' in memmgr_tb_hpp


def test_streamutils_step_without_memmgr(tmp_path: Path):
    StreamUtilsStep().run(BuildConfig(root_dir=tmp_path))

    assert (tmp_path / "streamutils_hls.h").exists()
    assert (tmp_path / "streamutils_tb.h").exists()
    assert not (tmp_path / "memmgr.hpp").exists()
    assert not (tmp_path / "memmgr_tb.hpp").exists()


def test_primitive_field_pack_unpack_helpers_are_empty():
    assert U16.gen_pack() == ""
    assert U16.gen_unpack() == ""


def test_datalist_pack_emits_expected_member_slices():
    content = Packet.gen_pack()

    assert "static ap_uint<bitwidth> pack_to_uint(const Packet& data) {" in content
    assert "ap_uint<bitwidth> res = 0;" in content
    assert "res.range(15, 0) = data.count;" in content
    assert "res.range(47, 16) = streamutils::float_to_uint(data.gain);" in content
    assert "res.range(49, 48) = (ap_uint<2>)(static_cast<unsigned int>(data.mode));" in content
    assert "res.range(81, 50) = Complex::pack_to_uint(data.z);" in content
    assert "return res;" in content


def test_datalist_unpack_emits_expected_member_slices():
    content = Packet.gen_unpack()

    assert "static Packet unpack_from_uint(const ap_uint<bitwidth>& packed) {" in content
    assert "Packet data;" in content
    assert "data.count = (ap_uint<16>)(packed.range(15, 0));" in content
    assert "data.gain = streamutils::uint_to_float((uint32_t)(packed.range(47, 16)));" in content
    assert "data.mode = static_cast<Mode>(static_cast<unsigned int>(packed.range(49, 48)));" in content
    assert "data.z = Complex::unpack_from_uint(packed.range(81, 50));" in content
    assert "return data;" in content


def test_dataarray_oversized_whole_object_pack_is_suppressed(tmp_path: Path):
    """A DataArray whose packed width exceeds the HLS ap_uint limit must not emit
    whole-object pack_to_uint/unpack_from_uint — that would be a non-synthesizable
    ap_uint<N> (N > 8191).  Element-wise / burst access is still emitted."""
    from waveflow.hw.dataschema import HLS_AP_UINT_MAX_BITWIDTH

    # 512 x 16 = 8192 bits, one bit over the 8191-bit ap_uint limit.
    Big = DataArray.specialize(element_type=U16, max_shape=(512,), member_name="buf")
    assert Big.get_bitwidth() > HLS_AP_UINT_MAX_BITWIDTH
    assert Big.can_pack_whole() is False

    content = (
        Big.as_buildable(word_bw_supported=[32])
        .run(BuildConfig(root_dir=tmp_path))
        .artifacts["include"]
        .read_text(encoding="utf-8")
    )
    assert "pack_to_uint" not in content
    assert "unpack_from_uint" not in content
    assert "ap_uint<bitwidth>" not in content
    # The buffer is still usable element-wise / by burst (array_utils path).
    assert "read_array" in content and "write_array" in content


def test_dataarray_oversized_pack_emitters_fail_fast():
    """The emitters themselves are a backstop: invoked directly on an oversized
    array they raise a descriptive ValueError naming the width and the limit."""
    Big = DataArray.specialize(element_type=U16, max_shape=(512,), member_name="buf")
    with pytest.raises(ValueError, match=r"exceeding the HLS ap_uint limit of 8191 bits"):
        Big.gen_pack()
    with pytest.raises(ValueError, match=r"exceeding the HLS ap_uint limit of 8191 bits"):
        Big.gen_unpack()


def test_dataarray_within_limit_still_packs_whole_object():
    """A small DataArray is unaffected — whole-object pack/unpack still emitted."""
    assert CoeffArray.can_pack_whole() is True
    assert "static ap_uint<bitwidth> pack_to_uint(const" in CoeffArray.gen_pack()
    assert "unpack_from_uint(const ap_uint<bitwidth>&" in CoeffArray.gen_unpack()


def test_datalist_gen_write_array_emits_expected_slices():
    content = Packet.gen_write(word_bw=32, dst_type="array")

    assert "template<int word_bw>" in content
    assert "void write_array(ap_uint<word_bw> x[]) const {" in content
    assert "static void write_array_impl(word_bw_tag<32>, const Packet* self, ap_uint<32> x[]) {" in content
    assert "write_array_impl(word_bw_tag<word_bw>{}, this, x);" in content
    assert "x[0] = 0;" in content
    assert "x[0].range(15, 0) = self->count;" in content
    assert "x[1] = streamutils::float_to_uint(self->gain);" in content
    assert "x[2] = 0;" in content
    assert "x[2].range(1, 0) = (ap_uint<2>)(static_cast<unsigned int>(self->mode));" in content
    assert "x[2].range(17, 2) = self->z.real;" in content
    assert "x[3].range(15, 0) = self->z.imag;" in content


def test_datalist_gen_write_stream_flushes_words():
    content = Packet.gen_write(word_bw=32, dst_type="stream")

    assert "void write_stream(hls::stream<ap_uint<word_bw>> &s) const {" in content
    assert "ap_uint<32> w = 0;" in content
    assert "w.range(15, 0) = self->count;" in content
    assert "s.write(w);" in content
    assert "w = 0;" in content
    assert "w = streamutils::float_to_uint(self->gain);" in content
    assert "w.range(1, 0) = (ap_uint<2>)(static_cast<unsigned int>(self->mode));" in content
    assert "w.range(17, 2) = self->z.real;" in content
    assert "w.range(15, 0) = self->z.imag;" in content


def test_datalist_gen_write_axi4_stream_uses_tlast_on_final_word():
    content = Packet.gen_write(word_bw=32, dst_type="axi4_stream")

    assert "void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {" in content
    assert "ap_uint<32> w = 0;" in content
    assert "streamutils::write_axi4_word<32>(s, w, false);" in content
    assert "streamutils::write_axi4_word<32>(s, w, tlast);" in content


def test_gen_write_requires_word_width_configuration():
    with pytest.raises(ValueError, match="word_bw must be provided"):
        Packet.gen_write()


def test_datalist_gen_read_array_emits_expected_slices():
    content = Packet.gen_read(word_bw=32, src_type="array")

    assert "template<int word_bw>" in content
    assert "void read_array(const ap_uint<word_bw> x[]) {" in content
    assert "static void read_array_impl(word_bw_tag<32>, Packet* self, const ap_uint<32> x[]) {" in content
    assert "read_array_impl(word_bw_tag<word_bw>{}, this, x);" in content
    assert "self->count = (ap_uint<16>)(x[0].range(15, 0));" in content
    assert "self->gain = streamutils::uint_to_float((uint32_t)(x[1]));" in content
    assert "self->mode = static_cast<Mode>(static_cast<unsigned int>(x[2].range(1, 0)));" in content
    assert "self->z.real = (ap_int<16>)(x[2].range(17, 2));" in content
    assert "self->z.imag = (ap_int<16>)(x[3].range(15, 0));" in content


def test_datalist_gen_read_stream_emits_word_reads_at_boundaries():
    content = Packet.gen_read(word_bw=32, src_type="stream")

    assert "void read_stream(hls::stream<ap_uint<word_bw>> &s) {" in content
    assert "ap_uint<32> w = 0;" in content
    assert "w = s.read();" in content
    assert "self->count = (ap_uint<16>)(w.range(15, 0));" in content
    assert "self->gain = streamutils::uint_to_float((uint32_t)(w));" in content
    assert "self->mode = static_cast<Mode>(static_cast<unsigned int>(w.range(1, 0)));" in content


def test_datalist_gen_read_axi4_stream_uses_data_field():
    content = Packet.gen_read(word_bw=32, src_type="axi4_stream")

    assert "void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {" in content
    assert "void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s) {" in content
    assert "ap_uint<32> w = 0;" in content
    assert "tl = streamutils::tlast_status::no_tlast;" in content
    assert "auto axis_word = s.read();" in content
    assert "w = axis_word.data;" in content
    assert "last = axis_word.last;" in content
    assert "tl = streamutils::tlast_status::tlast_at_end;" in content
    assert "self->gain = streamutils::uint_to_float((uint32_t)(w));" in content


def test_dataarray_gen_read_axi4_stream_nested_schema_avoids_return_in_loop():
    PacketArray = DataArray.specialize(element_type=Packet, max_shape=(2,), member_name="pkt")

    content = PacketArray.gen_read(word_bw=32, src_type="axi4_stream")

    assert "    {\n        const int n0_eff = 2;\n        int in_idx = 0;\n        int elem_count = 0;\n        bool stop = false;" in content
    assert "for (int i0 = 0; i0 < n0_eff && !stop; ++i0) {" in content
    assert "stop = true;" in content
    assert "tl = (elem_count < (n0_eff)) ? streamutils::tlast_status::tlast_early : streamutils::tlast_status::tlast_at_end;" in content


def test_gen_read_requires_word_width_configuration():
    with pytest.raises(ValueError, match="word_bw must be provided"):
        Packet.gen_read()


def test_datalist_initialization_nested_snapshot_and_types():
    packet = Packet()
    packet_ref = Packet()

    assert packet.is_close(packet_ref)
    assert isinstance(packet.count, np.uint32)
    assert isinstance(packet.gain, np.float32)
    assert packet.mode is Mode.AUTO
    assert isinstance(packet.z, Complex)
    assert isinstance(packet.z.real, np.int32)
    assert isinstance(packet.z.imag, np.int32)


def test_assignment_conversion_for_scalar_and_enum_fields():
    packet = Packet()

    packet.mode = 1
    packet.gain = 3
    packet.z.real = -5
    packet.count = 7

    assert packet.mode is Mode.ON
    assert isinstance(packet.gain, np.float32)
    assert float(packet.gain) == pytest.approx(3.0)
    assert isinstance(packet.z.real, np.int32)
    assert int(packet.z.real) == -5
    assert isinstance(packet.count, np.uint32)
    assert int(packet.count) == 7


def test_default_include_filename_uses_snake_case_class_name():
    class MyPacketHeader(DataList):
        elements = {}

    assert MyPacketHeader.default_include_filename() == "my_packet_header.h"


def test_public_hw_exports_point_to_dataschema2_symbols():
    assert PublicDataSchema is DataSchema
    assert PublicDataField is DataField
    assert PublicIntField is IntField
    assert PublicMemAddr is MemAddr
    assert PublicFloatField is FloatField
    assert PublicEnumField is EnumField
    assert PublicDataList is DataList
    assert PublicDataArray is DataArray


def test_root_include_dir_resolves_to_filename_only():
    class Instruction(DataList):
        elements = {}

    assert Instruction.include_path() == "instruction.h"
    assert Instruction.tb_include_path() == "instruction_tb.h"


def test_dataarray_subclass_include_uses_class_name_not_member_name(tmp_path: Path):
    class CoeffArray(DataArray):
        element_type = F32
        max_shape = (4,)
        static = True

    result = CoeffArray.as_buildable(word_bw_supported=[32]).run(BuildConfig(root_dir=tmp_path))
    out_path = result.artifacts["include"]

    assert out_path == tmp_path / "coeff_array.h"
    assert out_path.exists()


def test_non_root_include_dir_resolves_to_dir_filename():
    class Instruction(DataList):
        include_dir = "isa"
        elements = {}

    assert Instruction.include_path() == "isa/instruction.h"
    assert Instruction.tb_include_path() == "isa/instruction_tb.h"


def test_relative_include_path_uses_current_header_directory():
    class CommonMode(DataList):
        include_dir = "common"
        elements = {}

    class Instruction(DataList):
        include_dir = "isa"
        elements = {}

    assert Instruction.relative_include_path_to(CommonMode) == "../common/common_mode.h"
    assert Instruction.relative_tb_include_path_to(CommonMode) == "../common/common_mode_tb.h"


def test_invalid_specialize_kwargs_are_rejected():
    with pytest.raises(TypeError, match="Unknown specialization keyword"):
        IntField.specialize(bitwidth=16, include_dri="isa")


@pytest.mark.parametrize("field_type", [IntField.specialize(bitwidth=16), FloatField.specialize(bitwidth=32)])
def test_primitive_field_gen_include_raises(field_type):
    with pytest.raises(ValueError, match="does not support standalone include generation"):
        field_type.as_buildable()

# ---------------------------------------------------------------------------
# Phase 3: DataArray cpp_storage
# ---------------------------------------------------------------------------

def test_dataarray_cpp_storage_default_is_struct():
    from waveflow.hw.dataschema import DataArray, FloatField
    Float32 = FloatField.specialize(bitwidth=32)
    cls = DataArray.specialize(element_type=Float32, max_shape=(4,), static=True)
    assert cls.cpp_storage == "struct"


def test_dataarray_specialize_raw_storage():
    from waveflow.hw.dataschema import DataArray, FloatField
    Float32 = FloatField.specialize(bitwidth=32)
    cls = DataArray.specialize(element_type=Float32, max_shape=(4,), static=True, cpp_storage="raw")
    assert cls.cpp_storage == "raw"
    assert cls._declared_count() == 4


def test_dataarray_specialize_invalid_cpp_storage_raises():
    from waveflow.hw.dataschema import DataArray, FloatField
    Float32 = FloatField.specialize(bitwidth=32)
    with pytest.raises(ValueError, match="invalid"):
        DataArray.specialize(element_type=Float32, max_shape=(4,), static=True, cpp_storage="hybrid")


def test_dataarray_specialize_raw_nonstatic_raises():
    from waveflow.hw.dataschema import DataArray, FloatField
    Float32 = FloatField.specialize(bitwidth=32)
    with pytest.raises(ValueError, match="static=True"):
        DataArray.specialize(element_type=Float32, max_shape=(4,), static=False, cpp_storage="raw")


def test_dataarray_specialize_raw_multidim_raises():
    from waveflow.hw.dataschema import DataArray, FloatField
    Float32 = FloatField.specialize(bitwidth=32)
    with pytest.raises(ValueError, match="max_shape"):
        DataArray.specialize(element_type=Float32, max_shape=(4, 4), static=True, cpp_storage="raw")


def test_dataarray_subclass_invalid_cpp_storage_raises():
    from waveflow.hw.dataschema import DataArray, FloatField
    Float32 = FloatField.specialize(bitwidth=32)
    with pytest.raises(ValueError, match="invalid"):
        class BadArray(DataArray):
            element_type = Float32
            max_shape = (4,)
            static = True
            cpp_storage = "hybrid"


# ---------------------------------------------------------------------------
# VarDataArray tests
# ---------------------------------------------------------------------------

U8_V = IntField.specialize(bitwidth=8, signed=False)
U16_V = IntField.specialize(bitwidth=16, signed=False)
F32_V = FloatField.specialize(bitwidth=32)

VU8 = VarDataArray.specialize(elem_type=U8_V, len_max=10)
VU16 = VarDataArray.specialize(elem_type=U16_V, len_max=5)
VF32 = VarDataArray.specialize(elem_type=F32_V, len_max=8)


def test_vardataarray_default_nbits_len():
    """nbits_len defaults to max(1, len_max.bit_length())."""
    assert VarDataArray.specialize(elem_type=U8_V, len_max=0).nbits_len == 1
    assert VarDataArray.specialize(elem_type=U8_V, len_max=1).nbits_len == 1
    assert VarDataArray.specialize(elem_type=U8_V, len_max=2).nbits_len == 2
    assert VarDataArray.specialize(elem_type=U8_V, len_max=3).nbits_len == 2
    assert VarDataArray.specialize(elem_type=U8_V, len_max=4).nbits_len == 3
    assert VarDataArray.specialize(elem_type=U8_V, len_max=10).nbits_len == 4
    assert VarDataArray.specialize(elem_type=U8_V, len_max=100).nbits_len == 7


def test_vardataarray_custom_nbits_len():
    V = VarDataArray.specialize(elem_type=U8_V, len_max=10, nbits_len=8)
    assert V.nbits_len == 8
    assert V.get_bitwidth() == 8 + 10 * 8


def test_vardataarray_nbits_len_too_small_raises():
    with pytest.raises(ValueError, match="nbits_len"):
        VarDataArray.specialize(elem_type=U8_V, len_max=10, nbits_len=0)


def test_vardataarray_len_max_negative_raises():
    with pytest.raises(ValueError, match="len_max"):
        VarDataArray.specialize(elem_type=U8_V, len_max=-1)


def test_vardataarray_get_bitwidth_max():
    # nbits_len=4 for len_max=10, elem_bw=8
    assert VU8.get_bitwidth() == 4 + 10 * 8
    assert VU8.get_bitwidth_max() == VU8.get_bitwidth()


def test_vardataarray_init_empty():
    v = VU8()
    assert len(v) == 0
    assert v.get_bitwidth_active() == VU8.nbits_len


def test_vardataarray_zero_active_serialize_deserialize():
    v = VU8()
    packed = v.serialize(word_bw=32)
    assert packed.shape == (1,)
    v2 = VU8()
    v2.deserialize(packed, word_bw=32)
    assert len(v2) == 0
    assert v.is_close(v2)


def test_vardataarray_nonzero_active_serialize_deserialize():
    v = VU8()
    v.val = np.array([10, 20, 30], dtype=np.uint32)
    packed = v.serialize(word_bw=32)
    v2 = VU8()
    v2.deserialize(packed, word_bw=32)
    assert v.is_close(v2)
    assert np.array_equal(v2.val, [10, 20, 30])


def test_vardataarray_max_active_serialize_deserialize():
    v = VU8()
    v.val = np.arange(10, dtype=np.uint32)
    packed = v.serialize(word_bw=32)
    assert packed.shape == (VU8.nwords_per_inst(32),)
    v2 = VU8()
    v2.deserialize(packed, word_bw=32)
    assert v.is_close(v2)
    assert np.array_equal(v2.val, np.arange(10))


def test_vardataarray_assignment_exceeds_len_max_raises():
    v = VU8()
    with pytest.raises(ValueError, match="len_max"):
        v.val = np.arange(11, dtype=np.uint32)


def test_vardataarray_get_bitwidth_active_vs_max():
    v = VU8()
    v.val = np.array([1, 2, 3], dtype=np.uint32)
    assert v.get_bitwidth_active() < VU8.get_bitwidth_max()
    assert v.get_bitwidth_active() == VU8.nbits_len + 3 * 8


def test_vardataarray_nwords_active_le_nwords_max():
    v = VU8()
    v.val = np.array([1, 2, 3], dtype=np.uint32)
    assert v.nwords_active(32) <= VU8.nwords_max(32)


def test_vardataarray_nwords_active_matches_serialized_length():
    v = VU8()
    v.val = np.array([10, 20, 30, 40, 50], dtype=np.uint32)
    packed = v.serialize(word_bw=32)
    assert v.nwords_active(32) == len(packed)


def test_vardataarray_length_first_serialization():
    """Deserializing must read in-band length, not use an external parameter."""
    v = VU8()
    v.val = np.array([0xAA, 0xBB], dtype=np.uint32)
    packed = v.serialize(word_bw=32)
    # Manually check: bits [0 : nbits_len-1] should encode length=2
    nbits_len = VU8.nbits_len
    mask = (1 << nbits_len) - 1
    encoded_len = int(packed[0]) & mask
    assert encoded_len == 2


def test_vardataarray_float_roundtrip():
    v = VF32()
    v.val = np.array([1.5, -2.0, 3.14], dtype=np.float32)
    packed = v.serialize(word_bw=32)
    v2 = VF32()
    v2.deserialize(packed, word_bw=32)
    assert v.is_close(v2)


def test_vardataarray_specialize_caches():
    A = VarDataArray.specialize(elem_type=U8_V, len_max=10)
    B = VarDataArray.specialize(elem_type=U8_V, len_max=10)
    assert A is B


def test_vardataarray_specialize_different_args_different_classes():
    A = VarDataArray.specialize(elem_type=U8_V, len_max=5)
    B = VarDataArray.specialize(elem_type=U8_V, len_max=10)
    assert A is not B


def test_vardataarray_exported_from_hw_init():
    from waveflow.hw import VarDataArray as PublicVarDataArray
    assert PublicVarDataArray is VarDataArray


# ---------------------------------------------------------------------------
# DataList containing VarDataArray
# ---------------------------------------------------------------------------

_VArr5 = VarDataArray.specialize(elem_type=U16_V, len_max=5)


class PacketWithVarData(DataList):
    elements = {
        "header": U8_V,
        "payload": _VArr5,
        "footer": U8_V,
    }


def test_datalist_with_vardataarray_get_bitwidth():
    # max bitwidth: 8 + (3 + 5*16) + 8 = 8 + 83 + 8 = 99
    assert PacketWithVarData.get_bitwidth() == 8 + (3 + 5 * 16) + 8


def test_datalist_with_vardataarray_nwords_per_inst():
    """nwords_per_inst for DataList with VarDataArray gives worst-case count."""
    pkt = PacketWithVarData()
    assert PacketWithVarData.nwords_per_inst(32) >= pkt.nwords_active(32)


def test_datalist_with_vardataarray_get_bitwidth_active():
    pkt = PacketWithVarData()
    pkt.header = 1
    pkt.payload = np.array([100, 200], dtype=np.uint32)
    pkt.footer = 2
    active = pkt.get_bitwidth_active()
    # 8 + (3 + 2*16) + 8 = 8 + 35 + 8 = 51
    assert active == 8 + (3 + 2 * 16) + 8


def test_datalist_with_vardataarray_nwords_active_matches_serialized():
    pkt = PacketWithVarData()
    pkt.header = 0xAB
    pkt.payload = np.array([10, 20, 30], dtype=np.uint32)
    pkt.footer = 0xCD
    packed = pkt.serialize(word_bw=32)
    assert pkt.nwords_active(32) == len(packed)


def test_datalist_with_vardataarray_roundtrip():
    pkt = PacketWithVarData()
    pkt.header = 0xAB
    pkt.payload = np.array([100, 200, 300], dtype=np.uint32)
    pkt.footer = 0xCD
    packed = pkt.serialize(word_bw=32)
    pkt2 = PacketWithVarData()
    pkt2.deserialize(packed, word_bw=32)
    assert pkt.is_close(pkt2)
    assert pkt2.header == 0xAB
    assert np.array_equal(pkt2.payload, [100, 200, 300])
    assert pkt2.footer == 0xCD


def test_datalist_with_empty_vardataarray_roundtrip():
    pkt = PacketWithVarData()
    pkt.header = 1
    # payload stays empty (default)
    pkt.footer = 2
    packed = pkt.serialize(word_bw=32)
    pkt2 = PacketWithVarData()
    pkt2.deserialize(packed, word_bw=32)
    assert pkt.is_close(pkt2)
    assert len(pkt2.payload) == 0


def test_datalist_with_max_vardataarray_roundtrip():
    pkt = PacketWithVarData()
    pkt.header = 0xFF
    pkt.payload = np.array([1, 2, 3, 4, 5], dtype=np.uint32)
    pkt.footer = 0x01
    packed = pkt.serialize(word_bw=32)
    assert len(packed) == PacketWithVarData.nwords_per_inst(32)
    pkt2 = PacketWithVarData()
    pkt2.deserialize(packed, word_bw=32)
    assert pkt.is_close(pkt2)


def test_datalist_nwords_active_le_nwords_max():
    pkt = PacketWithVarData()
    pkt.payload = np.array([1], dtype=np.uint32)
    assert pkt.nwords_active(32) <= PacketWithVarData.nwords_per_inst(32)


# ---------------------------------------------------------------------------
# Base DataSchema max/active aliases
# ---------------------------------------------------------------------------

def test_dataschema_get_bitwidth_max_alias():
    assert U8_V.get_bitwidth_max() == U8_V.get_bitwidth()


def test_dataschema_get_bitwidth_active_default():
    u = U8_V()
    assert u.get_bitwidth_active() == U8_V.get_bitwidth()


def test_dataschema_nwords_max_alias():
    assert U8_V.nwords_max(32) == U8_V.nwords_per_inst(32)


def test_dataschema_nwords_active_default():
    u = U8_V()
    assert u.nwords_active(32) == U8_V.nwords_per_inst(32)


# ---------------------------------------------------------------------------
# VarDataArray C++ codegen
# ---------------------------------------------------------------------------

def test_vardataarray_gen_include_decl_structure():
    VSmall = VarDataArray.specialize(elem_type=U8_V, len_max=4)
    decl = VSmall._gen_include_decl(word_bw_supported=[32])
    assert "struct UInt8VarArray" in decl
    assert "ap_uint<3> len;" in decl     # nbits_len=3 for len_max=4
    assert "data[4];" in decl
    assert "static constexpr int len_max = 4;" in decl
    assert "static constexpr int nbits_len = 3;" in decl
    assert "static constexpr int bitwidth = " in decl
    assert "nwords_active" in decl
    assert "write_array" in decl
    assert "read_array" in decl


# ---------------------------------------------------------------------------
# VarDataArray — the limits.
#
# These pin what VarDataArray CANNOT do, because the tests above imply otherwise.
# `test_datalist_with_vardataarray_roundtrip` proves a DataList can HOLD one (in
# Python), and `test_vardataarray_gen_include_decl_structure` proves a STANDALONE
# one emits C++.  A reader reasonably composes those two into "a DataList with a
# VarDataArray member generates C++".  It does not — and that composition is the
# only reason the schema was written.  So state it.
# ---------------------------------------------------------------------------

def test_datalist_with_vardataarray_cannot_generate_cpp():
    """A VarDataArray member blocks its DataList's C++ generation.  KNOWN LIMIT.

    DataList's generator is *static-cursor*: `_gen_write_recursive` returns
    `(lines, ipos, iword)` where the positions are Python ints fixed at GENERATION
    time, and it emits straight-line code.  A VarDataArray's size is only known at
    RUNTIME, so it cannot return a static cursor — hence the base-class stub raises
    rather than a subclass simply being missing.

    This is structural, not an oversight: making it work needs runtime cursors, a
    last-member-only rule (nothing may follow a runtime cursor), and variable-length
    stream framing in every reader.  Until then VarDataArray is usable standalone in
    Python and in C++, but NOT as a DataList member in C++.

    When that changes, this test fails — which is the signal to delete it.
    """
    U32 = IntField.specialize(bitwidth=32, signed=False)
    Msg = VarDataArray.specialize(elem_type=U32, len_max=8)

    class _CmdWithMsg(DataList):
        elements = {"word_index": U32, "n_words": U32, "transfer_msg": Msg}

    # A standalone VarDataArray generates fine...
    assert "struct" in Msg._gen_include_decl(word_bw_supported=[64])

    # ...but the same schema inside a DataList does not.
    with pytest.raises(NotImplementedError, match="does not implement write generation"):
        _CmdWithMsg._gen_include_decl(word_bw_supported=[64])


def test_datalist_with_vardataarray_has_no_fixed_wire_size():
    """The other half of the same problem: the wire contract is constexpr, the payload is not.

    `nwords_per_inst` is what generated C++ and the XSI BFMs use to size a command
    (a reader does exactly one `s.read()` of that many words).  With a VarDataArray
    member it reports the WORST CASE while the actual serialization varies with the
    message length — so a variable-length member is not an added field, it is a new
    framing protocol.
    """
    U32 = IntField.specialize(bitwidth=32, signed=False)
    Msg = VarDataArray.specialize(elem_type=U32, len_max=8)

    class _CmdWithMsg(DataList):
        elements = {"word_index": U32, "n_words": U32, "transfer_msg": Msg}

    fixed = _CmdWithMsg.nwords_per_inst(64)          # the constexpr the C++ trusts
    obj = _CmdWithMsg()
    sizes = []
    for n in (0, 4, 8):
        obj.transfer_msg = np.arange(n, dtype=np.uint32)
        sizes.append(len(obj.serialize(word_bw=64)))

    assert sizes == [2, 4, 6], sizes          # actual, varies with the message
    assert fixed == 6                         # declared, always the worst case
    assert len(set(sizes)) > 1, "if this is constant the framing objection is gone"


def test_vardataarray_cpp_clamps_len_to_len_max():
    """`len` is read off the WIRE and is nbits_len wide, so it can exceed len_max.

    len_max=8 -> nbits_len=4 -> len encodes 0..15 against data[8].  Unclamped, the
    emitted `for (i = 0; i < len; ++i) data[i] = ...` writes out of bounds on a
    malformed length, and HLS gets no trip count.  Both loops must clamp.
    """
    U32 = IntField.specialize(bitwidth=32, signed=False)
    Msg = VarDataArray.specialize(elem_type=U32, len_max=8)
    assert Msg.nbits_len == 4 and Msg.len_max == 8      # 4 bits => 0..15 vs data[8]

    decl = Msg._gen_include_decl(word_bw_supported=[64])
    assert "i < static_cast<int>(this->len)" not in decl, "unclamped loop bound"
    assert decl.count("n_act = static_cast<int>(this->len) < len_max") == 3, \
        "all three loops (nwords_active, write_array, read_array) must clamp"
    assert decl.count("for (int i = 0; i < n_act; ++i)") == 3
