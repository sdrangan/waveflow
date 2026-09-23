"""Every built-in producer step must let its output artifact be renamed.

``BuildDag.add`` refuses two producers of one artifact, so a step that hard-codes its ``produces``
key can appear only **once** per DAG.  That is a real restriction, not a theoretical one: one
``xsi/`` directory serves several tops, a calibration sweep collects a point per scenario, and a
flow may verify after C simulation and again after co-simulation.  In each case the *path* already
varies -- by top, by ``run_id``, by ``output_path`` -- and only the artifact name was stuck.

The consumer half of this was always configurable (``manifest_artifact``, ``vcd_artifact``,
``events_artifact``, ...).  These tests pin the producer half, and pin the defaults so that no
existing caller has to change.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.build.build import BuildDag, SourceStep
from waveflow.build.calib_steps import CalibBusStep, CollectTimingStep, FitTimingStep
from waveflow.build.cosim_steps import ExtractCosimTimingStep, ValidateTimingStep
from waveflow.build.trace_steps import (
    AddVcdTopStep,
    ExtractBurstsStep,
    RtlSimStep,
    TraceManifestStep,
)
from waveflow.build.verify_steps import FunctionalVerifyStep


def _noop(_config):
    return None


# (label, field naming the produced artifact, default value, factory(name, artifact) -> step)
#
# Each factory takes the two things that must differ between a DAG's two instances: the step name
# and the artifact name.  Anything else varies only where the step would otherwise write one path
# twice.
PRODUCERS = [
    (
        "CollectTimingStep", "collected_artifact", "timing_collected",
        lambda n, a: CollectTimingStep(
            name=n, run_pysim=_noop, run_id=n, collected_artifact=a),
    ),
    (
        "FitTimingStep", "fit_artifact", "timing_fit",
        lambda n, a: FitTimingStep(
            name=n, build_design=_noop, output_path=f"results/{n}.json", fit_artifact=a),
    ),
    (
        "CalibBusStep", "bus_artifact", "bus_calibrated",
        lambda n, a: CalibBusStep(name=n, run_id=n, bus_artifact=a),
    ),
    (
        "ExtractCosimTimingStep", "cosim_timing_artifact", "cosim_timing",
        lambda n, a: ExtractCosimTimingStep(
            name=n, top=n, output_path=f"results/{n}.json", cosim_timing_artifact=a),
    ),
    (
        "ValidateTimingStep", "verdict_artifact", "timing_verdict",
        lambda n, a: ValidateTimingStep(
            name=n, output_path=f"results/{n}.json", verdict_artifact=a),
    ),
    (
        "AddVcdTopStep", "dumper_artifact", "vcd_dumper",
        lambda n, a: AddVcdTopStep(
            name=n, comp_class=object, source_artifact="src", top=n, dumper_artifact=a),
    ),
    (
        "TraceManifestStep", "manifest_artifact", "trace_manifest",
        lambda n, a: TraceManifestStep(
            name=n, comp_class=object, source_artifact="src",
            output_path=f"results/{n}.json", manifest_artifact=a),
    ),
    (
        "RtlSimStep", "vcd_artifact", "trace_vcd",
        lambda n, a: RtlSimStep(name=n, top=n, tb="tb", vcd_artifact=a),
    ),
    (
        "ExtractBurstsStep", "events_artifact", "timing_events",
        lambda n, a: ExtractBurstsStep(
            name=n, output_path=f"results/{n}.json", events_artifact=a),
    ),
    (
        "FunctionalVerifyStep", "report_artifact", "verify_report",
        lambda n, a: FunctionalVerifyStep(
            name=n, golden_dir_artifact="golden_dir", actual_dir_artifact="actual_dir",
            report_path=f"results/{n}.json", report_artifact=a),
    ),
]

_IDS = [label for label, *_ in PRODUCERS]


def _rooted_dag(tmp_path: Path, *steps) -> BuildDag:
    """A DAG with a SourceStep for everything *steps* consume but do not themselves produce.

    A DAG rejects a step whose input nothing produces, so the graph has to be rooted.  The sources
    are derived from the steps rather than listed, because several of these steps consume an
    artifact that another one *produces* -- seeding that name up front would collide with the very
    producer under test.
    """
    produced = {name for step in steps for name in step.produces}
    consumed = {name for step in steps for name in step.consumes}

    dag = BuildDag()
    for art in sorted(consumed - produced):
        dag.add(SourceStep(artifact=art, path=tmp_path / art))
    return dag


@pytest.mark.parametrize("label,field_name,default,factory", PRODUCERS, ids=_IDS)
def test_default_artifact_name_is_unchanged(label, field_name, default, factory):
    """Renaming is opt-in: every caller predating the field keeps the name it already used."""
    step = factory("only", default)
    assert getattr(step, field_name) == default
    assert default in step.produces


@pytest.mark.parametrize("label,field_name,default,factory", PRODUCERS, ids=_IDS)
def test_two_instances_collide_on_the_default_name(label, field_name, default, factory, tmp_path):
    """The restriction this field removes -- shown, so the test fails if it ever comes back."""
    first, second = factory("first", default), factory("second", default)
    dag = _rooted_dag(tmp_path, first, second)
    dag.add(first)
    with pytest.raises(ValueError, match="already claimed"):
        dag.add(second)


@pytest.mark.parametrize("label,field_name,default,factory", PRODUCERS, ids=_IDS)
def test_two_instances_coexist_with_distinct_names(label, field_name, default, factory, tmp_path):
    """With a name each, both steps register -- and the second's artifact is the one it was given."""
    first, second = factory("first", f"{default}_first"), factory("second", f"{default}_second")
    dag = _rooted_dag(tmp_path, first, second)
    dag.add(first)
    dag.add(second)

    assert f"{default}_second" in second.produces
    assert default not in second.produces


@pytest.mark.parametrize("label,field_name,default,factory", PRODUCERS, ids=_IDS)
def test_run_publishes_under_the_chosen_name(label, field_name, default, factory):
    """`produces` and what `run()` returns must agree, or the DAG loses the artifact."""
    import inspect

    step = factory("only", f"{default}_renamed")
    src = inspect.getsource(type(step).run)
    # The old shape was a literal `{"timing_events": out}`; the key must come from the field now.
    assert f'"{default}"' not in src, (
        f"{label}.run still returns the hard-coded key {default!r}"
    )
