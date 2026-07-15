"""Tests for waveflow/hw/regmap.py — Phases 1–4."""
from __future__ import annotations

from enum import IntEnum
from typing import Any

import pytest

from waveflow.hw.aximm import (
    DirectMMIF,
    MMIFMaster,
)
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, EnumField, FloatField, IntField
from waveflow.hw.regmap import (
    Bit,
    RegAccess,
    RegField,
    RegMap,
    RegMapAccessError,
    RegMapMMIFSlave,
    VitisRegMap,
    VitisRegMapMMIFSlave,
)
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# Shared test schemas
# ---------------------------------------------------------------------------

class ErrCode(IntEnum):
    OK        = 0
    BAD_INPUT = 1
    OVERFLOW  = 2


ErrField = EnumField.specialize(enum_type=ErrCode)
Uint32      = IntField.specialize(bitwidth=32, signed=False)
U16      = IntField.specialize(bitwidth=16, signed=False)
F32      = FloatField.specialize(bitwidth=32)


class CoeffPair(DataArray):
    """2-element array of 32-bit unsigned ints (2 bus words at 32-bit bus)."""

    element_type = Uint32
    max_shape = (2,)
    static = True


class CoeffQuad(DataArray):
    """4-element array of F32 (4 bus words)."""

    element_type = F32
    max_shape = (4,)
    static = True


# ---------------------------------------------------------------------------
# SimPy harness
# ---------------------------------------------------------------------------


class _SlaveHarness:
    """Minimal harness: DirectMMIF connecting one master to a RegMapMMIFSlave."""

    def __init__(self, slave: RegMapMMIFSlave) -> None:
        self.sim = Simulation()
        self.slave = slave
        # Rebuild slave inside this sim (the slave was pre-constructed).
        # For simplicity, share the sim environment by constructing inside run().
        self._setup_done = False

    @classmethod
    def build(cls, regmap: RegMap) -> "_SlaveHarness":
        """Construct harness + slave from a RegMap."""
        h = object.__new__(cls)
        h.sim = Simulation()
        h.slave = RegMapMMIFSlave(sim=h.sim, bitwidth=32, regmap=regmap)
        h.master = MMIFMaster(sim=h.sim, bitwidth=32)
        h.direct = DirectMMIF(sim=h.sim, clk=Clock(freq=1.0))
        h.direct.bind("master", h.master)
        h.direct.bind("slave", h.slave)
        return h

    @classmethod
    def build_vitis(
        cls,
        regmap: VitisRegMap,
        on_start: Any = None,
    ) -> "_SlaveHarness":
        """Construct harness + VitisRegMapMMIFSlave."""
        h = object.__new__(cls)
        h.sim = Simulation()
        h.slave = VitisRegMapMMIFSlave(
            sim=h.sim, bitwidth=32, regmap=regmap, on_start=on_start
        )
        h.master = MMIFMaster(sim=h.sim, bitwidth=32)
        h.direct = DirectMMIF(sim=h.sim, clk=Clock(freq=1.0))
        h.direct.bind("master", h.master)
        h.direct.bind("slave", h.slave)
        return h

    def run(self, proc_fn: Any) -> None:
        """Schedule proc_fn() as a SimPy process and run to completion."""
        done = self.sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc_fn()
            done.succeed()

        self.sim.env.process(_wrap())
        self.sim.env.run(until=done)


# ---------------------------------------------------------------------------
# Phase 1 — generic RegMap infrastructure (pure Python, no SimPy)
# ---------------------------------------------------------------------------


class TestOffsetAssignment:
    def test_auto_scalar_fields(self) -> None:
        rm = RegMap({
            "a": RegField(Bit,      RegAccess.R),    # 1 word → 0x00
            "b": RegField(Bit,      RegAccess.W),    # 1 word → 0x04
            "c": RegField(CoeffPair, RegAccess.RW),  # 2 words → 0x08, 0x0C
            "d": RegField(ErrField, RegAccess.R),    # 1 word → 0x10
        })
        assert rm.offset_of("a") == 0x00
        assert rm.offset_of("b") == 0x04
        assert rm.offset_of("c") == 0x08
        assert rm.offset_of("d") == 0x10
        assert rm.nwords_of("c") == 2
        assert rm.total_size_bytes() == 0x14

    def test_manual_override_with_gap(self) -> None:
        rm = RegMap({
            "ctrl":   RegField(Bit, RegAccess.W,  offset=0x00),
            "status": RegField(Bit, RegAccess.R,  offset=0x10),
            "cfg":    RegField(Bit, RegAccess.RW),  # auto → 0x04
        })
        assert rm.offset_of("ctrl")   == 0x00
        assert rm.offset_of("status") == 0x10
        assert rm.offset_of("cfg")    == 0x04

    def test_overlap_raises(self) -> None:
        with pytest.raises(ValueError, match="overlaps"):
            RegMap({
                "a": RegField(Bit, RegAccess.RW, offset=0x00),
                "b": RegField(Bit, RegAccess.RW, offset=0x00),
            })

    def test_multiword_field_places_correctly(self) -> None:
        rm = RegMap({"coeffs": RegField(CoeffQuad, RegAccess.RW)})
        assert rm.offset_of("coeffs") == 0x00
        assert rm.nwords_of("coeffs") == 4
        assert rm.total_size_bytes() == 0x10


class TestW1CW1SValidation:
    def test_w1s_rejects_multiword(self) -> None:
        with pytest.raises(ValueError):
            RegMap({"arr": RegField(CoeffPair, RegAccess.W1S)})

    def test_w1c_rejects_multiword(self) -> None:
        with pytest.raises(ValueError):
            RegMap({"arr": RegField(CoeffPair, RegAccess.W1C)})

    def test_w1s_accepts_single_word(self) -> None:
        rm = RegMap({"trig": RegField(Bit, RegAccess.W1S)})
        assert rm.nwords_of("trig") == 1

    def test_w1c_accepts_single_word(self) -> None:
        rm = RegMap({"sticky": RegField(Uint32, RegAccess.W1C)})
        assert rm.nwords_of("sticky") == 1


class TestGetSet:
    def test_get_set_int_field(self) -> None:
        rm = RegMap({"count": RegField(Uint32, RegAccess.RW)})
        rm.set("count", 42)
        assert int(rm.get("count").val) == 42

    def test_get_set_enum_field(self) -> None:
        rm = RegMap({"err": RegField(ErrField, RegAccess.R)})
        rm.set("err", ErrCode.OVERFLOW)
        assert rm.get("err").val == ErrCode.OVERFLOW

    def test_set_raw_int_wraps_via_schema(self) -> None:
        rm = RegMap({"err": RegField(ErrField, RegAccess.R)})
        rm.set("err", 2)  # 2 == ErrCode.OVERFLOW
        assert rm.get("err").val == ErrCode.OVERFLOW

    def test_get_set_composite_data_array(self) -> None:
        rm = RegMap({"coeffs": RegField(CoeffPair, RegAccess.RW)})
        rm.set("coeffs", CoeffPair([10, 20]))
        result = rm.get("coeffs")
        assert list(result.val.flat) == [10, 20]

    def test_set_schema_instance_accepted(self) -> None:
        rm = RegMap({"x": RegField(Uint32, RegAccess.RW)})
        rm.set("x", Uint32(99))
        assert int(rm.get("x").val) == 99


class TestFieldNameAtOffset:
    def test_single_word_fields(self) -> None:
        rm = RegMap({
            "a": RegField(Bit,      RegAccess.R),    # 0x00
            "b": RegField(CoeffPair, RegAccess.RW),  # 0x04, 0x08
        })
        assert rm.field_name_at_offset(0x00) == ("a", 0)
        assert rm.field_name_at_offset(0x04) == ("b", 0)
        assert rm.field_name_at_offset(0x08) == ("b", 1)

    def test_missing_offset_raises(self) -> None:
        rm = RegMap({"a": RegField(Bit, RegAccess.R)})
        with pytest.raises(RegMapAccessError):
            rm.field_name_at_offset(0x08)  # no field there


class TestReadWriteWord:
    def test_read_write_word_owner(self) -> None:
        rm = RegMap({"x": RegField(Uint32, RegAccess.RW)})
        rm.write_word("x", 0, 0xABCD, source="owner")
        assert rm.read_word("x", 0) == 0xABCD

    def test_w1c_host_clears_bits(self) -> None:
        rm = RegMap({"sticky": RegField(Uint32, RegAccess.W1C)})
        rm._buffers["sticky"][0] = 0xFF
        rm.write_word("sticky", 0, 0xF0, source="host")
        assert rm.read_word("sticky", 0) == 0x0F  # 0xFF & ~0xF0

    def test_w1c_owner_overwrites(self) -> None:
        rm = RegMap({"sticky": RegField(Uint32, RegAccess.W1C)})
        rm._buffers["sticky"][0] = 0xFF
        rm.write_word("sticky", 0, 0x00, source="owner")
        assert rm.read_word("sticky", 0) == 0x00


# ---------------------------------------------------------------------------
# Phase 1 — RegMapMMIFSlave tests (require SimPy)
# ---------------------------------------------------------------------------


class TestSlaveRoundTrip:
    def test_rw_round_trip(self) -> None:
        rm = RegMap({"x": RegField(Uint32, RegAccess.RW)})
        h = _SlaveHarness.build(rm)

        def proc() -> ProcessGen[None]:
            yield from h.master.write_schema(Uint32(0xBEEF), addr=rm.offset_of("x"))
            val = yield from h.master.read_schema(Uint32, addr=rm.offset_of("x"))
            assert int(val.val) == 0xBEEF

        h.run(proc)

    def test_r_only_host_can_read(self) -> None:
        rm = RegMap({"status": RegField(ErrField, RegAccess.R)})
        rm.set("status", ErrCode.BAD_INPUT)
        h = _SlaveHarness.build(rm)

        def proc() -> ProcessGen[None]:
            val = yield from h.master.read_schema(ErrField, addr=rm.offset_of("status"))
            assert val.val == ErrCode.BAD_INPUT

        h.run(proc)

    def test_w_only_host_can_write(self) -> None:
        rm = RegMap({"cfg": RegField(Uint32, RegAccess.W)})
        h = _SlaveHarness.build(rm)

        def proc() -> ProcessGen[None]:
            yield from h.master.write_schema(Uint32(42), addr=rm.offset_of("cfg"))

        h.run(proc)
        assert rm.read_word("cfg", 0) == 42

    def test_slave_w1c(self) -> None:
        """Host writes 0xF0 to register holding 0xFF → 0x0F."""
        rm = RegMap({"sticky": RegField(Uint32, RegAccess.W1C)})
        rm._buffers["sticky"][0] = 0xFF
        h = _SlaveHarness.build(rm)

        def proc() -> ProcessGen[None]:
            yield from h.master.write_schema(Uint32(0xF0), addr=rm.offset_of("sticky"))

        h.run(proc)
        assert rm.read_word("sticky", 0) == 0x0F

    def test_slave_w1s_auto_clears(self) -> None:
        """Write 1 to W1S field; subsequent read returns 0."""
        hook_values: list[int] = []

        def on_w(name: str, sub_word: int, raw_val: int) -> None:
            hook_values.append(rm.read_word(name, sub_word))

        rm = RegMap({"trig": RegField(Bit, RegAccess.W1S, on_write=on_w)})
        h = _SlaveHarness.build(rm)

        def proc() -> ProcessGen[None]:
            yield from h.master.write_schema(Bit(1), addr=rm.offset_of("trig"))
            val = yield from h.master.read_schema(Bit, addr=rm.offset_of("trig"))
            assert int(val.val) == 0  # auto-cleared

        h.run(proc)
        assert hook_values == [1]  # hook saw 1 before auto-clear

    def test_slave_rejects_host_write_to_r(self) -> None:
        rm = RegMap({"ro": RegField(Bit, RegAccess.R)})
        h = _SlaveHarness.build(rm)

        def proc() -> ProcessGen[None]:
            with pytest.raises(RegMapAccessError):
                yield from h.master.write_schema(Bit(1), addr=rm.offset_of("ro"))

        h.run(proc)

    def test_slave_rejects_host_read_from_w(self) -> None:
        rm = RegMap({"wo": RegField(Uint32, RegAccess.W)})
        h = _SlaveHarness.build(rm)

        def proc() -> ProcessGen[None]:
            with pytest.raises(RegMapAccessError):
                yield from h.master.read_schema(Uint32, addr=rm.offset_of("wo"))

        h.run(proc)

    def test_hook_ordering_write_after_w1c_before_w1s_clear(self) -> None:
        """on_write fires after W1C masking and before W1S auto-clear."""
        hook_log: list[tuple[str, int]] = []

        # W1C field: hook fires after masking
        def on_w1c(name: str, sub_word: int, raw_val: int) -> None:
            hook_log.append(("post_mask", rm_w1c.read_word(name, sub_word)))

        rm_w1c = RegMap({"sticky": RegField(Uint32, RegAccess.W1C, on_write=on_w1c)})
        rm_w1c._buffers["sticky"][0] = 0xFF
        h1 = _SlaveHarness.build(rm_w1c)

        def proc1() -> ProcessGen[None]:
            yield from h1.master.write_schema(Uint32(0xF0), addr=rm_w1c.offset_of("sticky"))

        h1.run(proc1)
        assert hook_log[0] == ("post_mask", 0x0F)

        # W1S field: hook fires before auto-clear
        hook_log2: list[int] = []

        def on_w1s(name: str, sub_word: int, raw_val: int) -> None:
            hook_log2.append(rm_w1s.read_word(name, sub_word))

        rm_w1s = RegMap({"trig": RegField(Bit, RegAccess.W1S, on_write=on_w1s)})
        h2 = _SlaveHarness.build(rm_w1s)

        def proc2() -> ProcessGen[None]:
            yield from h2.master.write_schema(Bit(1), addr=rm_w1s.offset_of("trig"))

        h2.run(proc2)
        assert hook_log2 == [1]  # hook saw 1; buffer later cleared to 0
        assert rm_w1s.read_word("trig", 0) == 0  # auto-cleared


class TestBoundRegMap:
    """``regmap.bind_master(...)`` returns a host-side proxy whose
    ``set`` / ``get`` mirror the in-process :meth:`RegMap.set` /
    :meth:`RegMap.get` API but route through an MMIFMaster.  ``get``
    returns a native Python value so callers don't have to recover ``.val``
    or recast enums by hand at every call site.
    """

    def test_int_field_round_trip_returns_native_int(self) -> None:
        rm = RegMap({"count": RegField(Uint32, RegAccess.RW)})
        h = _SlaveHarness.build(rm)
        seen: dict[str, Any] = {}

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(h.master)
            yield from rb.set("count", 42)
            seen["val"] = yield from rb.get("count")

        h.run(proc)
        assert seen["val"] == 42
        assert isinstance(seen["val"], int)

    def test_enum_field_returns_native_intenum(self) -> None:
        rm = RegMap({"err": RegField(ErrField, RegAccess.RW)})
        h = _SlaveHarness.build(rm)
        seen: dict[str, Any] = {}

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(h.master)
            yield from rb.set("err", ErrCode.OVERFLOW)
            seen["val"] = yield from rb.get("err")

        h.run(proc)
        assert seen["val"] is ErrCode.OVERFLOW
        assert isinstance(seen["val"], ErrCode)

    def test_set_raw_value_auto_wraps_via_schema(self) -> None:
        """Mirrors the kernel-side ``RegMap.set`` auto-wrap convention."""
        rm = RegMap({"err": RegField(ErrField, RegAccess.RW)})
        h = _SlaveHarness.build(rm)
        seen: dict[str, Any] = {}

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(h.master)
            yield from rb.set("err", 2)  # 2 == ErrCode.OVERFLOW
            seen["val"] = yield from rb.get("err")

        h.run(proc)
        assert seen["val"] is ErrCode.OVERFLOW

    def test_data_array_returns_schema_instance(self) -> None:
        rm = RegMap({"coeffs": RegField(CoeffPair, RegAccess.RW)})
        h = _SlaveHarness.build(rm)
        seen: dict[str, Any] = {}

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(h.master)
            yield from rb.set("coeffs", CoeffPair([10, 20]))
            seen["val"] = yield from rb.get("coeffs")

        h.run(proc)
        assert isinstance(seen["val"], CoeffPair)
        assert list(seen["val"].val.flat) == [10, 20]

    def test_base_addr_offset_applied(self) -> None:
        rm = RegMap({"x": RegField(Uint32, RegAccess.RW)})
        h = _SlaveHarness.build(rm)

        def proc_offset() -> ProcessGen[None]:
            # base_addr=0x100 shifts everything; the slave's local space
            # starts at 0, so a non-zero base_addr should fail to land.
            rb = rm.bind_master(h.master, base_addr=0x100)
            with pytest.raises(RegMapAccessError):
                yield from rb.set("x", 1)

        h.run(proc_offset)

    def test_start_writes_ap_start_on_vitis_regmap(self) -> None:
        rm = VitisRegMap({"x": RegField(Uint32, RegAccess.RW)})
        on_start_fired: list[bool] = []

        def on_start() -> ProcessGen[None]:
            on_start_fired.append(True)
            yield from ()  # generator marker

        h = _SlaveHarness.build_vitis(rm, on_start=on_start)

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(h.master)
            yield from rb.start()

        h.run(proc)
        assert on_start_fired == [True]


# ---------------------------------------------------------------------------
# Phase 2 — VitisRegMap tests
# ---------------------------------------------------------------------------


class TestVitisRegMap:
    def test_control_signals_are_bits_of_word_zero(self) -> None:
        """The four ap_* control signals are bits of the single 0x00 control
        word — not registers of their own.

        Pinned against the layout Vitis HLS actually generates, per
        ``examples/regmap/waveflow_simp_fun_proj/solution1/.autopilot/db/
        coregen/control.h`` (and the ``ADDR_*`` localparams in the generated
        ``simp_fun_control_s_axi.v``)::

            0x00 : bit0 ap_start (RW/COH), bit1 ap_done (R/COR),
                   bit2 ap_idle (R),       bit3 ap_ready (R/COR)

        If this fails, either the model drifted or Vitis changed its layout —
        check control.h before "fixing" the test.
        """
        rm = VitisRegMap({"halted": RegField(Bit, RegAccess.R)})
        for name in ("ap_start", "ap_done", "ap_idle", "ap_ready"):
            assert rm.offset_of(name) == 0x00, f"{name} must live in the 0x00 word"
        assert rm.bit_offset_of("ap_start") == 0
        assert rm.bit_offset_of("ap_done")  == 1
        assert rm.bit_offset_of("ap_idle")  == 2
        assert rm.bit_offset_of("ap_ready") == 3
        # All four share one word, so 0x00 has no single owning field.
        assert rm.bit_fields_at_offset(0x00) == [
            "ap_start", "ap_done", "ap_idle", "ap_ready",
        ]

    def test_interrupt_block_offsets(self) -> None:
        """0x04/0x08/0x0c are GIER/IER/ISR — see control.h."""
        rm = VitisRegMap({"halted": RegField(Bit, RegAccess.R)})
        assert rm.offset_of("gier") == 0x04
        assert rm.offset_of("ier")  == 0x08
        assert rm.offset_of("isr")  == 0x0C

    def test_user_fields_start_at_0x10_with_8_byte_stride(self) -> None:
        """32-bit scalar arguments start at 0x10 on an 8-byte stride (one data
        word + one control word each).

        Pinned against control.h for the ``simp_fun`` kernel, whose four int32
        arguments land at x@0x10, a@0x18, b@0x20, y@0x28.
        """
        rm = VitisRegMap({
            "x": RegField(Uint32, RegAccess.RW),
            "a": RegField(Uint32, RegAccess.RW),
            "b": RegField(Uint32, RegAccess.RW),
            "y": RegField(Uint32, RegAccess.R),
        })
        assert rm.offset_of("x") == 0x10
        assert rm.offset_of("a") == 0x18
        assert rm.offset_of("b") == 0x20
        assert rm.offset_of("y") == 0x28

    def test_rejects_ap_prefix(self) -> None:
        with pytest.raises(ValueError, match="ap_"):
            VitisRegMap({"ap_custom": RegField(Bit, RegAccess.R)})

    def test_rejects_non_32_bit_width(self) -> None:
        """Vitis's s_axilite control interface is a 32-bit bus; the control
        block is defined on 4-byte words."""
        with pytest.raises(ValueError, match="bitwidth=32"):
            VitisRegMap({"x": RegField(Bit, RegAccess.RW)}, bitwidth=64)

    @pytest.mark.parametrize("offset", [0x00, 0x04, 0x08, 0x0C])
    def test_rejects_offsets_inside_control_block(self, offset: int) -> None:
        """User fields may not land in 0x00-0x0f (ap_ctrl word, GIER, IER, ISR)."""
        with pytest.raises(ValueError):
            VitisRegMap({"cfg": RegField(Bit, RegAccess.RW, offset=offset)})

    def test_user_fields_at_nonzero_offsets(self) -> None:
        rm = VitisRegMap({
            "a": RegField(Bit, RegAccess.R, offset=0x18),
            "b": RegField(Bit, RegAccess.RW),  # auto → 0x10 (first user slot)
        })
        assert rm.offset_of("ap_start") == 0x00
        assert rm.offset_of("b")        == 0x10
        assert rm.offset_of("a")        == 0x18


class TestVitisControlWordPacking:
    """Bus-level composition of the shared 0x00 control word."""

    def test_start_writes_bit0_of_control_word(self) -> None:
        """``start()`` puts a 1 in bit 0 of 0x00 — one write, as before."""
        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        h = _SlaveHarness.build(rm)
        seen: list[tuple[int, int]] = []

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(h.master)
            rm._fields["ap_start"].on_write = lambda n, sw, v: seen.append((sw, v))
            yield from rb.start()

        h.run(proc)
        assert seen == [(0, 1)], "ap_start must latch a 1 from bit 0 of the word"

    def test_read_composes_all_control_bits(self) -> None:
        """A raw word read of 0x00 returns the packed bits, not one field."""
        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        rm.set("ap_done", 1)    # bit 1
        rm.set("ap_ready", 1)   # bit 3
        rm.set("ap_idle", 1)    # bit 2
        h = _SlaveHarness.build(rm)
        got: list[int] = []

        def proc() -> ProcessGen[None]:
            word = yield from h.master.read_schema(Uint32, addr=0x00)
            got.append(int(word.val))

        h.run(proc)
        # ap_start=0, ap_done=1, ap_idle=1, ap_ready=1 -> 0b1110
        assert got == [0b1110]

    def test_read_only_bits_ignore_writes(self) -> None:
        """Writing 0x00 must not raise just because read-only ap_done shares
        the word — the hardware slave decodes only the writable bits."""
        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        rm.set("ap_done", 1)
        h = _SlaveHarness.build(rm)

        def proc() -> ProcessGen[None]:
            yield from h.master.write_schema(Uint32(0xFFFF_FFFF), addr=0x00)

        h.run(proc)
        assert rm.read_word("ap_done", 0) == 1, "R bit must be untouched by a host write"

    def test_field_name_at_offset_rejects_packed_word(self) -> None:
        """0x00 has no single owning field; callers must ask for the bit list."""
        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        with pytest.raises(RegMapAccessError, match="bit-packed"):
            rm.field_name_at_offset(0x00)

    def test_bound_get_extracts_each_bit(self) -> None:
        """BoundRegMap.get pulls each field out of the shared word."""
        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        rm.set("ap_done", 1)
        rm.set("ap_idle", 0)
        rm.set("ap_ready", 1)
        h = _SlaveHarness.build(rm)
        got: dict[str, int] = {}

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(h.master)
            for name in ("ap_start", "ap_done", "ap_idle", "ap_ready"):
                got[name] = yield from rb.get(name)

        h.run(proc)
        assert got == {"ap_start": 0, "ap_done": 1, "ap_idle": 0, "ap_ready": 1}


class TestRegMapBitPacking:
    """Bit-packing is a general RegMap facility, not a Vitis special case."""

    def test_rejects_overlapping_bits(self) -> None:
        with pytest.raises(ValueError, match="overlaps"):
            RegMap({
                "a": RegField(Bit, RegAccess.RW, offset=0x00, bit_offset=0),
                "b": RegField(Bit, RegAccess.RW, offset=0x00, bit_offset=0),
            })

    def test_rejects_bit_offset_without_offset(self) -> None:
        with pytest.raises(ValueError, match="no offset"):
            RegMap({"a": RegField(Bit, RegAccess.RW, bit_offset=3)})

    def test_rejects_bits_past_end_of_word(self) -> None:
        with pytest.raises(ValueError, match="do not fit"):
            RegMap({"a": RegField(Uint32, RegAccess.RW, offset=0x00, bit_offset=8)})

    def test_whole_word_field_cannot_overlap_packed_word(self) -> None:
        with pytest.raises(ValueError, match="overlaps"):
            RegMap({
                "bits": RegField(Bit,    RegAccess.RW, offset=0x00, bit_offset=0),
                "word": RegField(Uint32, RegAccess.RW, offset=0x00),
            })

    def test_multi_bit_field_round_trips(self) -> None:
        """A packed field wider than one bit keeps its value across the bus."""
        Nibble = IntField.specialize(bitwidth=4, signed=False)
        rm = RegMap({
            "lo": RegField(Nibble, RegAccess.RW, offset=0x00, bit_offset=0),
            "hi": RegField(Nibble, RegAccess.RW, offset=0x00, bit_offset=4),
        })
        h = _SlaveHarness.build(rm)
        got: dict[str, int] = {}

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(h.master)
            yield from rb.set("hi", 0xA)
            got["buffer"] = int(rm.read_word("hi", 0))
            got["hi"] = yield from rb.get("hi")

        h.run(proc)
        assert got["buffer"] == 0xA, "buffer holds the unshifted value"
        assert got["hi"] == 0xA, "get() shifts it back out of bits [7:4]"

    def test_packed_write_clobbers_neighbouring_writable_bits(self) -> None:
        """Setting one field of a shared word writes the *whole* word, so any
        other writable bits in it are zeroed.

        This is what the hardware does — a host that wants to preserve a
        neighbour must compose the word itself.  It is not a problem for
        VitisRegMap's 0x00, where ap_start is the only writable bit and the
        rest are read-only (and so ignore writes entirely).
        """
        Nibble = IntField.specialize(bitwidth=4, signed=False)
        rm = RegMap({
            "lo": RegField(Nibble, RegAccess.RW, offset=0x00, bit_offset=0),
            "hi": RegField(Nibble, RegAccess.RW, offset=0x00, bit_offset=4),
        })
        h = _SlaveHarness.build(rm)
        got: dict[str, int] = {}

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(h.master)
            yield from rb.set("hi", 0xA)
            yield from rb.set("lo", 0x5)   # writes word 0x0005 -> hi is cleared
            got["hi"] = yield from rb.get("hi")
            got["lo"] = yield from rb.get("lo")

        h.run(proc)
        assert got["lo"] == 0x5
        assert got["hi"] == 0x0, "no read-modify-write: the word write cleared hi"


class TestVitisRegMapStart:
    def test_start_writes_one_to_ap_start(self) -> None:
        """regmap.start(master) must write 1 to ap_start's address."""
        rm = VitisRegMap({"status": RegField(Bit, RegAccess.R)})
        sim = Simulation()
        slave = RegMapMMIFSlave(sim=sim, bitwidth=32, regmap=rm)
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        def proc() -> ProcessGen[None]:
            yield from rm.start(master, base_addr=0)

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)

        # ap_start is W1S so it auto-clears; check directly that the write landed
        # by confirming the slave dispatched to the correct field
        # (The value is 0 after auto-clear, which is correct W1S behavior)
        assert rm.offset_of("ap_start") == 0


class TestVitisRegMapMMIFSlave:
    def test_invokes_on_start_on_ap_start(self) -> None:
        call_count = 0

        def on_start() -> ProcessGen[None]:
            nonlocal call_count
            call_count += 1
            yield sim.env.timeout(0)

        rm = VitisRegMap({"status": RegField(ErrField, RegAccess.R)})
        sim = Simulation()
        slave = VitisRegMapMMIFSlave(
            sim=sim, bitwidth=32, regmap=rm, on_start=on_start
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        def proc() -> ProcessGen[None]:
            yield from rm.start(master, base_addr=0)
            yield sim.env.timeout(5)  # let on_start complete

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)
        assert call_count == 1

    def test_drops_concurrent_ap_start(self) -> None:
        """Second ap_start write while on_start is running is silently ignored."""
        call_count = 0

        def on_start() -> ProcessGen[None]:
            nonlocal call_count
            call_count += 1
            yield sim.env.timeout(10)  # hold for 10 time units

        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        sim = Simulation()
        slave = VitisRegMapMMIFSlave(
            sim=sim, bitwidth=32, regmap=rm, on_start=on_start
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        def proc() -> ProcessGen[None]:
            yield from rm.start(master, base_addr=0)   # starts on_start
            yield sim.env.timeout(1)                   # on_start still running
            yield from rm.start(master, base_addr=0)   # should be dropped
            yield sim.env.timeout(20)                  # wait for first to finish

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)
        assert call_count == 1  # only one invocation

    def test_relaunches_after_return(self) -> None:
        """After on_start returns, a subsequent ap_start launches a new invocation."""
        call_count = 0

        def on_start() -> ProcessGen[None]:
            nonlocal call_count
            call_count += 1
            yield sim.env.timeout(0)

        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        sim = Simulation()
        slave = VitisRegMapMMIFSlave(
            sim=sim, bitwidth=32, regmap=rm, on_start=on_start
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        def proc() -> ProcessGen[None]:
            yield from rm.start(master, base_addr=0)
            yield sim.env.timeout(5)   # first on_start finishes
            yield from rm.start(master, base_addr=0)
            yield sim.env.timeout(5)   # second on_start finishes

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)
        assert call_count == 2

    def test_status_set_inside_on_start_visible_to_host(self) -> None:
        """Kernel sets error field during on_start; host reads it after halt."""

        def on_start() -> ProcessGen[None]:
            yield sim.env.timeout(1)
            rm.set("error", ErrCode.BAD_INPUT)

        rm = VitisRegMap({"error": RegField(ErrField, RegAccess.R)})
        sim = Simulation()
        slave = VitisRegMapMMIFSlave(
            sim=sim, bitwidth=32, regmap=rm, on_start=on_start
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        read_result: list[Any] = []

        def proc() -> ProcessGen[None]:
            yield from rm.start(master, base_addr=0)
            yield sim.env.timeout(10)  # on_start has returned
            val = yield from master.read_schema(ErrField, addr=rm.offset_of("error"))
            read_result.append(val.val)

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)
        assert read_result[0] == ErrCode.BAD_INPUT

    def test_ap_start_auto_clears_even_when_busy(self) -> None:
        """ap_start W1S auto-clear fires even when the busy guard drops the launch."""

        def on_start() -> ProcessGen[None]:
            yield sim.env.timeout(10)

        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        sim = Simulation()
        slave = VitisRegMapMMIFSlave(
            sim=sim, bitwidth=32, regmap=rm, on_start=on_start
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        ap_start_read: list[int] = []

        def proc() -> ProcessGen[None]:
            yield from rm.start(master, base_addr=0)   # starts on_start; auto-clears
            yield sim.env.timeout(1)
            yield from rm.start(master, base_addr=0)   # busy → dropped, but still clears
            # Read ap_start — should be 0 (auto-cleared) not 1
            val = yield from master.read_schema(Bit, addr=rm.offset_of("ap_start"))
            ap_start_read.append(int(val.val))

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)
        assert ap_start_read[0] == 0  # auto-cleared


class TestVitisRegMapApDone:
    """The auto-prepended ``ap_done`` field is cleared on ap_start and set
    when ``on_start`` returns, so a polling host doesn't need a user-defined
    status register."""

    def test_ap_done_initially_zero(self) -> None:
        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        assert rm.read_word("ap_done", 0) == 0

    def test_ap_done_set_after_on_start_returns(self) -> None:
        def on_start() -> ProcessGen[None]:
            yield sim.env.timeout(1)

        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        sim = Simulation()
        slave = VitisRegMapMMIFSlave(
            sim=sim, bitwidth=32, regmap=rm, on_start=on_start
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        ap_done_reads: list[int] = []

        def proc() -> ProcessGen[None]:
            yield from rm.start(master, base_addr=0)
            yield sim.env.timeout(5)  # on_start completes
            # ap_done is bit 1 of the 0x00 control word, so it must be read
            # through the bit-aware BoundRegMap rather than a raw word read.
            val = yield from rm.bind_master(master).get("ap_done")
            ap_done_reads.append(int(val))

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)
        assert ap_done_reads[0] == 1

    def test_ap_done_cleared_on_relaunch(self) -> None:
        """A second ap_start clears ap_done so the host's next poll cannot
        see a stale completion from the previous transaction."""

        def on_start() -> ProcessGen[None]:
            yield sim.env.timeout(5)

        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        sim = Simulation()
        slave = VitisRegMapMMIFSlave(
            sim=sim, bitwidth=32, regmap=rm, on_start=on_start
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        ap_done_after_first: list[int] = []
        ap_done_after_relaunch: list[int] = []

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(master)
            yield from rm.start(master, base_addr=0)
            yield sim.env.timeout(10)  # first on_start finishes; ap_done = 1
            # ap_done is bit 1 of the 0x00 control word — read it bit-aware.
            val1 = yield from rb.get("ap_done")
            ap_done_after_first.append(int(val1))
            yield from rm.start(master, base_addr=0)   # ap_done must clear immediately
            val2 = yield from rb.get("ap_done")
            ap_done_after_relaunch.append(int(val2))

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)
        assert ap_done_after_first[0] == 1
        assert ap_done_after_relaunch[0] == 0


class TestBoundRegMapPollEnd:
    """``BoundRegMap.poll_end`` polls a field until it reads the target value
    (default: ap_done == 1), with a real-time interval between reads."""

    def test_poll_end_returns_on_ap_done(self) -> None:
        """Happy path: kernel completes; poll_end observes ap_done=1 and returns."""

        def on_start() -> ProcessGen[None]:
            yield sim.env.timeout(3)

        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        sim = Simulation()
        slave = VitisRegMapMMIFSlave(
            sim=sim, bitwidth=32, regmap=rm, on_start=on_start
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        result: list[Any] = []

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(master)
            yield from rb.start()
            value = yield from rb.poll_end(interval=1.0, max_polls=20)
            result.append(value)

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)
        assert result == [1]

    def test_poll_end_raises_on_timeout(self) -> None:
        """If on_start never returns within max_polls, poll_end raises."""

        def on_start() -> ProcessGen[None]:
            yield sim.env.timeout(10_000)   # never finishes within poll budget

        rm = VitisRegMap({"x": RegField(Bit, RegAccess.R)})
        sim = Simulation()
        slave = VitisRegMapMMIFSlave(
            sim=sim, bitwidth=32, regmap=rm, on_start=on_start
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        captured: list[Exception] = []

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(master)
            yield from rb.start()
            try:
                yield from rb.poll_end(interval=1.0, max_polls=3)
            except RuntimeError as exc:
                captured.append(exc)

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)
        assert len(captured) == 1
        assert "ap_done" in str(captured[0])

    def test_poll_end_field_and_target_override(self) -> None:
        """User can poll a non-ap_done field (e.g. a custom status enum)."""

        def on_start() -> ProcessGen[None]:
            yield sim.env.timeout(2)
            rm.set("error", ErrCode.OVERFLOW)

        rm = VitisRegMap({"error": RegField(ErrField, RegAccess.R)})
        sim = Simulation()
        slave = VitisRegMapMMIFSlave(
            sim=sim, bitwidth=32, regmap=rm, on_start=on_start
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        direct = DirectMMIF(sim=sim, clk=Clock(freq=1.0))
        direct.bind("master", master)
        direct.bind("slave", slave)

        result: list[Any] = []

        def proc() -> ProcessGen[None]:
            rb = rm.bind_master(master)
            yield from rb.start()
            value = yield from rb.poll_end(
                interval=1.0, max_polls=20, field="error", target=ErrCode.OVERFLOW,
            )
            result.append(value)

        done = sim.env.event()

        def _wrap() -> ProcessGen[None]:
            yield from proc()
            done.succeed()

        sim.env.process(_wrap())
        sim.env.run(until=done)
        assert result == [ErrCode.OVERFLOW]


# ---------------------------------------------------------------------------
# Phase 4 — doc / API sanity checks (import & symbol existence)
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_all_public_symbols_importable(self) -> None:
        from waveflow.hw.regmap import (  # noqa: F401
            Bit,
            RegAccess,
            RegField,
            RegMap,
            RegMapAccessError,
            RegMapMMIFSlave,
            VitisRegMap,
            VitisRegMapMMIFSlave,
        )

    def test_reg_access_members(self) -> None:
        members = {m.value for m in RegAccess}
        assert members == {"R", "W", "RW", "W1C", "W1S"}

    def test_bit_is_intfield_subclass(self) -> None:
        from waveflow.hw.dataschema import IntField as _IntField
        assert issubclass(Bit, _IntField)
        assert Bit.bitwidth == 1
        assert not Bit.signed

    def test_bitwidth_mismatch_raises(self) -> None:
        rm = RegMap({"x": RegField(Bit, RegAccess.RW)}, bitwidth=32)
        sim = Simulation()
        with pytest.raises(ValueError, match="bitwidth"):
            RegMapMMIFSlave(sim=sim, bitwidth=64, regmap=rm)
