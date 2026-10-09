"""AC5 (gem5 part), AC6 and AC7 of ``plans/cpu_model.md``: the A53 platform's committed accuracy.

The numbers are read from the platform's committed tables -- the one-time test evaluation
(``cpu/accuracy.csv``) and the corpus -- so this test is cheap and needs no tools.

AC6 is accepted with a documented miss (``plans/cpu_model.md`` §14, user, 2026-10-08): three cycle
models miss the 25 % max bound on small operations.  Their maxima are **pinned** here, so a
recalibration that moves them shows up as a failure to be looked at, not as a silent change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from waveflow.cpu.calib.calibrate import MAX_BOUND, MEDIAN_BOUND, summarize

CPU = (
    Path(__file__).resolve().parents[2]
    / "waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1/cpu"
)

#: (family, target) -> the test-set max relative error accepted on 2026-10-08.
ACCEPTED_MISSES = {
    ("sched_ops.add", "cycles"): 0.393,
    ("sched_ops.delete", "cycles"): 0.290,
    ("dispatch", "cycles"): 0.381,
}


@pytest.fixture(scope="module")
def accuracy():
    return pd.read_csv(CPU / "accuracy.csv")


@pytest.fixture(scope="module")
def summary(accuracy):
    s = summarize(accuracy)
    return s[~s["informational"]]


def test_twins_every_measured_output_equals_its_python_twin():
    for path in sorted(CPU.glob("*/corpus.csv")):
        df = pd.read_csv(path)
        assert df["output_matches_twin"].all(), path.parent.name


def test_cycles_every_median_is_within_bound(summary):
    cyc = summary[summary["target"] == "cycles"]
    assert len(cyc) == 8
    assert (cyc["median"] <= MEDIAN_BOUND).all(), cyc


def test_cycles_max_is_within_bound_except_the_accepted_misses(summary):
    for row in summary[summary["target"] == "cycles"].itertuples():
        key = (row.family, row.target)
        if key in ACCEPTED_MISSES:
            assert row.max == pytest.approx(ACCEPTED_MISSES[key], abs=1e-3), key
        else:
            assert row.max <= MAX_BOUND, key


def test_energy_every_family_passes(summary):
    en = summary[summary["target"] == "energy_pj"]
    assert len(en) == 8
    assert (en["median"] <= MEDIAN_BOUND).all() and (en["max"] <= MAX_BOUND).all(), en


def test_every_test_prediction_was_interpolated(accuracy):
    assert set(accuracy["level"]) <= {"INTERPOLATED", "EXACT"}


def test_each_test_point_was_evaluated_once(accuracy):
    keys = accuracy[["family", "target", "point"]].apply(
        lambda r: (r.family, r.target, json.dumps(json.loads(r.point), sort_keys=True)),
        axis=1,
    )
    assert keys.is_unique
    assert accuracy["evaluated_at"].nunique() == 1


# ---------------------------------------------------------------------------
# AC8: area and leakage against McPAT on the held-out configurations (step 12).
# ---------------------------------------------------------------------------


def test_area_and_leakage_pass_on_the_test_configurations():
    acc = pd.read_csv(CPU / "area" / "accuracy.csv")
    s = summarize(acc)
    assert set(s["target"]) == {"area_mm2", "leak_mw"}
    assert (s["n"] == 8).all()
    assert (s["median"] <= MEDIAN_BOUND).all() and (s["max"] <= MAX_BOUND).all(), s
    assert set(acc["level"]) <= {"INTERPOLATED", "EXACT"}


def test_the_platform_prices_static_power_from_its_leakage_model():
    from waveflow.cpu.platform import CpuPlatform

    p = CpuPlatform.load()
    one, four = p.cpu_config(n_cores=1), p.cpu_config(n_cores=4)
    assert (
        50 < one.static_power_mw < 200
    )  # McPAT 22 nm: ~97 mW for one core and its 1 MiB L2
    assert (
        four.static_power_mw * 4 > one.static_power_mw
    )  # four cores leak more in total
    area = p.area_model().estimate(one)["area_mm2"]
    assert area.level.value in ("INTERPOLATED", "EXACT") and 2 < area.value < 5
