"""Step 13 of ``plans/cpu_model.md``: the micro-scheduler example, end to end in pysim.

The scheduler is software on the calibrated A53 platform; the accelerators are ``SimpFun`` behind
AXI-Lite.  These checks need no tools: they run the Python simulation only.
"""

from __future__ import annotations

import pytest

from examples.cpu_sched.cpu_sched import OPS, run
from waveflow.cpu.calib.kernels.sched_ops import _Counters, tg_insert, tg_remove


@pytest.fixture(scope="module")
def result():
    return run(n_jobs=120, n_accels=2, seed=3)


def test_every_job_runs_once_and_returns_the_right_answer(result):
    assert len(result.jobs) == 120
    assert all(j.dispatches == 1 for j in result.jobs.values())
    assert result.all_correct


def test_the_report_prices_every_scheduler_operation(result):
    rep = result.report
    assert set(rep.functions) == {*OPS, "dispatch"}
    assert (
        rep.functions["sched_ops.add"].n == rep.functions["sched_ops.delete"].n == 120
    )
    for st in rep.functions.values():
        assert st.cycles > 0 and st.energy_pj > 0
        assert st.confidence is not None and st.confidence.level.value != "UNCALIBRATED"
        assert sum(st.levels.values()) == st.n
    assert 0 < rep.utilization[0] <= 1
    assert rep.static_pj > 0 and rep.total_pj > rep.dynamic_pj


def test_the_trace_replays_to_the_same_counters(result):
    """The trace is the gem5 replay's input: re-applying it to an empty list must reproduce every
    operation's counters exactly, or the replay would measure a different program."""
    ready: list[tuple[int, int]] = []
    for t in result.trace:
        c = _Counters()
        n = len(ready)
        if t["op"] == "add":
            tg_insert(ready, (t["gid"], t["tg_prio"]), c)
        elif t["op"] == "delete":
            assert ready[0][0] == t["gid"]
            tg_remove(ready, t["gid"], c)
        elif t["gid"] >= 0:
            prio = next(p for g, p in ready if g == t["gid"])
            assert prio - 1 == t["tg_prio"]
            tg_remove(ready, t["gid"], c)
            tg_insert(ready, (t["gid"], t["tg_prio"]), c)
        assert (n, c.scanned, c.moved) == (
            t["n_tasks"],
            t["n_scanned"],
            t["n_moved"],
        ), t
    assert ready == []


def test_the_run_is_deterministic():
    a, b = run(n_jobs=40, seed=9), run(n_jobs=40, seed=9)
    assert a.trace == b.trace and a.sim_s == b.sim_s
