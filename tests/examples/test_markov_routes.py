"""markov synthesized both ways, against the record the docs quote (``plans/ip_packaging_routes.md``
Stage 6): a fresh run of ``markov_routes`` -- each route cached by content, so seconds once warm --
reproduces the committed ``docs/examples/markov/images/routes.json``'s area exactly, and its own
claims hold: the linked total is the parts plus the measured link term, and the per-device route
costs well under 1% LUT against the global one.

The record's seconds are not compared: the link re-runs on every call, and a run's seconds are a
measurement of the machine as much as of the design.  Needs Vivado and markov's csynth'd kernels.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.markov.markov_build import HERE, WORKSPACE, build_dag
from examples.markov.markov_routes import COUNTERS, DOCS, PARTS, RESULTS
from tests.examples._system_run import WORK, system_run

pytestmark = pytest.mark.xsi

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def fresh() -> dict:
    system_run(build_dag(work_dir=WORK), HERE, WORKSPACE, through="markov_routes")
    return json.loads((HERE / RESULTS[0]).read_text(encoding="utf-8"))


def test_a_fresh_run_reproduces_the_documented_area(fresh):
    doc = json.loads((REPO / DOCS[0]).read_text(encoding="utf-8"))
    for key in ("part", "period_ns", "global", "per_device", "gap"):
        assert fresh[key] == doc[key], (
            f"routes.json's {key} moved: re-run --through sync_routes_figure and update synth.md")


def test_the_linked_total_is_the_parts_plus_the_link_term(fresh):
    pd = fresh["per_device"]
    for k in COUNTERS:
        assert pd["linked"][k] == (sum(pd["parts"][p][k] for p in PARTS) + pd["top"][k]
                                   + pd["link_term"][k]), k


def test_per_device_costs_under_one_percent_of_area(fresh):
    assert abs(fresh["gap_pct"]["lut"]) < 1.0 and abs(fresh["gap_pct"]["ff"]) < 1.0
    assert fresh["gap"]["bram"] == 0
