"""The system, bottom-up (pysim): each device alone in its harness against its golden, then the pair on
the bus against the system's golden, then the DAG through the steps that need no toolchain.  Every test
counts first: a device that stops early ends the run quietly, and only a count shows it."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build import NO_TOOLCHAIN, build_dag  # noqa: E402
from devices import Checksum, ScaleDev  # noqa: E402
from scale_msgs import Code  # noqa: E402
from system import golden, jobs_for, make_system  # noqa: E402
from waveflow.build.build import BuildConfig  # noqa: E402
from waveflow.hw.harness import harness  # noqa: E402


def _bursts(sysm, port):
    return [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[port].words]


@pytest.mark.parametrize("cls, port", [(ScaleDev, "m_status"), (Checksum, "m_out")])
def test_each_device_alone_matches_its_golden_burst_for_burst(cls, port):
    """The device's own ``golden("test")``, as the harness states it, against what pysim recorded."""
    sysm = harness(cls, workload="test")
    sysm.run()
    want = sysm.golden_outputs()[port]
    got = _bursts(sysm, port)
    assert len(got) == len(want), (len(got), len(want))             # the count first
    for g, w in zip(got, want):
        np.testing.assert_array_equal(g, np.asarray(w, dtype=np.uint64))


@pytest.mark.parametrize("kind", ["test", "error"])
def test_the_pair_on_the_bus_answers_every_job_and_nothing_after_an_error(kind):
    sysm = make_system(kind)
    got = sysm.run()
    want = golden(jobs_for(kind))
    assert len(got) == len(want), (len(got), len(want))             # the count first
    for g, w in zip(got, want):
        assert (g["tx_id"], g["code"]) == (w["tx_id"], w["code"])
        if w["code"] != Code.OK:
            continue
        assert g["nsat"] == w["nsat"]
        np.testing.assert_array_equal(g["y"], w["y"])
        assert g["sum"] == w["sum"]
    if kind == "error":
        assert want[-1]["code"] != Code.OK and len(want) < len(jobs_for(kind))


def test_the_scenario_has_a_job_of_more_than_one_burst_and_one_of_part_of_a_word():
    ns = [len(j.x) for j in jobs_for("test")]
    assert max(ns) > 1024 and any(n % 4 for n in ns)


def test_the_dag_runs_through_every_step_that_needs_no_toolchain(tmp_path):
    dag = build_dag()
    for step in NO_TOOLCHAIN:
        res = dag.run(BuildConfig(root_dir=tmp_path), through=step)
        assert step in res
        bad = {k: r.message for k, r in res.items() if not r.success}
        assert not bad, (step, bad)
