"""Step 10 of ``plans/cpu_model.md``: the committed pre-registration is what the code says, and every
held-out point is interior to the fit.

Interior is checked on the **features** (the counters and the working set), computed by the Python
twins — not on the arguments — because that is the space a cost model's confidence is judged in.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from waveflow.cpu.calib.kernels import KERNELS
from waveflow.cpu.calib.sweep import (
    FIT_SEEDS,
    TEST_SEEDS,
    VAL_SEEDS,
    area_rows,
    sweep_rows,
)

PLATFORM = (
    Path(__file__).resolve().parents[2]
    / "waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1/cpu"
)


def test_the_committed_sweep_plan_is_the_generated_one():
    df = pd.read_csv(PLATFORM / "sweep_plan.csv")
    committed = [(r.kernel, json.loads(r.point), r.role) for r in df.itertuples()]
    assert committed == [(k, dict(p), r) for k, p, r in sweep_rows()]


def test_the_committed_area_plan_is_the_generated_one():
    df = pd.read_csv(PLATFORM / "area_plan.csv")
    assert df.to_dict("records") == area_rows()


def test_roles_use_disjoint_seeds():
    for _, p, role in sweep_rows():
        if "seed" in p:
            allowed = {"fit": FIT_SEEDS, "validation": VAL_SEEDS, "test": TEST_SEEDS}[
                role
            ]
            assert p["seed"] in allowed


def test_every_held_out_point_is_inside_the_fitted_feature_range():
    feats: dict = defaultdict(lambda: defaultdict(list))
    for k, p, role in sweep_rows():
        kern = KERNELS[k]
        out = kern.run_twin(p)
        f = kern.counters_of(out)
        if kern.working_set is not None:
            f["ws"] = kern.working_set(out)
        feats[k + (":" + p["op"] if "op" in p else "")][role].append(f)
    outside = []
    for fam, roles in feats.items():
        fit = roles["fit"]
        lo = {c: min(x[c] for x in fit) for c in fit[0]}
        hi = {c: max(x[c] for x in fit) for c in fit[0]}
        for role in ("validation", "test"):
            for f in roles.get(role, []):
                outside += [
                    (fam, role, c, v) for c, v in f.items() if not lo[c] <= v <= hi[c]
                ]
    assert not outside, outside


def test_area_held_out_configurations_are_interior():
    rows = area_rows()
    fit = [r for r in rows if r["role"] == "fit"]
    for r in rows:
        if r["role"] == "fit":
            continue
        for key in ("n_cores", "l1_kb", "l2_kb"):
            vals = [f[key] for f in fit]
            assert min(vals) < r[key] < max(vals), (r, key)
