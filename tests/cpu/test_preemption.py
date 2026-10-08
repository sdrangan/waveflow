"""Step 5 / AC2 (second part) of ``plans/cpu_model.md``: preemption and interrupts, exact timelines.

At 1 Hz; ``executed = floor((now - work_start) * f_clk)``; the preempted task re-queues its remaining
cycles at its original ``(prio, seq)`` and pays a switch when it resumes.
"""

from __future__ import annotations

from tests.cpu.conftest import fixed


def test_a_more_urgent_task_preempts_and_the_victim_resumes(make_run):
    # a (prio 5, 10 cycles): switch 0..1, work from 1.  b (prio 1, 3 cycles) arrives at 4:
    # a has executed 3, left 7.  b: switch 4..5, work 5..8.  a resumes: switch 8..9, work 9..16.
    run = make_run(preemptive=True, switch_cycles=1)
    run.submit(0, fixed("a", 10), prio=5).submit(4, fixed("b", 3), prio=1).run()
    a, b = run.rec("a"), run.rec("b")
    assert (b.t_start, b.t_end, b.wait_s) == (4, 8, 0)
    assert (a.t_start, a.t_end, a.n_preempted) == (0, 16, 1)
    assert (a.cycles, a.switch_cycles, a.busy_s, a.wait_s) == (10, 2, 12, 4)
    assert run.results["a"] == (16, "a")  # the result is released only at completion


def test_without_preemption_the_urgent_task_waits(make_run):
    # Same arrivals, run-to-completion: a ends at 11, then b: switch 11..12, work 12..15.
    run = make_run(preemptive=False, switch_cycles=1)
    run.submit(0, fixed("a", 10), prio=5).submit(4, fixed("b", 3), prio=1).run()
    assert run.rec("a").t_end == 11 and run.rec("a").n_preempted == 0
    assert (run.rec("b").t_start, run.rec("b").t_end, run.rec("b").wait_s) == (
        11,
        15,
        7,
    )


def test_preemption_during_the_switch_executes_nothing_and_loses_the_partial_switch(
    make_run,
):
    # switch 4.  a: switch 0..4, work would be 4..14.  b (prio 1, 3) at 2: a executed 0, paid a
    # partial switch of 2, left 10; the core holds no context.  b: switch 2..6, work 6..9.
    # a resumes: switch 9..13, work 13..23.
    run = make_run(preemptive=True, switch_cycles=4)
    run.submit(0, fixed("a", 10), prio=5).submit(2, fixed("b", 3), prio=1).run()
    a, b = run.rec("a"), run.rec("b")
    assert (b.t_end, b.switch_cycles) == (9, 4)
    assert (a.t_end, a.switch_cycles, a.busy_s, a.wait_s) == (23, 6, 16, 7)


def test_an_interrupt_preempts_even_without_preemptive_scheduling(make_run):
    # switch 1, entry 2.  a: switch 0..1, work 1..11.  isr (3 cycles) at 5: a executed 4, left 6.
    # isr: switch 5..6, work 3 + 2 entry = 5 cycles, 6..11.  a: switch 11..12, work 12..18.
    run = make_run(preemptive=False, switch_cycles=1, irq_entry_cycles=2)
    run.submit(0, fixed("a", 10), prio=5).submit(5, fixed("isr", 3), irq=True).run()
    isr, a = run.rec("isr"), run.rec("a")
    assert (isr.t_start, isr.t_end, isr.cycles, isr.is_irq) == (5, 11, 5, True)
    assert (a.t_end, a.n_preempted, a.switch_cycles) == (18, 1, 2)


def test_equal_priority_never_preempts(make_run):
    run = make_run(preemptive=True)
    run.submit(0, fixed("a", 10), prio=3).submit(4, fixed("b", 2), prio=3).run()
    assert run.rec("a").n_preempted == 0
    assert (run.rec("b").t_start, run.rec("b").t_end) == (10, 12)


def test_the_least_urgent_running_task_is_the_victim(make_run):
    # Two cores: a (prio 3) on core 0, c (prio 7) on core 1.  d (prio 1) at 2 preempts c, not a.
    # c executed 2, left 8; d: 2..4 on core 1; c resumes on core 1 at 4 (a still busy), 4..12.
    run = make_run(n_cores=2, preemptive=True)
    run.submit(0, fixed("a", 10), prio=3).submit(0, fixed("c", 10), prio=7)
    run.submit(2, fixed("d", 2), prio=1).run()
    a, c, d = run.rec("a"), run.rec("c"), run.rec("d")
    assert (a.n_preempted, a.t_end) == (0, 10)
    assert (d.core, d.t_start, d.t_end) == (1, 2, 4)
    assert (c.n_preempted, c.t_end, c.core) == (1, 12, 1)


def test_a_fraction_of_a_cycle_is_not_progress(make_run):
    # b (prio 1, 3) arrives at 2.5: a has run 2.5 cycles, floor -> 2 executed, left 8.
    # b: 2.5..5.5; a resumes 5.5..13.5.  The half cycle is busy time, not progress.
    run = make_run(preemptive=True)
    run.submit(0, fixed("a", 10), prio=5).submit(2.5, fixed("b", 3), prio=1).run()
    a = run.rec("a")
    assert run.rec("b").t_end == 5.5
    assert (a.t_end, a.busy_s, a.wait_s) == (13.5, 10.5, 3.0)


def test_a_preempted_task_resumes_ahead_of_later_equals(make_run):
    # a (prio 5) preempted at 1 by b (prio 1, 2 cycles); e (prio 5) arrived at 0.5, after a.
    # When b ends at 3, a (seq 0) outranks e (seq 1) and resumes first: 3..12, then e 12..14.
    run = make_run(preemptive=True)
    run.submit(0, fixed("a", 10), prio=5).submit(0.5, fixed("e", 2), prio=5)
    run.submit(1, fixed("b", 2), prio=1).run()
    assert [r.name for r in run.cpu.records] == ["b", "a", "e"]
    assert (run.rec("a").t_end, run.rec("e").t_end) == (12, 14)
