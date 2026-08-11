"""Unit tests for waveflow/hw/dataunion.py.

Coverage
--------
SchemaRegistry        — register, get_id, get_class, contains_*, registered_ids, items
register_schema       — decorator registers class and returns it unchanged
SchemaIDField         — specialize, caching, val validation, range, round-trip, codegen
LengthField           — specialize, caching, val validation, range, round-trip
DataUnionHdr          — specialize, caching, elements, instantiation, round-trip
Integration           — SchemaIDField + LengthField embedded in DataList
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from waveflow.build.build import BuildConfig
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.dataunion import (
    DataUnion,
    DataUnionHdr,
    LengthField,
    SchemaIDField,
    SchemaRegistry,
    register_schema,
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

class Alpha(DataList):
    elements = {"x": IntField.specialize(8, signed=False)}


class Beta(DataList):
    elements = {"y": IntField.specialize(16, signed=False)}


class Gamma(DataList):
    elements = {"z": IntField.specialize(32, signed=False)}


def _make_registry(name: str = "Reg") -> SchemaRegistry:
    reg = SchemaRegistry(name)
    reg.register(Alpha, 1)
    reg.register(Beta, 2)
    reg.register(Gamma, 5)
    return reg


# ===========================================================================
# SchemaRegistry
# ===========================================================================

class TestSchemaRegistry:
    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="non-empty string"):
            SchemaRegistry("")

    def test_non_string_name_raises(self):
        with pytest.raises(ValueError):
            SchemaRegistry(123)  # type: ignore[arg-type]

    def test_register_and_get_id(self):
        reg = SchemaRegistry("R")
        reg.register(Alpha, 7)
        assert reg.get_id(Alpha) == 7

    def test_register_and_get_class(self):
        reg = SchemaRegistry("R")
        reg.register(Alpha, 7)
        assert reg.get_class(7) is Alpha

    def test_duplicate_id_raises(self):
        reg = SchemaRegistry("R")
        reg.register(Alpha, 1)
        with pytest.raises(ValueError, match="already registered"):
            reg.register(Beta, 1)

    def test_duplicate_class_raises(self):
        reg = SchemaRegistry("R")
        reg.register(Alpha, 1)
        with pytest.raises(ValueError, match="already registered"):
            reg.register(Alpha, 2)

    def test_negative_id_raises(self):
        reg = SchemaRegistry("R")
        with pytest.raises(ValueError, match="non-negative int"):
            reg.register(Alpha, -1)

    def test_non_int_id_raises(self):
        reg = SchemaRegistry("R")
        with pytest.raises(ValueError):
            reg.register(Alpha, "a")  # type: ignore[arg-type]

    def test_get_id_unregistered_raises_key_error(self):
        reg = SchemaRegistry("R")
        with pytest.raises(KeyError, match="Alpha"):
            reg.get_id(Alpha)

    def test_get_class_unregistered_raises_key_error(self):
        reg = SchemaRegistry("R")
        with pytest.raises(KeyError, match="42"):
            reg.get_class(42)

    def test_contains_id(self):
        reg = _make_registry()
        assert reg.contains_id(1) is True
        assert reg.contains_id(99) is False

    def test_contains_class(self):
        reg = _make_registry()
        assert reg.contains_class(Alpha) is True
        assert reg.contains_class(Gamma) is True

    def test_contains_class_unregistered(self):
        reg = SchemaRegistry("R")
        assert reg.contains_class(Alpha) is False

    def test_registered_ids_is_frozenset(self):
        reg = _make_registry()
        ids = reg.registered_ids
        assert isinstance(ids, frozenset)
        assert ids == frozenset({1, 2, 5})

    def test_items_sorted_by_id(self):
        reg = _make_registry()
        pairs = list(reg.items())
        ids = [i for i, _ in pairs]
        assert ids == sorted(ids)
        assert pairs[0] == (1, Alpha)
        assert pairs[1] == (2, Beta)
        assert pairs[2] == (5, Gamma)

    def test_multiple_registries_independent(self):
        reg1 = SchemaRegistry("R1")
        reg2 = SchemaRegistry("R2")
        reg1.register(Alpha, 10)
        reg2.register(Alpha, 20)
        assert reg1.get_id(Alpha) == 10
        assert reg2.get_id(Alpha) == 20

    def test_next_id_empty_registry(self):
        reg = SchemaRegistry("R")
        assert reg.next_id() == 0

    def test_next_id_after_registration(self):
        reg = SchemaRegistry("R")
        reg.register(Alpha, 3)
        assert reg.next_id() == 4

    def test_next_id_discontinuous_ids(self):
        reg = _make_registry()  # IDs: 1, 2, 5
        assert reg.next_id() == 6


# ===========================================================================
# register_schema decorator
# ===========================================================================

class TestRegisterSchemaDecorator:
    def test_decorator_registers_class(self):
        reg = SchemaRegistry("Dec")

        @register_schema(schema_id=3, registry=reg)
        class Packet(DataList):
            elements = {"v": IntField.specialize(8, signed=False)}

        assert reg.get_id(Packet) == 3
        assert reg.get_class(3) is Packet

    def test_decorator_returns_original_class(self):
        reg = SchemaRegistry("Dec2")

        @register_schema(schema_id=1, registry=reg)
        class MySchema(DataList):
            elements = {}

        assert MySchema.__name__ == "MySchema"
        assert issubclass(MySchema, DataList)

    def test_decorator_duplicate_id_raises(self):
        reg = SchemaRegistry("Dec3")
        reg.register(Alpha, 1)
        with pytest.raises(ValueError, match="already registered"):
            @register_schema(schema_id=1, registry=reg)
            class Another(DataList):
                elements = {}

    def test_decorator_auto_id_empty_registry(self):
        reg = SchemaRegistry("Auto1")

        @register_schema(registry=reg)
        class First(DataList):
            elements = {}

        assert reg.get_id(First) == 0

    def test_decorator_auto_id_sequential(self):
        reg = SchemaRegistry("Auto2")

        @register_schema(registry=reg)
        class A(DataList):
            elements = {}

        @register_schema(registry=reg)
        class B(DataList):
            elements = {}

        @register_schema(registry=reg)
        class C(DataList):
            elements = {}

        assert reg.get_id(A) == 0
        assert reg.get_id(B) == 1
        assert reg.get_id(C) == 2

    def test_decorator_auto_id_after_explicit(self):
        reg = SchemaRegistry("Auto3")

        @register_schema(schema_id=10, registry=reg)
        class First(DataList):
            elements = {}

        @register_schema(registry=reg)
        class Second(DataList):
            elements = {}

        assert reg.get_id(First) == 10
        assert reg.get_id(Second) == 11

    def test_decorator_explicit_id_positional(self):
        reg = SchemaRegistry("Pos")

        @register_schema(7, registry=reg)
        class Pkt(DataList):
            elements = {}

        assert reg.get_id(Pkt) == 7


# ===========================================================================
# SchemaIDField
# ===========================================================================

class TestSchemaIDFieldSpecialize:
    def test_specialize_returns_type(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        assert isinstance(F, type)
        assert issubclass(F, SchemaIDField)

    def test_specialize_cache_hit(self):
        reg = _make_registry()
        F1 = SchemaIDField.specialize(reg, bitwidth=16)
        F2 = SchemaIDField.specialize(reg, bitwidth=16)
        assert F1 is F2

    def test_specialize_different_bitwidth_different_type(self):
        reg = _make_registry()
        F16 = SchemaIDField.specialize(reg, bitwidth=16)
        F8 = SchemaIDField.specialize(reg, bitwidth=8)
        assert F16 is not F8

    def test_specialize_different_registry_different_type(self):
        reg1 = SchemaRegistry("R1")
        reg1.register(Alpha, 1)
        reg2 = SchemaRegistry("R2")
        reg2.register(Alpha, 1)
        F1 = SchemaIDField.specialize(reg1)
        F2 = SchemaIDField.specialize(reg2)
        assert F1 is not F2

    def test_specialize_non_registry_raises(self):
        with pytest.raises(TypeError, match="SchemaRegistry"):
            SchemaIDField.specialize("not_a_registry")  # type: ignore[arg-type]

    def test_specialize_zero_bitwidth_raises(self):
        reg = _make_registry()
        with pytest.raises(ValueError, match="positive"):
            SchemaIDField.specialize(reg, bitwidth=0)

    def test_subclass_name_includes_registry_name(self):
        reg = SchemaRegistry("MySys")
        reg.register(Alpha, 1)
        F = SchemaIDField.specialize(reg, bitwidth=16)
        assert "MySys" in F.__name__

    def test_bitwidth_on_specialized(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        assert F.get_bitwidth() == 16

    def test_signed_is_false(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        assert F.signed is False

    def test_can_gen_include_true_on_specialized(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg)
        assert F.can_gen_include is True

    def test_can_gen_include_false_on_base(self):
        assert SchemaIDField.can_gen_include is False

    def test_cpp_type_is_enum_name(self):
        reg = SchemaRegistry("MySys")
        reg.register(Alpha, 1)
        F = SchemaIDField.specialize(reg, bitwidth=16)
        assert F.cpp_class_name() == "MySysSchemaID"


class TestSchemaIDFieldVal:
    def test_val_registered_id_succeeds(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        inst = F()
        inst.val = 1
        assert int(inst.val) == 1

    def test_val_another_registered_id(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        inst = F()
        inst.val = 5
        assert int(inst.val) == 5

    def test_val_unregistered_id_raises(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        inst = F()
        with pytest.raises(ValueError, match="not a registered schema ID"):
            inst.val = 99

    def test_val_negative_raises(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        inst = F()
        with pytest.raises(ValueError, match="negative"):
            inst.val = -1

    def test_val_overflow_raises(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=8)
        inst = F()
        with pytest.raises(ValueError, match="exceeds maximum"):
            inst.val = 256

    def test_val_float_noninteger_raises(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        inst = F()
        with pytest.raises(ValueError, match="non-integer"):
            inst.val = 1.5

    def test_init_value_is_zero_without_validation(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        inst = F()
        assert int(inst.val) == 0

    def test_constructor_with_valid_value(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        inst = F(1)
        assert int(inst.val) == 1

    def test_constructor_with_invalid_value_raises(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)
        with pytest.raises(ValueError):
            F(99)


class TestSchemaIDFieldSerialization:
    def test_serialize_deserialize_roundtrip(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)

        class Hdr(DataList):
            elements = {"sid": F}

        h = Hdr(sid=1)
        words = h.serialize(word_bw=32)
        h2 = Hdr().deserialize(words, word_bw=32)
        assert int(h2.sid) == 1

    def test_deserialize_valid_id_succeeds(self):
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)

        class Hdr(DataList):
            elements = {"sid": F}

        h = Hdr(sid=2)
        words = h.serialize(word_bw=32)
        h2 = Hdr().deserialize(words, word_bw=32)
        assert int(h2.sid) == 2

    def test_deserialize_unregistered_id_raises(self):
        """Crafted words with an unregistered schema ID should raise on deserialization."""
        reg = _make_registry()
        F = SchemaIDField.specialize(reg, bitwidth=16)

        class Hdr(DataList):
            elements = {"sid": F}

        # Craft a word with schema_id=99 (unregistered)
        bad_words = np.array([99], dtype=np.uint32)
        with pytest.raises(ValueError, match="not a registered schema ID"):
            Hdr().deserialize(bad_words, word_bw=32)


class TestSchemaIDFieldCodegen:
    def test_gen_include_decl_contains_enum_class(self):
        reg = SchemaRegistry("Net")
        reg.register(Alpha, 1)
        reg.register(Beta, 3)
        F = SchemaIDField.specialize(reg, bitwidth=16)
        decl = F._gen_include_decl()
        assert "enum class NetSchemaID" in decl
        assert "uint16_t" in decl
        assert "Alpha = 1," in decl
        assert "Beta = 3," in decl

    def test_gen_include_decl_sorted_by_id(self):
        reg = SchemaRegistry("Net2")
        reg.register(Gamma, 10)
        reg.register(Alpha, 2)
        F = SchemaIDField.specialize(reg, bitwidth=16)
        decl = F._gen_include_decl()
        lines = decl.splitlines()
        alpha_line = next(l for l in lines if "Alpha" in l)
        gamma_line = next(l for l in lines if "Gamma" in l)
        assert lines.index(alpha_line) < lines.index(gamma_line)

    def test_gen_include_decl_on_base_raises(self):
        with pytest.raises(TypeError, match="registry"):
            SchemaIDField._gen_include_decl()

    def test_gen_include_writes_file(self, tmp_path: Path):
        reg = SchemaRegistry("Sys")
        reg.register(Alpha, 1)
        reg.register(Beta, 2)
        F = SchemaIDField.specialize(reg, bitwidth=16)
        result = F.as_buildable().run(BuildConfig(root_dir=tmp_path))
        out_path = result.artifacts["include"]
        content = out_path.read_text(encoding="utf-8")
        assert "enum class SysSchemaID" in content
        assert "Alpha = 1," in content
        assert "Beta = 2," in content
        assert "#ifndef" in content
        assert "#define" in content
        assert "#endif" in content


# ===========================================================================
# LengthField
# ===========================================================================

class TestLengthFieldSpecialize:
    def test_specialize_returns_type(self):
        F = LengthField.specialize(bitwidth=16)
        assert isinstance(F, type)
        assert issubclass(F, LengthField)

    def test_specialize_cache_hit(self):
        F1 = LengthField.specialize(bitwidth=16)
        F2 = LengthField.specialize(bitwidth=16)
        assert F1 is F2

    def test_specialize_different_bitwidth_different_type(self):
        F16 = LengthField.specialize(bitwidth=16)
        F8 = LengthField.specialize(bitwidth=8)
        assert F16 is not F8

    def test_zero_bitwidth_raises(self):
        with pytest.raises(ValueError, match="positive"):
            LengthField.specialize(bitwidth=0)

    def test_negative_bitwidth_raises(self):
        with pytest.raises(ValueError, match="positive"):
            LengthField.specialize(bitwidth=-4)

    def test_bitwidth_on_specialized(self):
        F = LengthField.specialize(bitwidth=12)
        assert F.get_bitwidth() == 12

    def test_signed_is_false(self):
        F = LengthField.specialize(bitwidth=16)
        assert F.signed is False

    def test_cpp_type(self):
        F = LengthField.specialize(bitwidth=16)
        assert F.cpp_class_name() == "ap_uint<16>"


class TestLengthFieldVal:
    def test_val_zero_succeeds(self):
        F = LengthField.specialize(bitwidth=16)
        inst = F()
        inst.val = 0
        assert int(inst.val) == 0

    def test_val_max_succeeds(self):
        F = LengthField.specialize(bitwidth=16)
        inst = F()
        inst.val = 65535
        assert int(inst.val) == 65535

    def test_val_positive_succeeds(self):
        F = LengthField.specialize(bitwidth=16)
        inst = F()
        inst.val = 100
        assert int(inst.val) == 100

    def test_val_negative_raises(self):
        F = LengthField.specialize(bitwidth=16)
        inst = F()
        with pytest.raises(ValueError, match="negative"):
            inst.val = -1

    def test_val_overflow_raises(self):
        F = LengthField.specialize(bitwidth=16)
        inst = F()
        with pytest.raises(ValueError, match="exceeds maximum"):
            inst.val = 65536

    def test_val_overflow_small_bitwidth(self):
        F = LengthField.specialize(bitwidth=8)
        inst = F()
        with pytest.raises(ValueError, match="exceeds maximum"):
            inst.val = 256

    def test_val_float_noninteger_raises(self):
        F = LengthField.specialize(bitwidth=16)
        inst = F()
        with pytest.raises(ValueError, match="non-integer"):
            inst.val = 3.7

    def test_val_float_integer_accepted(self):
        F = LengthField.specialize(bitwidth=16)
        inst = F()
        inst.val = 5.0
        assert int(inst.val) == 5

    def test_constructor_with_valid_value(self):
        F = LengthField.specialize(bitwidth=16)
        inst = F(42)
        assert int(inst.val) == 42

    def test_constructor_with_negative_raises(self):
        F = LengthField.specialize(bitwidth=16)
        with pytest.raises(ValueError, match="negative"):
            F(-1)


class TestLengthFieldSerialization:
    def test_serialize_deserialize_roundtrip(self):
        F = LengthField.specialize(bitwidth=16)

        class Hdr(DataList):
            elements = {"nwords": F}

        h = Hdr(nwords=42)
        words = h.serialize(word_bw=32)
        h2 = Hdr().deserialize(words, word_bw=32)
        assert int(h2.nwords) == 42

    def test_zero_length_roundtrip(self):
        F = LengthField.specialize(bitwidth=16)

        class Hdr(DataList):
            elements = {"nwords": F}

        h = Hdr(nwords=0)
        words = h.serialize(word_bw=32)
        h2 = Hdr().deserialize(words, word_bw=32)
        assert int(h2.nwords) == 0

    def test_max_length_roundtrip(self):
        F = LengthField.specialize(bitwidth=16)

        class Hdr(DataList):
            elements = {"nwords": F}

        h = Hdr(nwords=65535)
        words = h.serialize(word_bw=32)
        h2 = Hdr().deserialize(words, word_bw=32)
        assert int(h2.nwords) == 65535


# ===========================================================================
# Integration: SchemaIDField + LengthField in a single DataList header
# ===========================================================================

class TestIntegration:
    def test_header_roundtrip(self):
        reg = SchemaRegistry("Proto")
        reg.register(Alpha, 1)
        reg.register(Beta, 2)

        SchemaID16 = SchemaIDField.specialize(reg, bitwidth=16)
        Length16 = LengthField.specialize(bitwidth=16)

        class Header(DataList):
            elements = {
                "schema_id": SchemaID16,
                "nwords": Length16,
            }

        h = Header(schema_id=1, nwords=4)
        words = h.serialize(word_bw=32)
        h2 = Header().deserialize(words, word_bw=32)
        assert int(h2.schema_id) == 1
        assert int(h2.nwords) == 4

    def test_header_schema_id_2(self):
        reg = SchemaRegistry("Proto2")
        reg.register(Alpha, 1)
        reg.register(Beta, 2)

        SchemaID16 = SchemaIDField.specialize(reg, bitwidth=16)
        Length16 = LengthField.specialize(bitwidth=16)

        class Header2(DataList):
            elements = {
                "schema_id": SchemaID16,
                "nwords": Length16,
            }

        h = Header2(schema_id=2, nwords=8)
        words = h.serialize(word_bw=32)
        h2 = Header2().deserialize(words, word_bw=32)
        assert int(h2.schema_id) == 2
        assert int(h2.nwords) == 8

    def test_header_unregistered_id_assignment_raises(self):
        reg = SchemaRegistry("Proto3")
        reg.register(Alpha, 1)
        SchemaID16 = SchemaIDField.specialize(reg, bitwidth=16)
        Length16 = LengthField.specialize(bitwidth=16)

        class Header3(DataList):
            elements = {
                "schema_id": SchemaID16,
                "nwords": Length16,
            }

        h = Header3()
        with pytest.raises(ValueError, match="not a registered schema ID"):
            h.schema_id = 99

    def test_header_negative_nwords_raises(self):
        reg = SchemaRegistry("Proto4")
        reg.register(Alpha, 1)
        SchemaID16 = SchemaIDField.specialize(reg, bitwidth=16)
        Length16 = LengthField.specialize(bitwidth=16)

        class Header4(DataList):
            elements = {
                "schema_id": SchemaID16,
                "nwords": Length16,
            }

        h = Header4()
        h.schema_id = 1
        with pytest.raises(ValueError, match="negative"):
            h.nwords = -3

    def test_independent_registries_do_not_share_specializations(self):
        reg_a = SchemaRegistry("A")
        reg_a.register(Alpha, 1)
        reg_b = SchemaRegistry("B")
        reg_b.register(Beta, 1)

        FA = SchemaIDField.specialize(reg_a)
        FB = SchemaIDField.specialize(reg_b)
        assert FA is not FB

        inst_a = FA(1)
        assert int(inst_a.val) == 1

        inst_b = FB(1)
        assert int(inst_b.val) == 1


# ===========================================================================
# DataUnionHdr
# ===========================================================================

def _make_hdr_types(reg_name: str = "Hdr"):
    reg = SchemaRegistry(reg_name)
    reg.register(Alpha, 1)
    reg.register(Beta, 2)
    sid_type = SchemaIDField.specialize(reg, bitwidth=16)
    len_type = LengthField.specialize(bitwidth=16)
    return reg, sid_type, len_type


class TestDataUnionHdrSpecialize:
    def test_specialize_returns_datalist_subclass(self):
        _, sid, ln = _make_hdr_types("T1")
        H = DataUnionHdr.specialize(sid, ln)
        assert isinstance(H, type)
        assert issubclass(H, DataUnionHdr)
        from waveflow.hw.dataschema import DataList
        assert issubclass(H, DataList)

    def test_specialize_cache_hit(self):
        _, sid, ln = _make_hdr_types("T2")
        H1 = DataUnionHdr.specialize(sid, ln)
        H2 = DataUnionHdr.specialize(sid, ln)
        assert H1 is H2

    def test_specialize_different_types_different_class(self):
        _, sid1, ln1 = _make_hdr_types("T3a")
        _, sid2, ln2 = _make_hdr_types("T3b")
        H1 = DataUnionHdr.specialize(sid1, ln1)
        H2 = DataUnionHdr.specialize(sid2, ln2)
        assert H1 is not H2

    def test_specialize_stores_type_refs(self):
        _, sid, ln = _make_hdr_types("T4")
        H = DataUnionHdr.specialize(sid, ln)
        assert H.schema_id_type is sid
        assert H.length_id_type is ln

    def test_specialize_elements_keys(self):
        _, sid, ln = _make_hdr_types("T5")
        H = DataUnionHdr.specialize(sid, ln)
        assert list(H.elements) == ["schema_id", "nwords"]

    def test_specialize_elements_types(self):
        _, sid, ln = _make_hdr_types("T6")
        H = DataUnionHdr.specialize(sid, ln)
        assert H.elements["schema_id"] is sid
        assert H.elements["nwords"] is ln

    def test_subclass_name_uses_registry_name(self):
        reg = SchemaRegistry("MyProto")
        reg.register(Alpha, 1)
        sid = SchemaIDField.specialize(reg, bitwidth=16)
        ln = LengthField.specialize(bitwidth=16)
        H = DataUnionHdr.specialize(sid, ln)
        assert "MyProto" in H.__name__

    def test_wrong_schema_id_type_raises(self):
        ln = LengthField.specialize(bitwidth=16)
        with pytest.raises(TypeError, match="SchemaIDField"):
            DataUnionHdr.specialize(ln, ln)  # type: ignore[arg-type]

    def test_wrong_length_type_raises(self):
        reg = SchemaRegistry("Err")
        reg.register(Alpha, 1)
        sid = SchemaIDField.specialize(reg, bitwidth=16)
        with pytest.raises(TypeError, match="LengthField"):
            DataUnionHdr.specialize(sid, sid)  # type: ignore[arg-type]

    def test_specialize_without_length_type(self):
        _, sid, _ = _make_hdr_types("NoLen")
        H = DataUnionHdr.specialize(sid)
        assert list(H.elements) == ["schema_id"]
        assert H.length_id_type is None

    def test_specialize_none_length_type_matches_default(self):
        _, sid, _ = _make_hdr_types("NoneLen")
        H_default = DataUnionHdr.specialize(sid)
        H_explicit_none = DataUnionHdr.specialize(sid, None)
        assert H_default is H_explicit_none

    def test_specialize_with_and_without_length_are_different(self):
        reg, sid, ln = _make_hdr_types("WithWithout")
        H_with = DataUnionHdr.specialize(sid, ln)
        H_without = DataUnionHdr.specialize(sid)
        assert H_with is not H_without


class TestDataUnionHdrInstantiation:
    def setup_method(self):
        self.reg, self.sid_type, self.len_type = _make_hdr_types("Inst")
        self.H = DataUnionHdr.specialize(self.sid_type, self.len_type)

    def test_instantiate_with_kwargs(self):
        h = self.H(schema_id=1, nwords=4)
        assert int(h.schema_id) == 1
        assert int(h.nwords) == 4

    def test_instantiate_empty_then_assign(self):
        h = self.H()
        h.schema_id = 2
        h.nwords = 8
        assert int(h.schema_id) == 2
        assert int(h.nwords) == 8

    def test_unregistered_schema_id_raises(self):
        h = self.H()
        with pytest.raises(ValueError, match="not a registered schema ID"):
            h.schema_id = 99

    def test_negative_nwords_raises(self):
        h = self.H(schema_id=1)
        with pytest.raises(ValueError, match="negative"):
            h.nwords = -1

    def test_bitwidth(self):
        # 16 + 16 = 32 bits total
        assert self.H.get_bitwidth() == 32

    def test_serialize_deserialize_roundtrip(self):
        h = self.H(schema_id=1, nwords=7)
        words = h.serialize(word_bw=32)
        h2 = self.H().deserialize(words, word_bw=32)
        assert int(h2.schema_id) == 1
        assert int(h2.nwords) == 7

    def test_all_registered_ids_roundtrip(self):
        for sid in self.reg.registered_ids:
            h = self.H(schema_id=sid, nwords=0)
            words = h.serialize(word_bw=32)
            h2 = self.H().deserialize(words, word_bw=32)
            assert int(h2.schema_id) == sid


class TestDataUnionHdrNoLength:
    """DataUnionHdr.specialize() without a length_id_type."""

    def setup_method(self):
        self.reg, self.sid_type, _ = _make_hdr_types("NoLenInst")
        self.H = DataUnionHdr.specialize(self.sid_type)

    def test_elements_has_only_schema_id(self):
        assert list(self.H.elements) == ["schema_id"]
        assert "nwords" not in self.H.elements

    def test_bitwidth_is_schema_id_bitwidth(self):
        assert self.H.get_bitwidth() == 16

    def test_nwords_per_inst(self):
        assert self.H.nwords_per_inst(word_bw=32) == 1

    def test_instantiate_with_schema_id(self):
        h = self.H(schema_id=1)
        assert int(h.schema_id) == 1

    def test_serialize_deserialize_roundtrip(self):
        h = self.H(schema_id=2)
        words = h.serialize(word_bw=32)
        h2 = self.H().deserialize(words, word_bw=32)
        assert int(h2.schema_id) == 2

    def test_nwords_attribute_absent(self):
        h = self.H(schema_id=1)
        with pytest.raises(AttributeError):
            _ = h.nwords  # type: ignore[attr-defined]


# ===========================================================================
# DataUnion
# ===========================================================================

def _make_du_registry(name: str = "Du"):
    """Return (registry, SchemaIDField, DataUnionHdr, DataUnion) for testing."""
    reg = SchemaRegistry(name)
    reg.register(Alpha, 1)   # 8 bits
    reg.register(Beta, 2)    # 16 bits
    reg.register(Gamma, 3)   # 32 bits
    sid = SchemaIDField.specialize(reg, bitwidth=16)
    hdr = DataUnionHdr.specialize(sid)
    du = DataUnion.specialize(hdr)
    return reg, du


class TestDataUnionSpecialize:
    def test_specialize_returns_subclass(self):
        _, DU = _make_du_registry("Sp1")
        assert isinstance(DU, type)
        assert issubclass(DU, DataUnion)

    def test_specialize_cache_hit(self):
        reg = SchemaRegistry("Sp2")
        reg.register(Alpha, 1)
        sid = SchemaIDField.specialize(reg, bitwidth=16)
        hdr = DataUnionHdr.specialize(sid)
        DU1 = DataUnion.specialize(hdr)
        DU2 = DataUnion.specialize(hdr)
        assert DU1 is DU2

    def test_specialize_different_hdrs_different_class(self):
        reg1 = SchemaRegistry("Sp3a")
        reg1.register(Alpha, 1)
        sid1 = SchemaIDField.specialize(reg1, bitwidth=16)
        hdr1 = DataUnionHdr.specialize(sid1)

        reg2 = SchemaRegistry("Sp3b")
        reg2.register(Alpha, 1)
        sid2 = SchemaIDField.specialize(reg2, bitwidth=16)
        hdr2 = DataUnionHdr.specialize(sid2)

        DU1 = DataUnion.specialize(hdr1)
        DU2 = DataUnion.specialize(hdr2)
        assert DU1 is not DU2

    def test_specialize_stores_hdr_type_and_registry(self):
        _, DU = _make_du_registry("Sp4")
        assert issubclass(DU.hdr_type, DataUnionHdr)
        assert isinstance(DU.registry, SchemaRegistry)

    def test_subclass_name_includes_registry_name(self):
        _, DU = _make_du_registry("MyReg")
        assert "MyReg" in DU.__name__

    def test_non_dataunionhdr_raises(self):
        with pytest.raises(TypeError, match="DataUnionHdr"):
            DataUnion.specialize(Alpha)  # type: ignore[arg-type]

    def test_base_class_instantiation_raises(self):
        with pytest.raises(TypeError, match="base DataUnion"):
            DataUnion()

    def test_no_registry_on_hdr_raises(self):
        class FakeHdr(DataUnionHdr):
            pass
        with pytest.raises(TypeError, match="registry"):
            DataUnion.specialize(FakeHdr)


class TestDataUnionPayloadSetter:
    def setup_method(self):
        self.reg, self.DU = _make_du_registry("Pay")

    def test_payload_none_by_default(self):
        du = self.DU()
        assert du.payload is None

    def test_set_payload_alpha(self):
        du = self.DU()
        a = Alpha(x=42)
        du.payload = a
        assert du.payload is a
        assert du.schema_id == 1

    def test_set_payload_beta(self):
        du = self.DU()
        du.payload = Beta(y=1000)
        assert du.schema_id == 2

    def test_set_payload_gamma(self):
        du = self.DU()
        du.payload = Gamma(z=0xDEADBEEF)
        assert du.schema_id == 3

    def test_set_payload_updates_schema_id_in_header(self):
        du = self.DU()
        du.payload = Alpha(x=7)
        assert int(du.hdr.schema_id) == 1

    def test_set_unregistered_payload_raises(self):
        du = self.DU()
        class Other(DataList):
            elements = {"v": IntField.specialize(8, signed=False)}
        with pytest.raises(KeyError):
            du.payload = Other(v=1)

    def test_replace_payload_updates_schema_id(self):
        du = self.DU()
        du.payload = Alpha(x=10)
        assert du.schema_id == 1
        du.payload = Beta(y=20)
        assert du.schema_id == 2


class TestDataUnionSizeHelpers:
    def setup_method(self):
        self.reg, self.DU = _make_du_registry("Sz")

    def test_max_payload_bw(self):
        # Alpha=8, Beta=16, Gamma=32 → max=32
        assert self.DU.max_payload_bw() == 32

    def test_max_payload_nwords_32(self):
        # Gamma=32 bits → ceil(32/32)=1 word
        assert self.DU.max_payload_nwords(32) == 1

    def test_max_payload_nwords_8(self):
        # Gamma=32 bits → ceil(32/8)=4 words
        assert self.DU.max_payload_nwords(8) == 4

    def test_nwords_per_inst_32(self):
        # hdr=1 word (16-bit sid in 32-bit word) + max_payload=1 word = 2
        assert self.DU.nwords_per_inst(32) == 2

    def test_nwords_per_inst_64(self):
        # hdr=1 word (16-bit sid in 64-bit word) + max_payload=1 word = 2
        assert self.DU.nwords_per_inst(64) == 2


class TestDataUnionSerialize:
    def setup_method(self):
        self.reg, self.DU = _make_du_registry("Ser")

    def test_serialize_without_payload_raises(self):
        du = self.DU()
        with pytest.raises(ValueError, match="payload is not set"):
            du.serialize(word_bw=32)

    def test_serialize_alpha_has_correct_nwords(self):
        du = self.DU()
        du.payload = Alpha(x=7)
        words = du.serialize(word_bw=32)
        assert len(words) == self.DU.nwords_per_inst(32)

    def test_serialize_gamma_has_correct_nwords(self):
        du = self.DU()
        du.payload = Gamma(z=0)
        words = du.serialize(word_bw=32)
        assert len(words) == self.DU.nwords_per_inst(32)

    def test_serialize_alpha_padded_to_max(self):
        # Alpha is 8 bits (1 byte), but max payload nwords=1 (32-bit). Serialize
        # Alpha, expect nwords_per_inst words (not just hdr+1).
        du = self.DU()
        du.payload = Alpha(x=255)
        words = du.serialize(word_bw=32)
        assert len(words) == 2  # 1 hdr + 1 payload (padded)

    def test_serialize_header_word_encodes_schema_id(self):
        du = self.DU()
        du.payload = Beta(y=0)
        words = du.serialize(word_bw=32)
        # Header is 16-bit schema_id in 32-bit word → first word = schema_id = 2
        assert int(words[0]) == 2


class TestDataUnionDeserialize:
    def setup_method(self):
        self.reg, self.DU = _make_du_registry("Des")

    def _roundtrip(self, payload, word_bw=32):
        du = self.DU()
        du.payload = payload
        words = du.serialize(word_bw=word_bw)
        return self.DU().deserialize(words, word_bw=word_bw)

    def test_roundtrip_alpha(self):
        rx = self._roundtrip(Alpha(x=42))
        assert rx.schema_id == 1
        assert isinstance(rx.payload, Alpha)
        assert int(rx.payload.x) == 42

    def test_roundtrip_beta(self):
        rx = self._roundtrip(Beta(y=1000))
        assert rx.schema_id == 2
        assert isinstance(rx.payload, Beta)
        assert int(rx.payload.y) == 1000

    def test_roundtrip_gamma(self):
        rx = self._roundtrip(Gamma(z=0xDEADBEEF))
        assert rx.schema_id == 3
        assert isinstance(rx.payload, Gamma)
        assert int(rx.payload.z) == 0xDEADBEEF

    def test_roundtrip_all_at_64bit(self):
        for payload in [Alpha(x=200), Beta(y=65535), Gamma(z=0)]:
            rx = self._roundtrip(payload, word_bw=64)
            assert rx.payload.__class__ is payload.__class__

    def test_deserialize_returns_self(self):
        du = self.DU()
        du.payload = Alpha(x=1)
        words = du.serialize(word_bw=32)
        rx = self.DU()
        result = rx.deserialize(words, word_bw=32)
        assert result is rx

    def test_deserialize_invalid_schema_id_raises(self):
        # Craft words with schema_id=99 (not registered); SchemaIDField rejects it
        words = np.array([99, 0], dtype=np.uint32)
        with pytest.raises((ValueError, KeyError)):
            self.DU().deserialize(words, word_bw=32)


class TestDataUnionCodegen:
    def setup_method(self):
        self.reg, self.DU = _make_du_registry("Cg")

    def test_cpp_class_name_uses_registry(self):
        assert "Cg" in self.DU.cpp_class_name()

    def test_include_path_ends_with_h(self):
        path = self.DU.include_path()
        assert path.endswith(".h")

    def test_gen_include_decl_contains_struct(self):
        decl = self.DU._gen_include_decl(word_bw_supported=[32])
        assert f"struct {self.DU.cpp_class_name()}" in decl

    def test_gen_include_decl_contains_payload_bits(self):
        decl = self.DU._gen_include_decl(word_bw_supported=[32])
        assert "payload_bits" in decl

    def test_gen_include_decl_contains_getters_for_all_schemas(self):
        decl = self.DU._gen_include_decl(word_bw_supported=[32])
        for _, schema_cls in self.reg.items():
            assert f"get_{schema_cls.__name__}" in decl
            assert f"set_{schema_cls.__name__}" in decl

    def test_gen_include_decl_contains_read_write_array(self):
        decl = self.DU._gen_include_decl(word_bw_supported=[32])
        assert "write_array" in decl
        assert "read_array" in decl

    def test_gen_include_decl_no_helpers_when_no_word_bw(self):
        decl = self.DU._gen_include_decl()
        assert "write_array" not in decl
        assert "read_array" not in decl
        assert "nwords" not in decl

    def test_gen_include_writes_file(self, tmp_path: Path):
        DU = self.DU
        _, DU = _make_du_registry("GenInc")
        cfg = BuildConfig(root_dir=tmp_path)
        # Generate dependencies first
        for _, schema_cls in DU.registry.items():
            schema_cls.as_buildable(word_bw_supported=[32]).run(cfg)
        DU.hdr_type.as_buildable(word_bw_supported=[32]).run(cfg)
        out_path = DU.gen_include(cfg=cfg, word_bw_supported=[32])
        content = out_path.read_text(encoding="utf-8")
        assert f"struct {DU.cpp_class_name()}" in content
        assert "payload_bits" in content
        assert "#ifndef" in content


class TestSchemaIDFieldScopedEnumConversions:
    """The C++ side of SchemaIDField is a scoped enum; the Python side inherits IntField.

    A scoped enum has no implicit conversion to or from an integer, so IntField's identity
    ``to_uint_expr`` and DataField's C-style ``from_uint_expr`` both emit code that no
    conforming C++ compiler accepts.  These pin the casts so the regression cannot come back
    silently -- the failure it caused was a Vitis csim compile error, which only the (slow,
    tool-dependent) ``-m vitis`` suite would otherwise catch.
    """

    @staticmethod
    def _field():
        reg = SchemaRegistry("ScopedEnumConv")

        @register_schema(schema_id=1, registry=reg)
        class Pkt(DataList):
            elements = {"a": IntField.specialize(bitwidth=8, signed=False)}

        return SchemaIDField.specialize(registry=reg, bitwidth=16)

    def test_pack_casts_through_unsigned(self) -> None:
        expr = self._field().to_uint_expr("data.schema_id")
        # Bare `data.schema_id` would be `ap_range_ref = <scoped enum>`: no viable operator=.
        assert "static_cast<unsigned int>" in expr
        assert expr != "data.schema_id"

    def test_write_path_casts_too(self) -> None:
        # The write_array / write_stream path goes through to_uint_value_expr, not to_uint_expr.
        expr = self._field().to_uint_value_expr("self->schema_id")
        assert "static_cast<unsigned int>" in expr

    def test_unpack_casts_through_unsigned(self) -> None:
        cls = self._field()
        expr = cls.from_uint_expr("packed.range(15, 0)")
        # A C-style `(EnumT)(ap_range_ref)` is an invalid cast; it must go via an integer.
        assert f"static_cast<{cls.cpp_class_name()}>" in expr
        assert "static_cast<unsigned int>" in expr

    def test_generated_header_has_no_bare_enum_assignment(self, tmp_path: Path) -> None:
        _, DU = _make_du_registry("ScopedEnumHdr")
        cfg = BuildConfig(root_dir=tmp_path)
        for _, schema_cls in DU.registry.items():
            schema_cls.as_buildable(word_bw_supported=[32]).run(cfg)
        DU.hdr_type.as_buildable(word_bw_supported=[32]).run(cfg)
        hdr = next(tmp_path.rglob("*_hdr.h"))
        text = hdr.read_text(encoding="utf-8")
        for line in text.splitlines():
            if ".range(" in line and "schema_id" in line and "=" in line:
                assert "static_cast" in line, f"uncast enum conversion: {line.strip()}"
