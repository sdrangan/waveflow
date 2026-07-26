"""tests/examples/test_mem_copy_timing.py — the writer records per-firing timing in a real pysim run.

The FreeRunMod mechanism and the TimingModel engine are unit-tested elsewhere; this is the
integration: MemCopy(calib_dir=...) attaches a StreamTimingModel to its writer, and a full
MemCopySim run populates firing_records with one row per job, shaped for collect_pysim.  With no
fitted params the delay is 0, so correctness and timing are unchanged — attaching a model only
*observes* until it is calibrated.
"""
from __future__ import annotations

import pytest

from examples.mem_copy.mem_copy import CopyJob
from examples.mem_copy.mem_copy_sim import MemCopySim


def test_uncalibrated_run_is_unchanged_and_records_nothing():
    """Default (no calib_dir): the writer has no model, no firing_records — the plain path."""
    sim = MemCopySim(jobs=(CopyJob(16, 512, 128),))
    dut = sim.run()
    assert dut.wstream.timing_model is None
    assert not hasattr(dut.wstream, "firing_records")


def test_calibrated_run_records_one_firing_per_job(tmp_path):
    jobs = (CopyJob(16, 4096, 128), CopyJob(200, 4300, 64), CopyJob(400, 4500, 128))
    sim = MemCopySim(jobs=jobs, calib_dir=str(tmp_path))
    dut = sim.run()

    recs = dut.wstream.firing_records
    assert len(recs) == len(jobs)
    # features track each job's word count; num_trans = ceil(nwords/16).
    assert [r["nwords"] for r in recs] == [128, 64, 128]
    assert [r["num_trans"] for r in recs] == [8, 4, 8]
    for r in recs:
        assert r["current_dly"] == 0.0            # unfitted seed -> no delay
        assert r["span"] > 0                       # a real firing span (seconds)
        assert {"nwords", "num_trans", "current_dly", "span"} <= set(r)


def _same_size_jobs(n, nwords=128):
    """n jobs of one size at DISTINCT, non-overlapping src+dst regions (a shared src or dst would
    let jobs clobber each other's patterns; stride 200 > 128 words keeps them apart)."""
    return tuple(CopyJob(16 + j * 200, 4096 + j * 200, nwords) for j in range(n))


def test_records_round_trip_through_collect_pysim(tmp_path):
    """firing_records is exactly what TimingModel.collect_pysim consumes."""
    sim = MemCopySim(jobs=_same_size_jobs(4), calib_dir=str(tmp_path))
    dut = sim.run()
    tm = dut.wstream.timing_model

    tm.collect_pysim(dut.wstream.firing_records, run_id="n128")
    pt = tm.get_params(tm.calib_dir / "pysim" / "n128", validate=False)
    assert pt["nwords"] == 128 and pt["num_trans"] == 8
    assert pt["span"] > 0 and pt["current_dly"] == 0.0


def test_a_fitted_model_injects_its_predicted_delay(tmp_path):
    """With fitted params the writer applies the delay per firing — the `timeout` in `run_iter`.

    Seed a constant 20-cycle residual and confirm every firing records (and the sim applies) that
    delay as time.  This is the mechanism that will close the 140->183 gap once the params come from
    a real fit.

    Note what is NOT asserted: that the *period* grows by 20 cycles.  It need not — a trailing delay
    on a stage that is not (yet) the bottleneck is absorbed into reduced idle-wait rather than added
    to the period.  That the period responds through the pipeline dynamics, not by mechanical
    addition, is the emergent-contention property the whole calibration rests on; validating the
    period is the job of the real RTL-vs-pysim closing-the-loop check, not this unit test."""
    sim = MemCopySim(jobs=_same_size_jobs(3), calib_dir=str(tmp_path))
    sim.tb.dut.wstream.timing_model._model.load_params(
        {"nwords": 0.0, "num_trans": 0.0, "intercept": 20.0})
    dut = sim.run()

    period = 1.0 / 100e6
    recs = dut.wstream.firing_records
    assert recs and all(r["current_dly"] == pytest.approx(20 * period) for r in recs)
