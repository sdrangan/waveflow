"""The LTBF engine (plans/mimo_cg/ltbf_cg_dse.md steps 4.1-4.2): pysim and the host-compiled body
against the fixed-point model, word for word."""

from __future__ import annotations

import pytest

from examples.ltbf_cg.hw import engine as eg
from examples.ltbf_cg.hw import host_check as hc


@pytest.mark.parametrize("explicit", [False, True], ids=["factored", "explicit"])
def test_pysim_matches_the_model_joint_and_per_ue(explicit):
    """A sector solved jointly, then UE by UE with H kept, then another sector."""
    s = eg.LtbfSystem(explicit=explicit)
    s.run()  # check() asserts every X word against the model
    assert s.tb.dut.jobs == len(s.jobs()) == 4


@pytest.mark.skipif(not hc.HLS_INCLUDE.is_dir(), reason="needs the Vitis HLS headers")
@pytest.mark.parametrize(
    "knobs",
    [
        {"explicit": False},
        {"explicit": True},
        {"explicit": False, "R": 4, "C": 2},
        {"explicit": False, "R": 1, "C": 4, "W": 12},
        {"explicit": True, "R": 4, "C": 4, "W": 20},
    ],
    ids=[
        "factored",
        "explicit",
        "factored-4x2",
        "factored-1x4-w12",
        "explicit-4x4-w20",
    ],
)
def test_the_hls_body_compiled_on_the_host_is_bit_exact(knobs, tmp_path):
    assert hc.host_check(eg.LtbfSystem(**knobs), tmp_path) == 0


def test_knobs_are_checked():
    with pytest.raises(ValueError):
        eg.LtbfSystem(R=3).run()
    with pytest.raises(ValueError):
        eg.LtbfSystem(explicit=True, R=2, C=4).run()
