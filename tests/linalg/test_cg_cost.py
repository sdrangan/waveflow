"""Step 8.4: the CG vector unit's cost-model forms (``waveflow.linalg.cg_cost``), before calibration.

The counted halves are checked against what csynth measured on the step 8.2 and 8.3 builds (Vitis
HLS 2024.1): DSPs and block RAM of the core, the other tasks' DSPs, and the block RAM of the unit's
channels (its stream-of-blocks buffers and the 64-bit memory adapters).  The fitted halves are
checked for their procedure, on synthetic data.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.linalg._cg_core_bench import cg_formats, stress_formats
from waveflow.calib.vitis_model import VitisResourceModel
from waveflow.linalg import cg_cost, cost
from waveflow.linalg.cg_vector import CgOp, CgVectorUnit
from waveflow.simulation.simulation import Simulation


def unit(K, N, L, formats, word=64) -> CgVectorUnit:
    return CgVectorUnit(
        name="u", sim=Simulation(), Kmax=K, Nmax=N, nitmax=K, L=L, formats=formats,
        word_bits=word,
    )  # fmt: skip


#: (configuration, core DSP, core BRAM, channel BRAM or None) as csynth reported them in steps
#: 8.2 (core only) and 8.3 (the unit fed from memory).
MEASURED = [
    ((8, 32, 4, cg_formats(12, 8)), 49, 0, 20),
    ((4, 32, 1, cg_formats(8, 0)), 4, 6, 16),
    ((16, 32, 2, stress_formats()), 25, 12, 16),
    ((16, 32, 16, cg_formats(16, 8)), 193, 0, None),
]


@pytest.mark.parametrize("cfg,dsp,bram,chan", MEASURED)
def test_counted_as_measured(cfg, dsp, bram, chan):
    u = unit(*cfg)
    m = VitisResourceModel(name="t", part=cost.PART)
    core = m.derived(u.core)
    assert (core["dsp"], core["bram"]) == (dsp, bram)
    assert [m.derived(c)["dsp"] for c in (u.rx, u.load, u.store)] == [1, 1, 1]
    if chan is not None:  # the four buffers plus the 64-bit adapters' 8 blocks
        assert cg_cost.channel_counted(u)["bram"] + 8 == chan


def test_dsps_per_lane():
    assert [cg_cost.dsps_per_lane(W) for W in (8, 10, 12, 14, 16)] == [3, 5, 12, 12, 12]


def test_message_features():
    u = unit(8, 32, 4, cg_formats(12, 8))
    s = cg_cost.message_features(u, CgOp.START, 8, 32)
    t = cg_cost.message_features(u, CgOp.STEP, 8, 16)
    assert (s["start"], s["start_rows"], s["start_groups"], s["rows"]) == (1, 64, 8, 0)
    assert (t["start"], t["start_rows"], t["start_groups"], t["rows"]) == (0, 0, 0, 32)
    assert t["groups"] == 4
    assert t["groups_div"] == 4 * (20 + 6)  # rz (20 bits) widened by g_div = 6
    assert s["w_in"] == s["w_out"] == 3 + 128 and t["w_in"] == 3 + 64


def test_fit_message_model_recovers_a_sum():
    rng = np.random.default_rng(4)
    true = {
        "intercept": 40.0,
        **dict(zip(cg_cost.MESSAGE_TERMS, (60, 1.1, 5.0, 3.2, 9, 0.4, 1.0, 1.2))),
    }
    rows = []
    for _ in range(80):
        r = {t: float(rng.integers(0, 300)) for t in cg_cost.MESSAGE_TERMS}
        r["interval"] = cg_cost.message_interval(true, r)
        rows.append(r)
    after_served = [{"w_in": w, "interval": 39.0} for w in (20, 80, 200)]
    after_reject = [{"w_prev": w, "interval": 30 + 1.06 * w} for w in (20, 80, 200)]
    got = cg_cost.fit_message_model(rows, after_served, after_reject)
    for t, value in true.items():
        assert got[t] == pytest.approx(value, rel=1e-6, abs=1e-6)
    kw = {"w_in": 150, "w_prev": 50}
    assert cg_cost.reject_interval(got, **kw, after_served=True) == pytest.approx(39.0)
    assert cg_cost.reject_interval(got, **kw, after_served=False) == pytest.approx(83.0)
    core = cg_cost.core_interval(got, CgOp.STEP, 8, 16, L=4, formats=cg_formats(12, 8))
    assert core == pytest.approx(40 + 3.2 * 32 + 9 * 4 + 0.4 * 104)


def test_models_refuse_another_platform():
    from waveflow.calib.platform import Platform

    other = Platform(name="other", dir=None, part=cost.PART, clk_freq=cost.CLK_HZ)
    u = unit(8, 32, 4, cg_formats(12, 8))
    for cls in (type(u), type(u.core), type(u.rx), type(u.load), type(u.store)):
        with pytest.raises(ValueError, match="describe the packaged platform"):
            cls.get_rm(other)


def test_packaged_platform_prices_the_centre_build():
    """The frozen step 8.4 models load from the package and price the centre calibration build
    (measured: unit 24,224 LUT, 14,482 FF, 52 DSP, 20 BRAM; a STEP of 8 x 32 every 1,411-1,413
    cycles)."""
    u = unit(8, 32, 4, cg_formats(12, 8))
    pred = cg_cost.predict_unit(u)["total"]
    assert (pred["dsp"], pred["bram"]) == (52, 20)
    assert pred["lut"] == pytest.approx(24224, rel=0.10)
    assert pred["ff"] == pytest.approx(14482, rel=0.10)
    feats = cg_cost.message_features(u, CgOp.STEP, 8, 32)
    assert cg_cost.message_interval(cg_cost.message_model(), feats) == pytest.approx(
        1412, rel=0.05
    )


def test_compose_prices_the_unit_like_predict_unit():
    from waveflow.calib.resource_model import compose

    u = unit(8, 32, 4, cg_formats(12, 8))
    u.add_rm(cost.platform())
    est = compose(u)
    want = cg_cost.predict_unit(u)["total"]
    for k in ("lut", "ff", "dsp", "bram"):
        assert est.total[k] == pytest.approx(want[k], abs=1)
