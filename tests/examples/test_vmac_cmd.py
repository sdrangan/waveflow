"""VMAC command/accelerator tests — encode/decode round-trips + the ``specialize`` cascade.

VMAC is **complex-only** and the numeric format is **structural** (on ``VmacAccel``), so
``VmacCmd`` carries only the op selector + reduce flag + geometry + operands — no format fields.
It is a plain ``DataList`` (an ``EnumField`` op, a ``BooleanField`` reduce, nested ``Region`` /
``Alpha`` sub-lists), so it must serialize / deserialize back to an identical value across word
widths — the wire-format contract for the HLS kernel.  The structural widths live on
``VmacAccel`` (an ``HwComponent`` with ``HwParam`` fields); its computed ``Cmd`` specializes the
command schema so a command's field widths track the silicon (``addr`` = ``mem_awidth`` bits; the
immediate complex ``imm`` = ``data_bw`` bits per component); same params → same schema object.
"""

import pytest

from examples.vmac.vmac import VmacAccel
from examples.vmac.vmac_datatypes import Alpha, OpCode, Region, VmacCmd
from waveflow.utils.fixputils import OMode, QMode

# a concrete accelerator: 32-bit addresses, 16-bit operands/immediates
ACCEL = VmacAccel(mem_dwidth=512, mem_awidth=32, data_bw=16, acc_bw=48, out_bw=12)
Cmd = ACCEL.Cmd


def _full_cmd():
    cmd = Cmd()
    cmd.op, cmd.reduce = OpCode.inner_prod, 1
    cmd.n_rows, cmd.n_cols = 6, 4
    cmd.a = {"addr": 0, "row_stride": 4}
    cmd.b = {"addr": 24, "row_stride": 8}  # row pitch wider than n_cols (sub-matrix)
    cmd.y = {"addr": 72, "row_stride": 4}
    cmd.alpha = {"direct": 1, "imm": (-16, 8), "addr": 0, "stride": 0}
    return cmd


# --- encode / decode round-trips ----------------------------------------------
@pytest.mark.parametrize("word_bw", [16, 32, 64])
def test_vmac_cmd_roundtrip(word_bw):
    cmd = _full_cmd()
    restored = Cmd().deserialize(cmd.serialize(word_bw), word_bw)
    assert restored.val == cmd.val


def test_nested_region_alpha_roundtrip():
    reg = Region(addr=12, row_stride=-3)  # negative pitch survives
    r2 = Region().deserialize(reg.serialize(32), 32)
    assert r2.val == reg.val == {"addr": 12, "row_stride": -3}

    al = Alpha(direct=1, imm=(-100, 77), addr=5, stride=-1)
    a2 = Alpha().deserialize(al.serialize(32), 32)
    assert a2.val == al.val
    assert a2.direct is True  # BooleanField -> Python bool
    assert int(a2.imm["re"]) == -100 and int(a2.imm["im"]) == 77


def test_default_cmd_roundtrips():
    cmd = Cmd()  # all defaults / zeros
    restored = Cmd().deserialize(cmd.serialize(32), 32)
    assert restored.val == cmd.val


def test_op_enum_field_roundtrips_all_members():
    for op in OpCode:
        cmd = _full_cmd()
        cmd.op = op
        restored = Cmd().deserialize(cmd.serialize(32), 32)
        assert OpCode(int(restored.op)) is op


def test_signed_fields_preserve_negatives():
    cmd = _full_cmd()
    cmd.a = {"addr": 10, "row_stride": -4}
    cmd.alpha = {
        "direct": 1,
        "imm": (-32768, -1),
        "addr": 0,
        "stride": 0,
    }  # data_bw=16 range
    restored = Cmd().deserialize(cmd.serialize(32), 32)
    assert restored.a.row_stride == -4
    assert (
        int(restored.alpha.imm["re"]) == -32768 and int(restored.alpha.imm["im"]) == -1
    )
    assert restored.val == cmd.val


def test_reduce_is_booleanfield():
    cmd = _full_cmd()
    assert isinstance(cmd.reduce, bool) and cmd.reduce is True
    cmd.reduce = 0
    assert cmd.reduce is False


def test_accel_q_o_mode_properties():
    # q_rnd / o_sat are structural (on the accelerator) — ap_fixed's compile-time Q/O.
    a = VmacAccel(data_bw=16, q_rnd=0, o_sat=0)
    assert a.types.q_mode is QMode.AP_TRN and a.types.o_mode is OMode.AP_WRAP
    b = VmacAccel(data_bw=16, q_rnd=1, o_sat=1)
    assert b.types.q_mode is QMode.AP_RND and b.types.o_mode is OMode.AP_SAT


# --- the instance -> type bridge + cascade ------------------------------------
def test_accel_cmd_shared_and_distinct():
    # instances are not cached, but the schema their HwParam widths specialize IS:
    # same widths -> the same cached Cmd class; different widths -> a different one.
    a = VmacAccel(mem_dwidth=512, mem_awidth=32, data_bw=16, acc_bw=48, out_bw=12)
    b = VmacAccel(mem_dwidth=999, mem_awidth=32, data_bw=16, acc_bw=99, out_bw=7)
    assert a.Cmd is ACCEL.Cmd  # Cmd depends only on (mem_awidth, data_bw)
    assert b.Cmd is ACCEL.Cmd
    c = VmacAccel(mem_awidth=24, data_bw=12)
    assert c.Cmd is not ACCEL.Cmd


def test_accel_is_hwcomponent_with_hwparams():
    from waveflow.hw.hw_component import HwComponent, _hw_param_names

    assert issubclass(VmacAccel, HwComponent)
    # the extractor sees every structural width — incl. the format moved off the command —
    # as a HwParam template param
    assert _hw_param_names(VmacAccel) >= {
        "mem_dwidth",
        "mem_awidth",
        "data_bw",
        "int_bits",
        "acc_bw",
        "out_bw",
        "q_rnd",
        "o_sat",
    }


def test_accel_carries_structural_params():
    assert (
        int(ACCEL.mem_dwidth),
        int(ACCEL.mem_awidth),
        int(ACCEL.data_bw),
        int(ACCEL.acc_bw),
        int(ACCEL.out_bw),
    ) == (512, 32, 16, 48, 12)


def test_cmd_specialize_cached_and_matches_accel():
    assert ACCEL.Cmd is VmacCmd.specialize(mem_awidth=32, data_bw=16)
    assert VmacCmd.specialize(mem_awidth=32, data_bw=16) is VmacCmd.specialize(
        mem_awidth=32, data_bw=16
    )
    assert issubclass(ACCEL.Cmd, VmacCmd)


def test_cmd_field_widths_track_mem_awidth_and_data_bw():
    # addr (and strides) follow mem_awidth; the immediate complex value's components follow data_bw
    region = ACCEL.Cmd.get_element_schema("a")
    alpha = ACCEL.Cmd.get_element_schema("alpha")
    assert region.get_element_schema("addr").get_bitwidth() == 32
    assert region.get_element_schema("row_stride").get_bitwidth() == 32
    imm = alpha.get_element_schema("imm")
    assert imm.inner_type.get_bitwidth() == 16  # per-component data_bw
    assert imm.get_bitwidth() == 32  # packed re/im = 2*data_bw

    wide = VmacAccel(mem_dwidth=256, mem_awidth=40, data_bw=24, acc_bw=64, out_bw=24)
    assert (
        wide.Cmd.get_element_schema("a").get_element_schema("addr").get_bitwidth() == 40
    )
    assert (
        wide.Cmd.get_element_schema("alpha")
        .get_element_schema("imm")
        .inner_type.get_bitwidth()
        == 24
    )


def test_region_alpha_specialize_cached_and_sized():
    assert Region.specialize(mem_awidth=32) is Region.specialize(mem_awidth=32)
    assert Region.specialize(mem_awidth=40) is not Region.specialize(mem_awidth=32)
    assert (
        Region.specialize(mem_awidth=40).get_element_schema("addr").get_bitwidth() == 40
    )
    assert Alpha.specialize(mem_awidth=32, data_bw=16) is Alpha.specialize(
        mem_awidth=32, data_bw=16
    )
    assert (
        Alpha.specialize(mem_awidth=32, data_bw=16)
        .get_element_schema("imm")
        .inner_type.get_bitwidth()
        == 16
    )
    assert (
        Alpha.specialize(mem_awidth=32, data_bw=24)
        .get_element_schema("imm")
        .inner_type.get_bitwidth()
        == 24
    )


def test_format_fields_removed_from_cmd():
    fields = set(VmacCmd.elements)
    # all numeric format + mode is structural (on VmacAccel) or derived — none on the command
    assert {
        "in_bw",
        "out_bw",
        "acc_bw",
        "int_bits",
        "shift",
        "q_rnd",
        "o_sat",
        "mode",
    }.isdisjoint(fields)
    # the command carries only the op + reduce + geometry + operands (no C / beta / op flags)
    assert fields == {"op", "reduce", "n_rows", "n_cols", "a", "b", "y", "alpha"}
