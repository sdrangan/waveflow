"""Step 7.1 (plans/mimo_cg/mimo_cg_paper_sims.md): formats, format ids and the rendered traits."""

from __future__ import annotations

import pytest

from waveflow.linalg import formats as F
from waveflow.utils.fixputils import Format, OMode, QMode


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


def traits(
    W: int, I: int, lane_bits: int = 16, family: str = "wf_test_traits"
) -> F.Traits:
    return F.Traits(
        family,
        (("a_t", reg(W, I)), ("acc_t", Format(2 * W + 6, 2 * I + 3, True))),
        (("a_mem", reg(W, I)),),
        lane_bits,
    )


def test_ap_type_and_mem_format():
    assert F.ap_type(reg(12, 3)) == "ap_fixed<12, 3, AP_RND, AP_SAT>"
    assert F.ap_type(Format(8, 8, False)) == "ap_ufixed<8, 8, AP_TRN, AP_WRAP>"
    # the memory format keeps the integer bits, widens the fraction, and drops the modes
    assert F.mem_format(reg(12, 3)) == Format(16, 3, True)
    assert F.mem_format(reg(8, 2), lane_bits=8) == Format(8, 2, True)
    with pytest.raises(ValueError, match="wider than the 16-bit lane"):
        F.mem_format(reg(18, 3))
    # two registers that differ only in their modes share one memory element type
    assert F.mem_elem_type(reg(12, 3)) is F.mem_elem_type(Format(12, 3, True))


def test_format_id_is_stable_and_content_derived():
    # pinned: a changed id silently re-keys every calibration record and stales every build
    assert F.format_id("x", reg(12, 3), 16) == F.format_id("x", reg(12, 3), 16)
    assert traits(12, 3).id == 1079894343
    assert 0 < traits(12, 3).id < 2**31
    assert traits(12, 3).id != traits(12, 4).id
    assert traits(12, 3).id != traits(12, 3, lane_bits=8).id
    assert traits(12, 3).id != traits(12, 3, family="wf_other_traits").id
    rnd = F.Traits("wf_test_traits", (("a_t", reg(12, 3)),))
    trn = F.Traits("wf_test_traits", (("a_t", Format(12, 3, True)),))
    assert rnd.id != trn.id  # a register's modes are part of its type


def test_traits_header_holds_one_specialization_per_id():
    a, b = traits(12, 3), traits(8, 2, lane_bits=8)
    text = F.render_traits_header([a, b, a])
    assert text.count("template <int ID> struct wf_test_traits;") == 1
    assert text.count(f"template <> struct wf_test_traits<{a.id}>") == 1
    assert text.count(f"template <> struct wf_test_traits<{b.id}>") == 1
    assert "typedef ap_fixed<12, 3, AP_RND, AP_SAT> a_t;" in text
    assert "typedef ap_fixed<8, 2, AP_RND, AP_SAT> a_t;" in text
    assert text.count('#include "complex__fixed16_3_array_utils.h"') == 1
    assert '#include "complex__fixed8_2_array_utils.h"' in text


def test_a_format_id_collision_raises(monkeypatch):
    monkeypatch.setattr(F, "format_id", lambda *parts: 7)
    with pytest.raises(ValueError, match="format id collision"):
        F.render_traits_header([traits(12, 3), traits(8, 2)])
    # the same content twice is no collision
    assert F.unique_traits([traits(12, 3), traits(12, 3)]) == [traits(12, 3)]
