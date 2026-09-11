"""S1 gate -- the Python twiddle table must equal the Vitis one, bit for bit.

The golden (``golden/twiddle_L16_R4_W18_I2.json``) is produced by instantiating Vitis's own
``TwiddleTable`` template and dumping raw stored integers; see ``cpp/dump_twiddle.cpp`` and
``tools/regen_golden.sh``.  It is checked in, so these tests need neither Vitis nor a compiler.

Comparison is on **stored bits**, never on floats: a ``double`` round-trip can absorb the
1-LSB error this stage exists to catch.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from fft_bitexact.wf_fft.twiddle import (
    twiddle_bits,
    twiddle_complex_type,
    twiddle_ideal,
    twiddle_table,
)
from waveflow.hw.fixpoint import FixedField
from waveflow.utils import fixputils
from waveflow.utils.fixputils import OMode, QMode

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "twiddle_L16_R4_W18_I2.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN.read_text())


def _golden_arrays(g: dict) -> tuple[np.ndarray, np.ndarray]:
    return (np.array([e["re"] for e in g["table"]], dtype=np.int64),
            np.array([e["im"] for e in g["table"]], dtype=np.int64))


def test_golden_is_the_expected_shape(golden):
    """Guard the golden itself -- a truncated regen would make every other test vacuous."""
    assert golden["L"] == 16 and golden["R"] == 4
    assert golden["W"] == 18 and golden["I"] == 2
    assert golden["ext_len"] == 16
    assert len(golden["table"]) == 16


def test_twiddle_table_is_bit_exact(golden):
    """THE S1 GATE: Python == Vitis, stored bit for stored bit."""
    g_re, g_im = _golden_arrays(golden)
    re, im = twiddle_bits(16, 4)
    assert re.tolist() == g_re.tolist(), "twiddle real parts diverge from Vitis"
    assert im.tolist() == g_im.tolist(), "twiddle imag parts diverge from Vitis"


def test_axis_points_are_exact(golden):
    """+-1 and 0 must land exactly -- this is why the format has I=2 integer bits.

    ``ap_fixed<18,2>`` stores 1.0 as 2^16.  With I=1 it would saturate to just under 1 and every
    butterfly touching an axis twiddle would be off by an LSB.
    """
    g_re, g_im = _golden_arrays(golden)
    one = 1 << 16
    assert g_re[0] == one and g_im[0] == 0            # i=0  ->  1 - 0j
    assert g_re[4] == 0                               # i=L/4 -> 0 - 1j
    assert g_im[4] == ((-one) & ((1 << 18) - 1))      # -1 as an 18-bit pattern


def test_truncation_would_be_wrong(golden):
    """The test has teeth: ap_fixed's DEFAULT modes give a different table.

    ``TwiddleTable`` casts through ``T_roundingBasedCastType`` = ``ap_fixed<W,I,AP_RND,AP_SAT>``
    (``hls_ssr_fft_twiddle_table.hpp:62``), not the truncation typedef beside it, which is
    declared four times and never used.  Modelling this with ``AP_TRN``/``AP_WRAP`` -- the
    defaults, and the obvious guess -- is wrong at 10 of 16 entries.  If this test ever fails,
    someone has "simplified" the quantization mode.
    """
    g_re, g_im = _golden_arrays(golden)
    ideal = twiddle_ideal(16, 16)
    trn = FixedField.specialize(18, 2, True, QMode.AP_TRN, OMode.AP_WRAP)

    def stored(vals):
        return np.asarray(fixputils.to_bits(fixputils.quantize_real(vals, trn.get_format()), 18))

    n_re = int((stored(np.real(ideal)) != g_re).sum())
    n_im = int((stored(np.imag(ideal)) != g_im).sum())
    assert n_re == 7 and n_im == 7, (
        f"expected truncation to differ at 7 re / 7 im entries, got {n_re} / {n_im}")


def test_negation_happens_before_quantization(golden):
    """``imag = -sin(...)`` is negated in double, then quantized -- not quantized then negated.

    Under AP_RND (round half up, toward +inf) those are different operations.  Modelling it the wrong
    way round is a plausible mistake that this pins.
    """
    _, g_im = _golden_arrays(golden)          # only the imaginary path is under test here
    ft = FixedField.specialize(18, 2, True, QMode.AP_RND, OMode.AP_SAT)
    i = np.arange(16, dtype=np.float64)

    def stored(vals):
        return np.asarray(fixputils.to_bits(fixputils.quantize_real(vals, ft.get_format()), 18))

    before = stored(-np.sin(2.0 * i * np.pi / 16.0))          # correct order
    after = np.asarray(fixputils.to_bits(
        -fixputils.quantize_real(np.sin(2.0 * i * np.pi / 16.0), ft.get_format()), 18))
    assert before.tolist() == g_im.tolist()
    if after.tolist() == g_im.tolist():
        pytest.skip("L=16 does not discriminate the two orders; revisit at a larger L")


def test_carrier_is_a_complex_dataarray_S2_can_use():
    """The table must be a DataArray[ComplexField], not loose re/im arrays.

    S2 feeds it straight into ``complexfield.cmult``.  Handing back bare numpy would force the
    butterfly to re-wrap on every call and drop the format that carries growth tracking.
    """
    t = twiddle_table(16, 4)
    assert t.element_type is twiddle_complex_type()
    assert getattr(t.element_type, "is_complex_field", False)
    fmt = t.element_type.inner_format()
    assert (fmt.W, fmt.int_bits) == (18, 2)
    assert (fmt.q_mode, fmt.o_mode) == (QMode.AP_RND, OMode.AP_SAT)
