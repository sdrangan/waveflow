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


def test_plain_multiplies_below_12_bits_are_luts():
    narrow = unit(8, 8, 32, 4, 8, 4, 10, 4).core.resource_structure()
    wide = unit(8, 8, 32, 4, 8, 4, 12, 4).core.resource_structure()
    gauss = unit(8, 8, 32, 4, 8, 4, 10, 3).core.resource_structure()
    assert sum(g.count for g in narrow.multipliers) == 2  # the index products only
    assert (
        dict(zip(narrow.lut_ff_basis.names, narrow.lut_ff_basis.bases))["lut_mult"] > 0
    )
    assert sum(g.count for g in wide.multipliers) == 2 + 4 * 32
    assert sum(g.count for g in gauss.multipliers) == 2 + 3 * 32


def test_message_features():
    u = unit(8, 8, 32, 4, 8, 4, 12, 4)
    f = cost.message_features(u, MatmulOp.MUL, 8, 8, 32)
    assert f["tiles"] == 8 and f["sweep"] == 8 * (8 + 4 + 8 - 2)
    assert (
        f["out"] == 8 * 4 * 8 // 4 and f["b_load"] == 8 * 32 // 4 and f["a_load"] == 16
    )
    assert f["w_in"] == 3 + 32 + 128 and f["w_out"] == 3 + 128 and f["ah"] == 0
    assert cost.message_features(u, MatmulOp.MUL_AH, 8, 8, 32)["ah"] == 64


def test_fit_message_model_recovers_a_max_of_two_lines():
    rng = np.random.default_rng(3)
    true_c = {
        "intercept": 30.0,
        "sweep": 1.0,
        "out": 1.0,
        "b_load": 1.0,
        "a_load": 1.0,
        "tiles": 6.0,
    }
    true_io = {"intercept": 12.0, "w_in": 1.05, "w_out": 0.0, "ah": 2.0}
    rows = []
    for _ in range(120):
        r = {
            t: float(rng.integers(0, 400))
            for t in (*cost.COMPUTE_TERMS, *cost.IO_TERMS)
        }
        r["interval"] = cost.message_interval({"compute": true_c, "io": true_io}, r)
        rows.append(r)
    got = cost.fit_message_model(rows)
    for r in rows:
        assert cost.message_interval(got, r) == pytest.approx(r["interval"], rel=1e-6)


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
