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
    from waveflow.cpu.calib.kernels import KERNELS

    for (
        kernel
    ) in KERNELS:  # the kernel corpora by name: cpu/area/ holds McPAT-only rows
        df = pd.read_csv(CPU / kernel / "corpus.csv")
        assert df["output_matches_twin"].all(), kernel


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


def test_cpu_config_keywords_override_the_platform_defaults():
    """Review fix: a keyword naming a platform default overrode nothing -- it raised TypeError --
    and an explicit ``static_power_mw`` was silently replaced by the leakage model's."""
    from waveflow.cpu.platform import CpuPlatform

    p = CpuPlatform.load()
    cfg = p.cpu_config(
        n_cores=2,
        name="mine",
        f_clk_hz=1.0e9,
        switch_cycles=10,
        static_power_mw=5.0,
        l2_bytes=512 * 1024,
    )
    assert (cfg.name, cfg.f_clk_hz, cfg.switch_cycles) == ("mine", 1.0e9, 10)
    assert cfg.static_power_mw == 5.0 and cfg.l2_bytes == 512 * 1024
    default = p.cpu_config(n_cores=2, l2_bytes=512 * 1024)
    assert default.name == p.name and default.switch_cycles == p.switch_cycles()
    leak = p.area_model().estimate(default)["leak_mw"].value
    assert default.static_power_mw == pytest.approx(leak / 2)


# ---------------------------------------------------------------------------
# Step 15 (informational): the A53 models at a second cache configuration, without refitting.
# ---------------------------------------------------------------------------


def test_the_cross_configuration_table_is_complete_and_measured_correctly():
    df = pd.read_csv(CPU / "cross_config.csv")
    assert len(df) == 56  # every registered test point
    assert set(df["l1d"]) == {16 * 1024} and set(df["l2"]) == {512 * 1024}
    assert df["output_matches_twin"].all()
    # The cache-sensitive kernel really moved, and its regime features carried the model with it.
    g = df[df["family"] == "gather_hist"]
    assert (g["measured"] / g["measured_at_reference"]).median() > 1.3
    assert g["rel_err"].max() <= MAX_BOUND


def test_the_tested_models_cannot_be_refitted_silently():
    """Rule 11: once the test set scored the saved models, refitting them is refused."""
    from waveflow.cpu.calib.calibrate import (
        ModelsAlreadyTested,
        fit_and_validate,
        fit_area,
    )

    with pytest.raises(ModelsAlreadyTested):
        fit_and_validate(CPU.parent)
    with pytest.raises(ModelsAlreadyTested):
        fit_area(CPU.parent)
