"""AC10 and AC11 (second part) of ``plans/cpu_model.md``: the scheduler trace on gem5 against the model.

One gem5 run of ``examples/cpu_sched/sched_replay.c`` in ``mode=total`` -- one measured region around
the whole back-to-back replay, so no per-operation markers perturb it -- against the platform model's
price for the same 410 operations and 200 dispatches.

AC10 is accepted with a documented miss (plan §14, user, 2026-10-08): the model over-predicts this
back-to-back sequence by 19.6 %.  The measurement and the error are **pinned**, so a change to the
tools, the trace or the models shows up here.  gem5 is deterministic: the cycle count is exact for
gem5 v25.1.0.1 (HPI, 1.2 GHz, DDR4_2400_8x8 x1) and Vitis 2024.1's gcc 12.2.0 at -O2.
"""

from __future__ import annotations

import time

import pytest

from examples.cpu_sched.replay import (
    _model_wall_s,
    load_trace,
    replay_total,
    replayable,
    trace_text,
)
from waveflow.cpu.calib.gem5 import Gem5Runner
from waveflow.cpu.platform import CpuPlatform

MEASURED_TOTAL = 63266.0
ACCEPTED_TOTAL_REL_ERR = 0.196


@pytest.mark.gem5
def test_the_replay_matches_the_trace_and_the_model_within_the_accepted_miss(tmp_path):
    runner = Gem5Runner(workdir=tmp_path)
    why = runner.unavailable()
    if why:
        pytest.skip(why)
    trace = load_trace()
    platform = CpuPlatform.load()

    from examples.cpu_sched.replay import REPLAY

    runner.build(REPLAY)
    t0 = time.perf_counter()
    res = replay_total(trace, runner=runner)
    gem5_wall = time.perf_counter() - t0
    assert res["counters_match"] is True
    assert res["total_cycles"] == MEASURED_TOTAL

    predicted = 0.0
    for t in trace:
        feats = {k: float(t[k]) for k in ("n_tasks", "n_scanned", "n_moved")}
        predicted += platform.model(f"sched_ops.{t['op']}").predict_feat(feats)
        if t["op"] == "delete":
            predicted += platform.model("dispatch").predict_feat({"n_dispatch": 1.0})
    rel = abs(predicted - MEASURED_TOTAL) / MEASURED_TOTAL
    assert rel == pytest.approx(ACCEPTED_TOTAL_REL_ERR, abs=2e-3)

    # AC11: pricing the trace through a Processor is at least 1,000x faster than gem5 running it.
    assert gem5_wall / _model_wall_s(platform, trace) >= 1000


def test_a_no_op_aging_call_is_not_replayed():
    """Review fix: an aging call that found nothing to promote (``gid`` -1) reached the C replay as
    ``r -1``, which parses as 0xFFFFFFFF and re-inserts a job that does not exist.  It is dropped
    before the trace is written, and ``trace_text`` refuses one."""
    trace = load_trace()
    noop = {"op": "reprio", "gid": -1, "n_tasks": 3.0, "n_scanned": 0.0, "n_moved": 0.0}
    with_noop = trace[:5] + [noop] + trace[5:]
    assert replayable(with_noop) == trace
    assert trace_text(replayable(with_noop)) == trace_text(trace)
    with pytest.raises(ValueError, match="no-op"):
        trace_text(with_noop)
