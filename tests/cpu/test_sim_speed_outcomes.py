"""The processor model's simulated outcome is pinned (``plans/sim_speed_cpu_linalg.md``, step 2).

``docs/evaluation/sim_speed/cpu.md`` makes the processor model's simulation faster; a speed-up is one
only if the simulation still does the same thing.  These runs are pinned to their unoptimized
outcome, every :class:`~waveflow.cpu.task.TaskRecord` exactly (``tests/cpu/_outcomes.py``):

* the micro-scheduler's 410-operation trace replayed on a real list through a :class:`Processor`
  (:class:`~waveflow.cpu.bench.SchedTrace`), whose prices must also add up to the untimed sum;
* the bench's two arrival patterns, 2,000 tasks each.

Re-record only for a change that is *meant* to change the outcome:
``python -m tests.cpu.test_sim_speed_outcomes --record``.
"""

from __future__ import annotations

import pytest

from tests.cpu import _outcomes as O
from waveflow.cpu.bench import SchedTrace, run_bench
from waveflow.cpu.processor import Processor

BENCH_TASKS = 2000
SECTION = "cpu"


def trace_outcome() -> tuple[dict, SchedTrace, Processor, dict]:
    """The pinned fingerprint of the trace replay, and the replay, its processor and its untimed sum."""
    st = SchedTrace(O.load_trace())
    untimed = st.untimed()
    cpu = st.pysim()
    out = {
        **O.fingerprint(cpu.records),
        "sim_s": cpu.now,
        "final": [list(g) for g in st.ready],
    }
    return out, st, cpu, untimed


def bench_outcome(pattern: str) -> dict:
    r = run_bench(pattern, BENCH_TASKS)
    return {**O.fingerprint(r["records"], per_record=False), "sim_s": r["sim_s"]}


def test_the_trace_replay_runs_the_trace_and_adds_up():
    """The timed replay runs the program the trace recorded, and charges what the untimed sum says."""
    got, st, cpu, untimed = trace_outcome()
    assert st.counters_match(), "the replay's counters differ from the trace's"
    assert got["final"] == [], "every job added was dispatched: the list ends empty"
    assert len(cpu.records) == untimed["calls"]
    assert sum(r.cycles for r in cpu.records) == pytest.approx(
        untimed["cycles"], rel=1e-9
    )
    assert sum(r.energy_pj for r in cpu.records) == pytest.approx(
        untimed["energy_pj"], rel=1e-9
    )


def test_the_trace_replay_outcome_is_pinned():
    got = trace_outcome()[0]
    want = O.load(SECTION)["trace"]
    O.assert_same(got, want, "trace replay")
    assert got["sim_s"] == want["sim_s"]
    assert got["final"] == want["final"]


@pytest.mark.parametrize("pattern", ["stream", "burst"])
def test_the_bench_outcome_is_pinned(pattern):
    got = bench_outcome(pattern)
    want = O.load(SECTION)[f"bench_{pattern}"]
    O.assert_same(got, want, f"bench {pattern}")
    assert got["sim_s"] == want["sim_s"]


def record() -> None:
    O.save(
        SECTION,
        {
            "trace": trace_outcome()[0],
            **{f"bench_{p}": bench_outcome(p) for p in ("stream", "burst")},
        },
    )


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--record"]:
        sys.exit("usage: python -m tests.cpu.test_sim_speed_outcomes --record")
    record()
    print(f"recorded section '{SECTION}' of {O.FIXTURE}")
