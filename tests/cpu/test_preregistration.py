"""AC12 of ``plans/cpu_model.md`` (pre-registration): measured after registering, fitted on fit only.

* every corpus row is a registered point, with its registered role, and was measured under the
  commit that added ``sweep_plan.csv``;
* each fitted model saw exactly its family's fit rows (its fit summary counts them);
* the test evaluation ran at a commit that descends from the registration.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from waveflow.cpu.calib.calibrate import FAMILIES, load_corpus
from waveflow.cpu.calib.prereg import SweepPlan, history_available, require_committed

ROOT = Path(__file__).resolve().parents[2]
CPU = ROOT / "waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1/cpu"


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(CPU)


def test_every_row_is_registered_with_its_role(corpus):
    plan = SweepPlan.load(CPU / "sweep_plan.csv")
    assert len(corpus) == len(plan.roles) == 305
    for row in corpus.itertuples():
        assert plan.role(row.kernel, json.loads(row.point)) == row.role


def _need_history():
    if not history_available(CPU / "sweep_plan.csv"):
        pytest.skip(
            "shallow clone: the registration commit is not in the local history"
        )


def test_every_row_was_measured_under_the_registration_commit(corpus):
    _need_history()
    plan = SweepPlan.load(CPU / "sweep_plan.csv")
    assert set(corpus["prereg_commit"]) == {plan.commit}


def test_each_model_saw_only_its_fit_rows(corpus):
    for fam in FAMILIES:
        n_fit = int((fam.select(corpus)["role"] == "fit").sum())
        for target in ("cycles", "energy_pj"):
            data = json.loads(
                (CPU / "models" / fam.name / f"{target}.json").read_text()
            )
            summary = next(
                v for k, v in data.items() if isinstance(v, dict) and "n_points" in v
            )
            assert summary["n_points"] == n_fit, (fam.name, target)


def test_the_test_evaluation_descends_from_the_registration():
    _need_history()
    acc = pd.read_csv(CPU / "accuracy.csv")
    (evaluated,) = set(acc["evaluated_at"])
    registered = require_committed(CPU / "sweep_plan.csv")
    run = subprocess.run(
        ["git", "merge-base", "--is-ancestor", registered, str(evaluated)],
        cwd=ROOT,
        check=False,
    )
    assert run.returncode == 0, (registered, evaluated)
