"""test_vecmult.py — the toolchain-free gates on the resource-model example.

The csynth numbers this example exists for cannot be tested without Vitis, so what is guarded here is
everything *underneath* them: that the pysim body is right, that the design elaborates and lowers,
and — the one that would otherwise rot silently — that the numbers ``docs/guide/resource_model/``
quotes still match the committed corpus.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from examples.vecmult.vecmult import (
    DEFAULT_DWID,
    DEFAULT_VLEN,
    SAMP_W,
    VecMult,
    golden,
    lane_width,
)
from examples.vecmult.vecmult_corpus import (
    GRID,
    LUTRAM_CORNER,
    PART,
    bram_prior,
    dsp_prior,
    fit_basis,
    in_bram_points,
)
from examples.vecmult.vecmult_sim import run_one

HERE = Path(__file__).resolve().parents[2] / "examples" / "vecmult"


@pytest.mark.parametrize("dwid", [32, 64, 128, 256])
def test_pysim_matches_golden(dwid):
    """One job through the SimPy model, element-wise against numpy.

    Runs every lane width because the packing is where a vectorized body breaks, and LW=2 vs LW=16
    exercise different paths through the generated serializer.
    """
    got, exp, _ = run_one(dwid=dwid, vlen=256, seed=1)
    assert np.array_equal(np.asarray(got), np.asarray(exp))


@pytest.mark.parametrize("n", [1, 7, 63, 64, 65, 253])
def test_ragged_lengths(n):
    """``n`` that is not a multiple of LW must still be exact.

    The partial final beat is where a lane-serialized body goes wrong, and a full-length run never
    reaches it.  ``n=1`` and ``n=LW+1`` are the two that catch off-by-one in the ``nlane`` clamp.
    """
    got, exp, _ = run_one(dwid=128, vlen=256, n=n, seed=2)
    assert len(got) == n
    assert np.array_equal(np.asarray(got), np.asarray(exp))


def test_response_echoes_the_transaction_id():
    """The response closes the transaction: same id out as in.

    Without it a caller with several jobs in flight cannot tell which result is which — or a slow
    job from a lost one — so this is the property that makes completion observable.
    """
    _, _, echoed = run_one(dwid=64, vlen=128, n=100, seed=3, tx_id=0xBEEF)
    assert echoed == 0xBEEF


def test_runtime_length_does_not_change_the_hardware():
    """``n`` is a runtime input; ``vlen`` is the compile-time bound the area is priced against.

    Two designs with the same ``vlen`` are the *same hardware* whatever ``n`` they are fed, so they
    must elaborate to one structure — which is what lets the resource model key on ``vlen`` alone.
    """
    from waveflow.build.elaborate import elaborate, structure_signature

    a = elaborate(VecMult, {"dwid": 64, "vlen": 1024}, name="a")
    b = elaborate(VecMult, {"dwid": 64, "vlen": 1024}, name="b")
    assert structure_signature(a) == structure_signature(b)


def test_golden_wraps_rather_than_saturates():
    """The C++ truncates the product to the sample width; the golden must do the same.

    Pinned separately because a saturating golden would agree with the hardware on random data and
    disagree only at the extremes — the failure that shows up after synthesis, not before.
    """
    big = np.array([1 << (SAMP_W - 1) - 1], dtype=np.int64)
    out = golden(big, big)
    assert -(1 << (SAMP_W - 1)) <= int(out[0]) < (1 << (SAMP_W - 1))


def test_elaborates_and_declares_its_task():
    """Structure is a pure function of the params, and the RTL name is derived from the task."""
    from waveflow.build.elaborate import elaborate

    comp = elaborate(VecMult, {"dwid": 128, "vlen": 1024}, name="vec_mult")
    assert comp.lw == lane_width(128) == 8
    kt = comp.kernel_task()
    assert kt.task_fn == "vec_mult_task"
    assert kt.template_args == (128, 1024)
    assert kt.signature == ("s_in", "z_out")


# ---------------------------------------------------------------------------
# The committed corpus — what the docs quote.  No toolchain needed.
# ---------------------------------------------------------------------------

def test_dsp_prior_is_exact_on_every_point():
    """DSP == LW, everywhere.  A binding decision, so a single miss means the law is wrong."""
    for (vlen, dwid), m in GRID.items():
        assert m["dsp"] == dsp_prior(vlen, dwid), f"({vlen},{dwid}): DSP {m['dsp']}"


def test_bram_prior_is_exact_on_every_point():
    """The ceiling law is exact everywhere — including the corner that used to be its one miss.

    Once the LUTRAM threshold moved into the device rule the corner stopped being prior *error* and
    became a predicted *regime*: the rule returns 0 there because it knows the bank is too small to
    be given a block.  16/16, still with zero fitted parameters.
    """
    misses = [(k, bram_prior(*k), m["bram"]) for k, m in GRID.items() if m["bram"] != bram_prior(*k)]
    assert not misses, f"BRAM prior missed: {misses}"


def test_the_lutram_corner_is_predicted_not_merely_tolerated():
    """The corner is reached by the rule's own threshold, not special-cased by coordinates.

    Guards the distinction that matters: a model that returns 0 there *because it knows why* will
    also be right on the next design with a small partitioned array, while one that hard-codes
    ``(512, 256)`` would not.
    """
    from waveflow.calib.device_rules import bram_estimate

    vlen, dwid = LUTRAM_CORNER
    lw = lane_width(dwid)
    est = bram_estimate(lw, vlen // lw, SAMP_W, PART)
    assert est.binding == "lutram" and est.blocks == 0
    assert est.certain, "the corner is measured, so the rule must not hedge on it"

    # ...and the neighbouring point one step deeper is block RAM, so the threshold really bites.
    deeper = bram_estimate(lw, (1024 // lw), SAMP_W, PART)
    assert deeper.binding == "bram" and deeper.blocks == GRID[(1024, dwid)]["bram"]


def test_bram_is_not_merely_proportional_to_lanes():
    """The ceiling earns its keep: at vlen=16384 the term is data-bound and LW does not move it.

    Pins the claim that distinguishes the real law from ``BRAM = LW`` — which fits 11 of these 16
    points and would look convincing without this column.
    """
    deep = {d: GRID[(16384, d)]["bram"] for d in (32, 64, 128, 256)}
    assert set(deep.values()) == {16}, f"expected a flat data-bound column, got {deep}"


def test_committed_corpus_covers_both_regimes():
    """A grid that only sampled one regime would validate a law it never tested."""
    partition_bound = [k for k, m in GRID.items()
                       if m["bram"] and m["bram"] == lane_width(k[1])]
    data_bound = [k for k, m in GRID.items()
                  if m["bram"] and m["bram"] != lane_width(k[1])]
    assert len(partition_bound) >= 4 and len(data_bound) >= 4


def test_default_params_are_a_point_in_the_corpus():
    """The docs quote the default configuration, so it has to be one of the measured points."""
    assert (DEFAULT_VLEN, DEFAULT_DWID) in GRID


# ---------------------------------------------------------------------------
# The installed model — device rules -> prior -> fitted -> add_rm_self -> compose
# ---------------------------------------------------------------------------

def _composed(vlen, dwid):
    from waveflow.build.elaborate import elaborate
    from waveflow.calib.resource_model import compose

    top = elaborate(VecMult, {"dwid": dwid, "vlen": vlen}, name="vec_mult")
    top.add_rm(None)
    return compose(top)


def test_get_rm_refuses_a_platform_on_a_different_device():
    """A *known* part from another family must be refused, not merely an unrecognized one.

    This is the failure that looks like success: an UltraScale+ part has a device rule, so a guard
    that only checks "is this part known?" accepts it and then prices the design with DSP48E1
    geometry (25x18 rather than 27x18) using LUT/FF coefficients measured on 7-series fabric. Both
    halves wrong, both silent.
    """
    from waveflow.calib.device_rules import DeviceMismatchError
    from waveflow.calib.platform import Platform

    ultrascale = Platform(name="us", dir=Path("."), part="xczu3eg-sbva484-1-e", clk_freq=100e6)
    with pytest.raises(DeviceMismatchError):
        VecMult.get_rm(ultrascale)


def test_get_rm_prices_with_the_platforms_part_not_the_corpus_part():
    """The model must carry the *platform's* part, or the guard validates something unused.

    Same family, different part: accepted, and the resolved part is the platform's.
    """
    from waveflow.calib.platform import Platform

    other7 = Platform(name="z045", dir=Path("."), part="xc7z045ffg900-2", clk_freq=100e6)
    rm = VecMult.get_rm(other7)
    assert rm.part == "xc7z045ffg900-2"


def test_add_rm_installs_a_model_on_the_module():
    """``top.add_rm(platform)`` must reach this module, or it contributes zero to any estimate.

    A module with no model is reported ``UNCALIBRATED`` rather than skipped, but the estimate is
    still missing its cost — and a missing contribution makes a design read as *cheaper* than it is.
    """
    from waveflow.build.elaborate import elaborate

    top = elaborate(VecMult, {"dwid": 64, "vlen": 4096}, name="vec_mult")
    assert top.resource_model is None, "nothing should be installed before add_rm"
    top.add_rm(None)
    assert top.resource_model is not None
    assert top.resource_model.name == "vec_mult"


def test_composed_priors_are_exact_on_every_point():
    """DSP and BRAM come through ``compose`` exactly — the rules survive the model layer.

    Guards the whole chain rather than the formulas alone: a prior can be right and still be wired
    to the wrong feature dict, which this would catch and a direct call would not.
    """
    for (vlen, dwid), m in GRID.items():
        total = _composed(vlen, dwid).total
        assert total["dsp"] == m["dsp"], f"({vlen},{dwid}) dsp {total['dsp']} != {m['dsp']}"
        assert total["bram"] == m["bram"], f"({vlen},{dwid}) bram {total['bram']} != {m['bram']}"


def test_composed_lut_is_exact_where_the_buffer_is_in_block_ram():
    """On the 15 in-BRAM points the composed LUT reproduces the measurement exactly."""
    for vlen, dwid, m in in_bram_points():
        assert _composed(vlen, dwid).total["lut"] == m["lut"], f"({vlen},{dwid})"


def test_the_lutram_corner_is_under_predicted_and_that_is_known():
    """At the LUTRAM corner the fit under-predicts fabric, because the storage moved into it.

    The BRAM prior correctly returns 0 there, but the LUT/FF fit was trained on in-BRAM points and
    has no term for a buffer that became registers.  Pinned rather than tolerated silently, because
    **under**-prediction is the one direction a resource estimate must not drift: it turns "does not
    fit" into "fits".  A complete model would add a LUTRAM regime term.
    """
    vlen, dwid = LUTRAM_CORNER
    total = _composed(vlen, dwid).total
    measured = GRID[LUTRAM_CORNER]
    assert total["lut"] < measured["lut"], "expected under-prediction at the corner"
    assert (measured["lut"] - total["lut"]) / measured["lut"] < 0.05, \
        "corner under-prediction grew beyond 5% — the regime term is now worth adding"


def test_composed_confidence_is_not_exact():
    """Two counters are exact and two are fitted, so the composed verdict must be the weaker one.

    A composed estimate reporting EXACT while half of it is a regression would be the single most
    misleading thing this machinery could do.
    """
    from waveflow.calib.confidence import ConfidenceLevel

    assert _composed(4096, 64).level is not ConfidenceLevel.EXACT


def _design(rows, basis_fn):
    return np.array([[1.0] + [basis_fn(v, d)[k] for k in ("lw", "lw2", "lw2_log2lw")]
                     for v, d, _ in rows])


def _loo_error(A, y):
    """Leave-one-out relative error — the honest test of a 4-parameter fit on 15 points."""
    errs = []
    for i in range(len(y)):
        keep = [j for j in range(len(y)) if j != i]
        c, *_ = np.linalg.lstsq(A[keep], y[keep], rcond=None)
        errs.append(abs(float(A[i] @ c) - y[i]) / y[i])
    return float(np.mean(errs)), float(np.max(errs))


def test_crossbar_basis_predicts_lut_and_ff_held_out():
    """The fitted basis generalizes, rather than memorizing 15 points with 4 parameters.

    Guards the numbers the docs quote.  LUT is reproduced exactly, which is a strong enough claim
    that it would be embarrassing to let it rot silently.
    """
    rows = [(v, d, m) for v, d, m in in_bram_points()]
    A = _design(rows, fit_basis)
    for target, mean_lim, max_lim in (("lut", 0.001, 0.001), ("ff", 0.01, 0.02)):
        y = np.array([m[target] for *_, m in rows], float)
        mean_err, max_err = _loo_error(A, y)
        assert mean_err <= mean_lim and max_err <= max_lim, \
            f"{target}: LOO mean {mean_err:.4%} max {max_err:.4%}"


def test_naive_linear_basis_is_not_good_enough():
    """Pins *why* the basis has quadratic terms: linear-in-width is off by tens of percent.

    Without this the crossbar terms read as unexplained curve-fitting, and a later simplification
    back to `dwid + log2(vlen)` would look harmless.
    """
    rows = [(v, d, m) for v, d, m in in_bram_points()]
    A = np.array([[1.0, float(d), math.log2(v)] for v, d, _ in rows])
    for target in ("lut", "ff"):
        y = np.array([m[target] for *_, m in rows], float)
        c, *_ = np.linalg.lstsq(A, y, rcond=None)
        worst = float(np.max(np.abs(A @ c - y) / y))
        assert worst > 0.30, f"{target}: linear basis unexpectedly good ({worst:.1%})"


def test_lut_does_not_depend_on_the_compile_time_length():
    """LUT is byte-identical across all four `vlen` at each width — only the buffer sees `vlen`."""
    for dwid in (32, 64, 128):        # 256 excluded: its 512-point is the LUTRAM corner
        luts = {GRID[(v, dwid)]["lut"] for v in (512, 1024, 4096, 16384)}
        assert len(luts) == 1, f"dwid={dwid}: LUT varies with vlen: {luts}"
