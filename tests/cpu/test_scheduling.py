"""Step 4 / AC2 (first part) of ``plans/cpu_model.md``: exact scheduling timelines.

Every test runs at 1 Hz, so times are cycles; each expectation is worked out by hand in a comment.
"""

from __future__ import annotations

import pandas as pd
import pytest

from tests.cpu.conftest import fixed
from waveflow.calib.calib import LinCalibModel
from waveflow.calib.confidence import ConfidenceLevel
from waveflow.cpu import SwFunction


def times(run, name):
    r = run.rec(name)
    return (r.t_submit, r.t_start, r.t_end, r.core, r.wait_s)


def test_one_core_runs_in_submission_order_and_charges_each_switch(make_run):
    # a: 0..12 (switch 2 + 10).  b waits 12, then 12..19 (switch 2 + 5).
    run = make_run(switch_cycles=2)
    run.submit(0, fixed("a", 10)).submit(0, fixed("b", 5)).run()
    assert times(run, "a") == (0, 0, 12, 0, 0)
    assert times(run, "b") == (0, 12, 19, 0, 12)
    assert run.rec("b").switch_cycles == 2


def test_lower_prio_number_runs_first(make_run):
    # a holds the core 0..10.  At t=1, c (prio 5) then d (prio 1) arrive: d is more urgent,
    # so d runs 10..13 and c 13..17, even though c arrived first.
    run = make_run()
    run.submit(0, fixed("a", 10)).submit(1, fixed("c", 4), prio=5)
    run.submit(1, fixed("d", 3), prio=1).run()
    assert times(run, "d") == (1, 10, 13, 0, 9)
    assert times(run, "c") == (1, 13, 17, 0, 12)


def test_equal_priorities_are_first_come_first_served(make_run):
    run = make_run()
    run.submit(0, fixed("a", 10))
    for i, at in enumerate(
        (3, 1, 2)
    ):  # submitted out of name order; served by arrival time
        run.submit(at, fixed(f"t{i}", 1), prio=4)
    run.run()
    assert [r.name for r in run.cpu.records] == ["a", "t1", "t2", "t0"]
    assert [run.rec(n).t_end for n in ("t1", "t2", "t0")] == [11, 12, 13]


def test_two_cores_share_the_queue_and_the_lowest_free_core_wins(make_run):
    # a -> core 0, b -> core 1 at t=0; c waits for the first free core: core 0 at 10 (a ends) and
    # core 1 at 10 (b ends) tie, so the lowest index (0) takes it.  c: 10..20.
    run = make_run(n_cores=2)
    run.submit(0, fixed("a", 10)).submit(0, fixed("b", 10)).submit(0, fixed("c", 10))
    run.run()
    assert times(run, "a")[3] == 0 and times(run, "b")[3] == 1
    assert times(run, "c") == (0, 10, 20, 0, 10)
    rep = run.cpu.report()
    assert rep.elapsed_s == 20
    assert rep.core_busy_s == [20, 10]
    assert rep.utilization == [1.0, 0.5]


def test_the_first_task_on_a_core_pays_a_switch_and_a_resumed_core_does_not_skip_it(
    make_run,
):
    # The switch is charged whenever a core starts a task other than its last one -- including
    # its very first task.  Two different tasks back to back both pay it.
    run = make_run(switch_cycles=3)
    run.submit(0, fixed("a", 1)).submit(10, fixed("b", 1)).run()
    assert times(run, "a") == (0, 0, 4, 0, 0)
    assert times(run, "b") == (10, 10, 14, 0, 0)
    assert run.cpu.report().core_busy_s == [8]


def test_idle_time_counts_against_utilization(make_run):
    # Busy 0..4 and 10..14 over a 20 s horizon.
    run = make_run()
    run.submit(0, fixed("a", 4)).submit(10, fixed("b", 4)).run()
    rep = run.cpu.report(elapsed_s=20)
    assert rep.utilization == [pytest.approx(0.4)]


def test_queueing_delay_is_latency_minus_busy_time(make_run):
    # Three 5-cycle tasks on one core at t=0 with switch 1: ends at 6, 12, 18; waits 0, 6, 12.
    run = make_run(switch_cycles=1)
    for n in "xyz":
        run.submit(0, fixed(n, 5))
    run.run()
    assert [run.rec(n).wait_s for n in "xyz"] == [0, 6, 12]
    assert [run.rec(n).latency_s for n in "xyz"] == [6, 12, 18]
    st = run.cpu.report().functions
    assert st["z"].wait_mean_s == 12 and st["x"].latency_max_s == 6


def test_report_groups_calls_of_one_function(make_run):
    f = fixed("job", 4)
    run = make_run(n_cores=1)
    run.submit(0, f, key="j0").submit(0, f, key="j1").submit(0, f, key="j2").run()
    st = run.cpu.report().functions["job"]
    # ends at 4, 8, 12; waits 0, 4, 8.
    assert (st.n, st.cycles) == (3, 12)
    assert st.latency_mean_s == pytest.approx(8.0)
    assert (st.wait_mean_s, st.wait_max_s) == (4.0, 8.0)


def test_an_interrupt_on_a_free_core_pays_its_entry_overhead(make_run):
    run = make_run(switch_cycles=2, irq_entry_cycles=3)
    run.submit(0, fixed("isr", 5), irq=True).run()
    r = run.rec("isr")
    assert (r.t_end, r.cycles, r.switch_cycles, r.is_irq) == (10, 8, 2, True)


def test_report_confidence_is_the_weakest_call(make_run):
    fitted = LinCalibModel(basis=["n"], target="cycles")
    fitted.fit(pd.DataFrame({"n": [1, 2, 3, 4], "cycles": [11, 12.2, 12.9, 14.1]}))
    func = SwFunction(name="f", fn=lambda n: (None, {"n": n}), cycles=fitted)

    run = make_run()

    def caller(n):
        def proc():
            yield from run.cpu.execute(func, n)

        return proc

    run.sim.env.process(caller(2)())
    run.run()
    assert (
        run.cpu.report().functions["f"].confidence.level == ConfidenceLevel.INTERPOLATED
    )

    run.sim.env.process(caller(40)())  # outside the fitted range [1, 4]
    run.run()
    st = run.cpu.report().functions["f"]
    assert st.n == 2
    assert st.confidence.level == ConfidenceLevel.EXTRAPOLATED


def test_a_fixed_cost_reports_uncalibrated(make_run):
    run = make_run().submit(0, fixed("a", 1)).run()
    conf = run.cpu.report().functions["a"].confidence
    assert conf.level == ConfidenceLevel.UNCALIBRATED
