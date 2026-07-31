"""E1 + E2: compose a whole-design estimate, and validate it against measurements it never saw.

The composition rule is ``predict(comp) = comp's own model + Σ predict(child)``. Here that means four
module models plus the top's own interface term:

======================  ==================================================================
``FirCompute``          prior (DSP, BRAM — exact) + fitted (LUT, FF)
``FirCmdRx``            lookup — four configurations, one per sample width
``MemRStream``          lookup — **one** configuration across the whole grid
``MemWStream``          lookup — one configuration
``FirBlock`` (its own)  interface lookup, keyed on boundary structure
======================  ==================================================================

**The validation matters more than the model.**  The design totals in the corpus are never used to fit
anything — only the per-module figures are — so comparing a composed estimate against them is a genuine
held-out test of whether per-module composition reproduces whole-design synthesis.

And it leads with **decision fidelity** rather than relative error, because that is the claim the
numbers support and the one exploration actually needs: a model with 10% LUT error still picks the
right design when candidates are well separated, and picking right is the job.
"""
from __future__ import annotations

import pathlib

import numpy as np
import pytest

from waveflow.build.elaborate import elaborate
from waveflow.calib.confidence import ConfidenceLevel
from waveflow.calib.platform import Platform, platform_fallback_path
from waveflow.calib.resource_model import boundary_signature, compose
from examples.fir_block.fir_block import FirBlock, FirCompute
from examples.fir_block.fir_block_corpus import GRID, INTERFACE_BY_MEM_DWIDTH

MEM_DW = 32

#: **This example's own** library, not the packaged one.  ``fir_block``'s modules are its design, not
#: framework infrastructure, so their measurements live with the example rather than shipping to every
#: installed user.  The path mirrors what a user project has at its root (``calib/platforms/`` is the
#: default ``platforms_root``), because an example *is* a project — copy the directory and the layout
#: is already right.  Seeded from the packaged platform, so the framework's mem-stream records come
#: across too; see ``docs/guide/platform/create.md``.
_EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "examples" / "fir_block"
PLATFORM = Platform.resolve(_EXAMPLE / "calib" / "platforms", "zynq7020_bfm_100mhz",
                            fallbacks=platform_fallback_path())


def _top(ntap, samp_w, unroll, mem_dwidth=MEM_DW, modelled=True):
    top = elaborate(FirBlock, {"mem_dwidth": mem_dwidth, "ntap": ntap, "samp_w": samp_w,
                               "samp_i": 2, "unroll_lane": unroll}, name="fir_block")
    if modelled:
        top.add_rm(PLATFORM)       # <- the entire wiring: one call, recursing the hierarchy
    return top


def _compute(ntap, samp_w, unroll, mem_dwidth=MEM_DW):
    return elaborate(FirCompute, {"mem_dwidth": mem_dwidth, "ntap": ntap, "samp_w": samp_w,
                                  "samp_i": 2, "unroll_lane": unroll}, name="fir_compute")


# ---------------------------------------------------------------------------
# E1 — composition mechanics
# ---------------------------------------------------------------------------

def test_composition_covers_every_module():
    est = compose(_top(32, 16, False))
    assert {n for _, n, _, _ in est.per_module} == {
        "FirBlock", "FirCmdRx", "MemRStream", "FirCompute", "MemWStream"}


def test_the_tops_own_term_is_the_interface():
    est = compose(_top(32, 16, False))
    assert est.own == INTERFACE_BY_MEM_DWIDTH[32]


def test_a_module_with_no_model_is_reported_not_silently_skipped():
    est = compose(_top(32, 16, False, modelled=False), lambda c: None)
    assert est.total == {c: 0 for c in est.total}
    assert est.level is ConfidenceLevel.UNCALIBRATED
    assert all("not zero" in c.summary for _, _, _, c in est.per_module)


def test_the_estimate_reports_its_weakest_link():
    """A composed estimate is only as good as its worst part, and names which part that is."""
    est = compose(_top(256, 16, False))      # ntap far outside the fitted range
    assert est.level is ConfidenceLevel.EXTRAPOLATED
    assert "FirCompute" in {n for _, n, _ in est.weakest()}


def test_boundary_signature_ignores_the_compute_parameters():
    """The interface term keys on boundary shape, so compute knobs must not change it."""
    assert boundary_signature(_top(8, 8, False)) == boundary_signature(_top(32, 24, True))


def test_boundary_signature_moves_with_the_memory_width():
    assert boundary_signature(_top(32, 16, False, 32)) != boundary_signature(_top(32, 16, False, 64))


# ---------------------------------------------------------------------------
# E2 — held-out validation against design totals never used to fit
# ---------------------------------------------------------------------------

def _predicted_vs_measured():
    out = []
    for (n, w, u), m in sorted(GRID.items()):
        est = compose(_top(n, w, u))
        out.append(((n, w, u), est.total, m))
    return out


def test_dsp_and_bram_totals_are_exact():
    """The binding decisions compose exactly — prior plus zero interface DSP."""
    for key, pred, m in _predicted_vs_measured():
        assert pred["dsp"] == m["top_dsp"], f"{key}: dsp {pred['dsp']} != {m['top_dsp']}"
        assert pred["bram"] == m["top_bram"], f"{key}: bram {pred['bram']} != {m['top_bram']}"


def test_lut_and_ff_totals_meet_their_bound():
    """Composed whole-design LUT/FF against totals that fit nothing.

    Note the totals are *easier* than the compute module alone: the constant interface term and the
    three static modules are exact, so they dilute the fitted module's error.
    """
    for counter, key in (("lut", "top_lut"), ("ff", "top_ff")):
        errs = [abs(p[counter] - m[key]) / m[key] for _, p, m in _predicted_vs_measured()]
        assert np.mean(errs) < 0.08, f"{counter} mean {np.mean(errs):.1%}"
        assert max(errs) < 0.20, f"{counter} worst {max(errs):.1%}"


def test_decision_fidelity_the_claim_that_actually_matters():
    """Does the estimate *rank* designs the way synthesis does?

    The claim exploration needs. Absolute error can be 10% and every choice still correct, provided the
    ordering holds — so this is asserted directly rather than inferred from a regression score.
    """
    rows = _predicted_vs_measured()
    for counter, key in (("lut", "top_lut"), ("ff", "top_ff"), ("dsp", "top_dsp")):
        pred = [p[counter] for _, p, _ in rows]
        meas = [m[key] for _, _, m in rows]
        rho = np.corrcoef(np.argsort(np.argsort(pred)), np.argsort(np.argsort(meas)))[0, 1]
        assert rho > 0.93, f"{counter} rank correlation {rho:.3f}"


def test_the_cheapest_design_is_identified_correctly():
    """The decision an exploration actually makes: which point minimises DSP."""
    rows = _predicted_vs_measured()
    best_pred = min(rows, key=lambda r: r[1]["dsp"])[0]
    best_meas = min(rows, key=lambda r: r[2]["top_dsp"])[0]
    assert best_pred == best_meas


def test_a_truly_held_out_point():
    """Refit with one grid point removed, then predict its *design total*.

    The strictest form available here: that point's compute figures were not in the fit, and no design
    total ever is.
    """
    from examples.fir_block.fir_block_corpus import points
    from examples.fir_block.fir_block_resource import fir_compute_fitted

    held = (32, 16, False)
    top = _top(*held)
    # Re-fit the compute model without the held point and swap it in, leaving every other module's
    # attached model alone — the override path `model_for` still exists for exactly this.
    refit = fir_compute_fitted(platform=PLATFORM).fit(
        [(_compute(n, w, u), m) for n, w, u, m in points() if (n, w, u) != held])
    compute = next(c for c in top.sub_comps.values() if type(c).__name__ == "FirCompute")
    compute._resource_model = refit

    est = compose(top)
    m = GRID[held]
    assert est.total["dsp"] == m["top_dsp"]
    assert abs(est.total["lut"] - m["top_lut"]) / m["top_lut"] < 0.15
    assert abs(est.total["ff"] - m["top_ff"]) / m["top_ff"] < 0.15


def test_an_unmeasured_boundary_is_refused():
    """A boundary width nobody synthesized has no interface term, and the estimate says so."""
    est = compose(_top(32, 16, False, mem_dwidth=128))
    tops = [c for _, n, _, c in est.per_module if n == "FirBlock"]
    assert tops[0].level is ConfidenceLevel.UNCALIBRATED
    assert "cannot interpolate" in tops[0].summary
