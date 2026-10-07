"""Step 7.5: the systolic unit's cost-model forms (``waveflow.linalg.cost``), before calibration.

The counted halves are checked against what csynth measured on the step 7.3 and 7.4 builds
(Vitis HLS 2024.1): DSPs of the core and the unit, block RAM of the core's ``B`` store and of the
stream-of-blocks buffers.  The fitted halves are checked for their procedure, on synthetic data.
"""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.linalg import cost
from waveflow.linalg.systolic import MatmulOp, SystolicUnit
from waveflow.simulation.simulation import Simulation
from waveflow.utils.fixputils import Format, OMode, QMode


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


def unit(M, K, N, R, C, L, W, form, word=64) -> SystolicUnit:
    return SystolicUnit(
        name="u", sim=Simulation(), Mmax=M, Kmax=K, Nmax=N, R=R, C=C, L=L, form=form,
        word_bits=word, a=reg(W, 3), b=reg(W, 4), c=reg(W, 5),
    )  # fmt: skip


#: (configuration, core DSP, core BRAM) as csynth reported them in steps 7.3 and 7.4.
MEASURED = [
    ((8, 8, 32, 4, 8, 4, 12, 4), 130, 0),
    ((16, 16, 32, 16, 16, 4, 16, 4), 1026, 0),
    ((16, 16, 32, 1, 4, 1, 8, 3), 14, 8),
    ((8, 8, 32, 8, 32, 16, 14, 3), 770, 0),
]


@pytest.mark.parametrize("cfg,dsp,bram", MEASURED)
def test_core_counted_as_measured(cfg, dsp, bram):
    u = unit(*cfg)
    d = cost.VitisResourceModel(name="t", part=cost.PART).derived(u.core)
    assert (d["dsp"], d["bram"]) == (dsp, bram)


def test_unit_dsp_and_buffers_as_measured():
    """Step 7.4: the unit's DSPs (136, 20) and its buffers' block RAM (9 and 6: three 96-bit
    buffers of three blocks; three 16-bit buffers of two)."""
    centre, smallest = unit(8, 8, 32, 4, 8, 4, 12, 4), unit(16, 16, 32, 1, 4, 1, 8, 3)
    m = cost.VitisResourceModel(name="t", part=cost.PART)
    for u, want in ((centre, 136), (smallest, 20)):
        assert sum(m.derived(c)["dsp"] for c in (u.rx, u.load, u.core, u.store)) == want
    assert cost.channel_counted(centre)["bram"] == 9
    assert cost.channel_counted(smallest)["bram"] == 6


def test_form4_packs_narrow_products():
    """Four-multiply form: 4 DSPs per element from 12 bits, 3 at 10, 2 at 8 (measured, 7.5)."""
    m = cost.VitisResourceModel(name="t", part=cost.PART)
    for W, per in ((8, 2), (10, 3), (12, 4), (16, 4)):
        assert m.derived(unit(8, 8, 32, 4, 8, 4, W, 4).core)["dsp"] == 2 + per * 32
    assert m.derived(unit(8, 8, 32, 4, 8, 4, 8, 3).core)["dsp"] == 2 + 3 * 32


def test_buffers_at_32_bit_words():
    """The ``A`` buffer is split four ways with 32-bit words (12 blocks for 96-bit groups)."""
    u = unit(8, 8, 32, 4, 8, 4, 12, 4, word=32)
    assert cost.channel_counted(u)["bram"] == 12 + 3 + 3


def test_message_features():
    u = unit(8, 8, 32, 4, 8, 4, 12, 4)
    f = cost.message_features(u, MatmulOp.MUL, 8, 8, 32)
    assert f["tiles"] == 8 and f["sweep"] == 8 * (8 + 4 + 8 - 2)
    assert (
        f["out"] == 8 * 4 * 8 // 4 and f["b_load"] == 8 * 32 // 4 and f["a_load"] == 16
    )
    assert f["w_in"] == 3 + 32 + 128 and f["w_out"] == 3 + 128 and f["ah"] == 0
    assert cost.message_features(u, MatmulOp.MUL_AH, 8, 8, 32)["ah"] == 64


def test_fit_message_model_recovers_a_sum():
    rng = np.random.default_rng(3)
    true = {
        "intercept": 46.0,
        **dict(zip(cost.MESSAGE_TERMS, (1.0, 1.4, 0.6, 10.0, 1.2, 0.9, 1.3))),
    }
    rows = []
    for _ in range(60):
        r = {t: float(rng.integers(0, 400)) for t in cost.MESSAGE_TERMS}
        r["interval"] = cost.message_interval(true, r)
        rows.append(r)
    got = cost.fit_message_model(rows)
    for t, value in true.items():
        assert got[t] == pytest.approx(value, rel=1e-6, abs=1e-6)
    assert cost.reject_interval(got, 100) == pytest.approx(46.0 + 120.0)


def test_fit_channels_recovers_constants():
    rows = []
    for word in (32, 64):
        for cfg in ((8, 8, 32, 4, 8, 4, 12, 4), (16, 16, 32, 1, 4, 1, 8, 3)):
            u = unit(*cfg, word=word)
            c = cost.channel_counted(u)
            rows.append({"unit": u, "bram": c["bram"] + (6 if word == 32 else 8),
                         "lut": 1000 + 10 * word, "ff": 2000 + 20 * word})  # fmt: skip
    coef = cost.fit_channels(rows)
    assert coef["adapters_bram"] == {"32": 6, "64": 8}
    assert coef["lut"]["word"] == pytest.approx(10) and coef["ff"][
        "intercept"
    ] == pytest.approx(2000)


def test_packaged_platform_prices_the_centre_build():
    """The frozen step 7.5 models load from the package and price the centre calibration build
    (measured: unit 24,339 LUT, 14,543 FF, 136 DSP, 17 BRAM; full A·B interval 720 cycles).
    """
    u = unit(8, 8, 32, 4, 8, 4, 12, 4)
    pred = cost.predict_unit(u)["total"]
    assert (pred["dsp"], pred["bram"]) == (136, 17)
    assert pred["lut"] == pytest.approx(24339, rel=0.10)
    assert pred["ff"] == pytest.approx(14543, rel=0.10)
    feats = cost.message_features(u, MatmulOp.MUL, 8, 8, 32)
    assert cost.message_interval(cost.message_model(), feats) == pytest.approx(
        720, rel=0.05
    )


def test_compose_prices_the_unit_like_predict_unit():
    """``add_rm`` then ``compose``: the four tasks plus the unit's own share (its channels) add up
    to :func:`cost.predict_unit`'s total."""
    from waveflow.calib.resource_model import compose

    u = unit(8, 8, 32, 4, 8, 4, 12, 4)
    u.add_rm(cost.platform())
    est = compose(u)
    want = cost.predict_unit(u)["total"]
    for k in ("lut", "ff", "dsp", "bram"):
        assert est.total[k] == pytest.approx(want[k], abs=1)
    own = {path: res for path, _cls, res, _conf in est.per_module}
    # the unit's own block RAM: its buffers and the 64-bit m_axi adapters
    assert own["u"]["bram"] == cost.channel_counted(u)["bram"] + 8


def test_models_refuse_another_platform():
    from waveflow.calib.platform import Platform

    other = Platform(name="other", dir=None, part="xc7z020clg400-1", clk_freq=100e6)
    slower = Platform(name="slower", dir=None, part=cost.PART, clk_freq=200e6)
    for plat in (other, slower):
        with pytest.raises(ValueError, match="fitted for"):
            SystolicUnit.get_rm(plat)
        with pytest.raises(ValueError, match="fitted for"):
            type(unit(8, 8, 32, 4, 8, 4, 12, 4).core).get_rm(plat)
    assert SystolicUnit.get_rm(cost.platform()) is not None
