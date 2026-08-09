"""P0 of ``plans/harmonize_calib.md`` — the equivalence gate the whole refactor is measured against.

Every prediction the calibration stack makes today is recorded here as a **golden snapshot**.  The
harmonization moves `ResourceModel` onto `CalibModel`, dissolves one class, renames two methods and
re-expresses a third — none of which is allowed to change a single number.  This file is what turns
that from an intention into a check.

Why a snapshot rather than assertions about values: the point is not that 9424 LUTs is *right* (the
model pages argue that separately) but that it is **unchanged**.  A refactor that silently shifted a
prediction by one would otherwise be indistinguishable from one that did not.

Regenerate deliberately with::

    python -m tests.calib.test_harmonize_equivalence --regenerate

and expect the diff to be empty.  A non-empty diff during the refactor is a bug until proven
otherwise.  Two exceptions were sanctioned, both deliberate and both recorded in the plan:

1. **P1's ``platform_dir`` key fix**, which moves module keys on purpose.
2. **P5's ``vlen512_dw256`` level**, ``INTERPOLATED`` -> ``EXTRAPOLATED``.  No *number* moved.  That
   point is ``LUTRAM_CORNER``, which ``tests/examples/test_vecmult.py`` already documents as
   under-predicted because the fit has no term for a buffer that became registers.  Before P5 the
   range check ran over the *derived* basis terms; it now runs over the recorded parameters, where
   ``mem0_depth=32`` is plainly below the fitted ``[64, 8192]``.  So the model stopped vouching for a
   point it was known to get wrong — the refactor made the confidence *more* honest, which is the one
   direction a changed level is allowed to move.
3. **VecMult's ``LutFfBasis`` switch**, which moved ``ff`` by **one** at the ``dwid=64`` points.  The
   basis is mathematically the same three terms as the ``PerLane``/``Crossbar`` declaration it
   replaced; only the column *order* into the least-squares solve changed.  The underlying value at
   those points is exactly ``597.5`` either way -- a rounding knife-edge, and ``round`` picks the
   side the last floating-point bit lands on.  Measured is 599, so neither answer is more right.
4. **The LUTRAM corner's ``lut``**, 6956 -> **7084**, which is the measured value.  The corner used
   to under-predict by 1.8% because the fit had no term for storage HLS moved into fabric;
   ``lutram_luts`` now prices it exactly.  A snapshot moving *onto* the measurement is the one
   direction this guard should never block.
5. **FirCompute's migration to ``VitisResourceModel``**, which moved every ``fir_block`` LUT/FF by
   under ~1%.  DSP and BRAM are unchanged and still exact.  The cause is the corpus, not the model:
   the old path hand-built 24 sample pairs from ``GRID`` while the new one reads the record store,
   which holds those 24 **plus two at ``mem_dwidth=64``**.  Those are valid in-regime measurements of
   the same module -- ``mem_dwidth`` reaches the basis through ``n_mult`` and ``store_bits`` -- so
   they are trained on rather than filtered out.  Measured leave-one-out over the grid, including
   them is a wash for LUT (9.78 -> 9.57% mean) and about 2pp worse on FF's worst case
   (18.85 -> 21.33%); excluding valid data to flatter a metric is exactly the trade this guard exists
   to make visible.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from waveflow.build.elaborate import elaborate
from waveflow.calib.platform import Platform
from waveflow.calib.resource_model import compose

REPO = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).resolve().parent / "golden" / "predictions.json"

#: The committed platform `fir_block` was calibrated against.  Using it rather than ``None`` is what
#: makes the snapshot meaningful: with no platform the three lookup modules resolve to nothing and
#: two thirds of the composed total silently disappears.
FIR_PLATFORM = REPO / "examples" / "fir_block" / "calib" / "platforms" / "zynq7020_bfm_100mhz"


def _round(d: dict) -> dict:
    """Counters are ints; anything else is coerced so JSON round-trips byte-identically."""
    return {k: (int(v) if isinstance(v, (int, float)) else v) for k, v in sorted(d.items())}


# ---------------------------------------------------------------------------
# Snapshot builders — one per axis
# ---------------------------------------------------------------------------

def _vecmult_snapshot() -> dict:
    """Every corpus point of `VecMult`, predicted through the installed model and ``compose``."""
    from examples.vecmult.vecmult import VecMult
    from examples.vecmult.vecmult_corpus import GRID

    out = {}
    for (vlen, dwid) in sorted(GRID):
        top = elaborate(VecMult, {"dwid": dwid, "vlen": vlen}, name="vec_mult")
        top.add_rm(None)                       # VecMult carries its own corpus; no platform needed
        est = compose(top)
        out[f"vlen{vlen}_dw{dwid}"] = {
            "total": _round(est.total),
            "level": est.level.value,
            "own": _round(top.resource_model.predict(top)),
        }
    return out


def _fir_block_snapshot() -> dict:
    """`FirBlock` composed against its committed platform — exercises all four model kinds at once.

    Lookup (the two mem-streams and the command receiver), fitted-with-prior (the compute) and
    interface (the composite's own term) all appear in one number, which is why this is the more
    demanding of the two snapshots.
    """
    from examples.fir_block.fir_block import FirBlock
    from examples.fir_block.fir_block_corpus import GRID

    plat = Platform(name="zynq7020_bfm_100mhz", dir=FIR_PLATFORM,
                    part="xc7z020clg484-1", clk_freq=100e6)
    out = {}
    for (ntap, samp_w, unroll) in sorted(GRID):
        params = {"mem_dwidth": 32, "ntap": ntap, "samp_w": samp_w,
                  "samp_i": 2, "unroll_lane": unroll}
        top = elaborate(FirBlock, params, name="fir_block")
        top.add_rm(plat)
        est = compose(top)
        out[f"ntap{ntap}_w{samp_w}_{'unroll' if unroll else 'serial'}"] = {
            "total": _round(est.total),
            "level": est.level.value,
            "per_module": {cls: _round(res) for _, cls, res, _ in est.per_module},
        }
    return out


def _timing_snapshot() -> dict:
    """`LinCalibModel` fitted on a fixed frame — the numeric layer both axes share.

    The full `TimingModel` needs an RTL/pysim corpus on disk, so it is exercised by its own tests
    rather than snapshotted here.  What matters for the refactor is that the *fitting* underneath is
    untouched, which this pins.
    """
    import pandas as pd

    from waveflow.calib.calib import LinCalibModel

    df = pd.DataFrame([
        {"nwords": n, "num_trans": t, "residual": 8.0 + 1.5 * n + 3.0 * t}
        for n in (4, 16, 64, 256) for t in (1, 2, 4)
    ])
    m = LinCalibModel(basis=["nwords", "num_trans"], target="residual").fit(df)
    rows = [{"nwords": 32, "num_trans": 3}, {"nwords": 512, "num_trans": 8}]
    return {
        "coeffs": {k: round(float(v), 9) for k, v in sorted(m.coeffs.items())},
        "predictions": [round(float(m.predict_feat(r)), 9) for r in rows],
        "levels": [m.confidence_feat(r).level.value for r in rows],
    }


def build_snapshot() -> dict:
    return {
        "vecmult": _vecmult_snapshot(),
        "fir_block": _fir_block_snapshot(),
        "timing_numeric": _timing_snapshot(),
    }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def golden() -> dict:
    if not GOLDEN.is_file():
        pytest.skip(f"no golden snapshot at {GOLDEN}; run with --regenerate")
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def current() -> dict:
    return build_snapshot()


@pytest.mark.parametrize("axis", ["vecmult", "fir_block", "timing_numeric"])
def test_predictions_are_unchanged(axis, golden, current):
    """Every recorded prediction still comes out identical.

    Parametrized per axis so a failure names which stack moved rather than dumping all three.
    """
    assert current[axis] == golden[axis], (
        f"{axis} predictions changed. If this is the sanctioned platform_dir key move from P1, "
        f"regenerate deliberately; otherwise it is a regression."
    )


def test_snapshot_covers_every_corpus_point():
    """The gate is only as good as its coverage — a snapshot missing points proves nothing."""
    from examples.fir_block.fir_block_corpus import GRID as FIR_GRID
    from examples.vecmult.vecmult_corpus import GRID as VEC_GRID

    snap = build_snapshot()
    assert len(snap["vecmult"]) == len(VEC_GRID) == 16
    assert len(snap["fir_block"]) == len(FIR_GRID) == 24


def test_snapshot_exercises_all_four_model_kinds():
    """`fir_block` must reach lookup, fitted, prior-inside-fitted and interface in one compose.

    If a later phase accidentally drops a module from the walk, the totals would still be *a*
    number — this is what notices that the number stopped covering everything.
    """
    snap = build_snapshot()
    one = next(iter(snap["fir_block"].values()))
    assert set(one["per_module"]) == {
        "FirBlock", "FirCmdRx", "MemRStream", "FirCompute", "MemWStream"}


if __name__ == "__main__":  # pragma: no cover - regeneration entry point
    import sys

    if "--regenerate" in sys.argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(build_snapshot(), indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(f"wrote {GOLDEN.relative_to(REPO)}")
    else:
        print(json.dumps(build_snapshot(), indent=2, sort_keys=True))
