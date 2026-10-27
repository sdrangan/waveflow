"""The instruction word: one 64-bit DataList, encoded the same way by the fast path and the schema,
round-tripping, and spelled in RVV syntax."""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.vecunit import isa as I


def test_instruction_word_is_one_64_bit_word():
    assert I.VecInstr.get_bitwidth() == 64
    assert len(I.VecInstr().serialize(word_bw=64)) == 1
    assert len(I.VecJob().serialize(word_bw=64)) == 1
    assert len(I.VecResp().serialize(word_bw=64)) == 1


def test_encode_matches_the_schema_and_round_trips():
    rng = np.random.default_rng(0)
    widths = {"opcode": 7, "funct": 6, "chain": 1, "conj": 1, "bcast": 1, "rd": 8, "rs1": 8,
              "rs2": 8, "imm1": 12, "imm2": 12}
    for _ in range(300):
        f = {k: int(rng.integers(0, 1 << w)) for k, w in widths.items()}
        x = I.Instr(**{**f, "chain": bool(f["chain"]), "conj": bool(f["conj"]),
                       "bcast": bool(f["bcast"])})
        v = I.VecInstr()
        for k, val in f.items():
            setattr(v, k, val)
        assert x.encode() == int(v.serialize(word_bw=64)[0])
        assert I.Instr.decode(x.encode()) == x


@pytest.mark.parametrize("x, text", [
    (I.setvl(8), "wf.setvl 8"),
    (I.vmv_v_i(3, -5), "vmv.v.i v3, -5"),
    (I.vmacc_vs(0, 2, 2).c, "wf.vmacc.vs.c v0, s2, v2"),
    (I.vmul_vv(5, 0, 2, conj=True, bcast=True), "wf.vmulc.vb v5, v0, v2"),
    (I.vmacc_vv(1, 2, 3, conj=True), "wf.vmaccc.vv v1, v2, v3"),
    (I.vnmsac_vv(1, 2, 3), "vnmsac.vv v1, v2, v3"),
    (I.vdotr(4, 1, 1), "wf.vdotr s4, v1, v1"),
    (I.sdivz(2, 1, 0), "wf.sdivz s2, s1, s0"),
    (I.vlse(1, 0, 3, 8).c, "wf.vlse.c v1, qi0, 3, 8"),
    (I.vsse(1, 0, 2, 8), "wf.vsse qo1, v0, 2, 8"),
    (I.qpop(1), "wf.qpop qi1"),
    (I.qpush(0), "wf.qpush qo0"),
    (I.ld(1, 0, 8, 32), "wf.ld qi1, p0, 8, 32"),
    (I.st(0, 1, 7, 8, 32), "wf.st p0, qo1, 7, 8, 32"),
])
def test_disassembly(x, text):
    assert str(x) == text
    assert str(I.Instr.decode(x.encode())) == text


def test_signed_immediate_and_field_ranges():
    assert I.Instr.decode(I.vmv_v_i(0, -2048).encode()).simm == -2048
    with pytest.raises(ValueError):
        I.vmv_v_i(0, 2048)
    with pytest.raises(ValueError):
        I.vlse(256, 0, 0, 1).encode()
    with pytest.raises(ValueError):
        I.vadd_vv(0, 1, 2).__class__(I.Opcode.VEC, I.VOp.ADD, imm1=4096).encode()


def test_operand_forms_only_on_multiplying_instructions():
    for ctor in (I.vmul_vv, I.vmacc_vv, I.vnmsac_vv, I.vmadd_vv):
        assert ctor(0, 1, 2, conj=True, bcast=True).conj
    with pytest.raises(ValueError):
        I._v(I.VOp.ADD, 0, 1, 2, conj=True)
